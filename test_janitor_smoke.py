"""Smoke test for janitor.py.

Sprint 5.1.b (DD 2026-07-20 11:33 MSK): expanded sweep + expire-aware
heal. This test file covers:

  - legacy single-sweep paths (kept for regression detection)
  - NEW processed-state sweep (status IN ready/failed/skipped)
  - NEW exclude-publishing-and-published states from auto-delete
  - NEW heal_stuck_posts expire-aware (failed→draft only if not expired)

Seeded candidates live under a unique guid prefix so the smoke's
own deletes don't pollute prod, and the temp DB is created via
``make_isolated_db()``.

Run: ``python test_janitor_smoke.py`` from the project venv.
"""
from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _utcnow_naive() -> datetime:
    """Naive UTC, replacing deprecated datetime.utcnow() (Py 3.12+)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

PROJECT = Path(__file__).parent
sys.path.insert(0, str(PROJECT))

import janitor  # noqa: E402
from config import PIPE  # noqa: E402
from _smoke_lib import (  # noqa: E402
    make_isolated_db,
    patch_db_path,
    restore_db_path,
)


TEST_PREFIX = "[janitor-smoke-"  # unique marker for cleanup
HEAL_TEST_PREFIX = "[heal-smoke-"
EXPIRE_TEST_PREFIX = "[exp-smoke-"


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _seed_candidate(
    conn: sqlite3.Connection,
    *,
    status: str,
    expires_at: str | None,
    title: str,
    guid_prefix: str = TEST_PREFIX,
    category: str = "tech",
    half_life_h: float = 24.0,
) -> int:
    """Insert one minimal candidate. Returns id."""
    guid = f"{guid_prefix}{uuid.uuid4().hex[:8]}"
    cur = conn.execute(
        """
        INSERT INTO candidates (
            source_id, guid, url, title, status, safety_status,
            expires_at, base_score, weight, category, half_life_h
        ) VALUES (
            (SELECT id FROM sources LIMIT 1),
            ?, ?, ?, ?, 'review',
            ?, 5.0, 5.0, ?, ?
        )
        RETURNING id
        """,
        (guid, guid, title, status, expires_at, category, half_life_h),
    )
    return cur.fetchone()[0]


def _cleanup(conn: sqlite3.Connection, prefix: str) -> None:
    # Delete from candidates (cascading) — janitor.run_once uses raw
    # DELETE FROM candidates so the prefix-by-guid cleanup still
    # works as a normal SQL DELETE.
    cur = conn.execute(
        "DELETE FROM candidates WHERE guid LIKE ?", (prefix + "%",)
    )
    if cur.rowcount:
        print(f"  cleanup[{prefix}]: removed {cur.rowcount} smoke-test row(s)")
    conn.commit()


# ──────────────────────────────────────────────────────────────────────────
# Legacy single-sweep tests (regression detection)
# ──────────────────────────────────────────────────────────────────────────

def test_dry_run_does_not_delete() -> None:
    conn = sqlite3.connect(PIPE.db_path)
    try:
        past = (_utcnow_naive() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        _seed_candidate(conn, status="new", expires_at=past,
                        title="dry run expired")
        n_before, p_before = janitor.count_expired()
        # Call twice — count_expired must not be destructive.
        n_after,  p_after  = janitor.count_expired()
        _assert(n_before == n_after,
                f"count_expired NEW must be stable across calls: {n_before} != {n_after}")
        _assert(p_before == p_after,
                f"count_expired PROCESSED must be stable: {p_before} != {p_after}")
        # Sanity: SELECT the row is still there.
        still_there = conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE guid LIKE ?",
            (TEST_PREFIX + "%",),
        ).fetchone()[0]
        _assert(still_there == 1,
                f"dry-run path deleted our seed — found {still_there} rows")
        print(f"  PASS  test_dry_run_does_not_delete (n={n_before}, p={p_before})")
    finally:
        _cleanup(conn, TEST_PREFIX)
        conn.close()


def test_deletes_only_expired_new_items() -> None:
    """The legacy 'new sweep' must still behave correctly."""
    conn = sqlite3.connect(PIPE.db_path)
    try:
        past   = (_utcnow_naive() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        future = (_utcnow_naive() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

        expired_id = _seed_candidate(conn, status="new", expires_at=past,
                                     title="expired-new")
        keep_id_1  = _seed_candidate(conn, status="new", expires_at=future,
                                     title="future-new")
        # status='ready' is now part of PROCESSED sweep, not NEW sweep.
        # We assert via the run_once return value: deleted_new must be 1
        # only, deleted_processed must be 0, since this test sets no
        # processed candidates.
        conn.commit()

        deleted_new, kept_new, deleted_processed, kept_processed = janitor.run_once()

        survivors = {r[0] for r in conn.execute(
            "SELECT id FROM candidates WHERE id IN (?, ?)",
            (expired_id, keep_id_1),
        ).fetchall()}

        _assert(expired_id not in survivors,
                f"expired-new id={expired_id} survived; should have been deleted")
        _assert(keep_id_1 in survivors,
                "future-new was wrongly deleted")
        _assert(deleted_new == 1,
                f"expected deleted_new=1, got {deleted_new}")
        _assert(deleted_processed == 0,
                f"expected deleted_processed=0, got {deleted_processed}")
        _assert(kept_new == 1,
                f"expected kept_new=1 (only the future-new), got {kept_new}")
        print(f"  PASS  test_deletes_only_expired_new_items "
              f"(dn={deleted_new}, kn={kept_new}, dp={deleted_processed}, kp={kept_processed})")
    finally:
        _cleanup(conn, TEST_PREFIX)
        conn.close()


def test_is_idempotent() -> None:
    """Re-running must not throw and must not re-delete the same row."""
    conn = sqlite3.connect(PIPE.db_path)
    try:
        past = (_utcnow_naive() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        _seed_candidate(conn, status="new", expires_at=past, title="idempotency")
        conn.commit()

        d1_new, k1_new, d1_proc, k1_proc = janitor.run_once()
        d2_new, k2_new, d2_proc, k2_proc = janitor.run_once()
        _assert(d1_new > 0, "first run should have deleted our expired-new seed")
        _assert(d2_new == 0,
                f"second run deleted {d2_new} new(s); should be 0 (idempotent)")
        print(f"  PASS  test_is_idempotent (1st d_new={d1_new}, 2nd d_new={d2_new})")
    finally:
        _cleanup(conn, TEST_PREFIX)
        conn.close()


def test_list_candidates_returns_preview_rows() -> None:
    """list_candidates must include status column (Sprint 5.1.b)."""
    conn = sqlite3.connect(PIPE.db_path)
    try:
        past = (_utcnow_naive() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        id_seed = _seed_candidate(conn, status="new", expires_at=past, title="preview")
        conn.commit()

        rows = janitor.list_candidates(limit=50)
        matched = [r for r in rows if r["id"] == id_seed]
        _assert(len(matched) >= 1, "our seeded id must be in the list_candidates result")
        row = matched[0]
        for col in ("id", "title", "category", "status", "base_score", "weight",
                    "expires_at", "hours_left"):
            _assert(col in row.keys(),
                    f"list_candidates row must include column {col!r}")
        _assert(row["hours_left"] < 0,
                f"hours_left must be negative for past expiry, got {row['hours_left']}")
        print("  PASS  test_list_candidates_returns_preview_rows")
    finally:
        _cleanup(conn, TEST_PREFIX)
        conn.close()


def test_count_expired_matches_list() -> None:
    """count_expired() returned tuple + list_candidates size must agree,
    summed across both sweeps."""
    conn = sqlite3.connect(PIPE.db_path)
    try:
        past = (_utcnow_naive() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        # one NEW + one PROCESSED
        _seed_candidate(conn, status="new",    expires_at=past, title="new-sweep-count")
        _seed_candidate(conn, status="failed", expires_at=past, title="proc-sweep-count")
        conn.commit()

        n_new, n_proc = janitor.count_expired()
        n_list        = len(janitor.list_candidates(limit=10_000))
        _assert((n_new + n_proc) == n_list,
                f"count=(new={n_new}, proc={n_proc}) but list_size={n_list}")
        print(f"  PASS  test_count_expired_matches_list (n={n_new}, p={n_proc})")
    finally:
        _cleanup(conn, TEST_PREFIX)
        conn.close()


# ──────────────────────────────────────────────────────────────────────────
# Sprint 5.1.b: NEW processed-sweep + expire-aware heal
# ──────────────────────────────────────────────────────────────────────────

def test_processed_sweep_deletes_expired_ready_failed_skipped() -> None:
    """Status in (ready, failed, skipped) AND expires_at < now → DELETE.
    Status 'publishing' and 'published' must NOT be deleted, even if expired."""
    conn = sqlite3.connect(PIPE.db_path)
    try:
        past   = (_utcnow_naive() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        future = (_utcnow_naive() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

        # All 5 candidates expired; only 3 should be deleted.
        id_ready_expired    = _seed_candidate(conn, status="ready",    expires_at=past,
                                              title="expired-ready")
        id_failed_expired   = _seed_candidate(conn, status="failed",   expires_at=past,
                                              title="expired-failed")
        id_skipped_expired  = _seed_candidate(conn, status="skipped",  expires_at=past,
                                              title="expired-skipped")
        # Should NOT be auto-deleted:
        id_publishing_exp   = _seed_candidate(conn, status="publishing", expires_at=past,
                                              title="active-publishing-no-delete")
        id_published_exp    = _seed_candidate(conn, status="published",  expires_at=past,
                                              title="terminal-published-no-delete")
        conn.commit()

        deleted_new, kept_new, deleted_processed, kept_processed = janitor.run_once()

        survivors = {r[0] for r in conn.execute(
            "SELECT id FROM candidates WHERE id IN (?,?,?,?,?)",
            (id_ready_expired, id_failed_expired, id_skipped_expired,
             id_publishing_exp, id_published_exp),
        ).fetchall()}

        # Deleted from processed sweep
        _assert(id_ready_expired   not in survivors, "expired-ready survived")
        _assert(id_failed_expired  not in survivors, "expired-failed survived")
        _assert(id_skipped_expired not in survivors, "expired-skipped survived")
        # NOT deleted
        _assert(id_publishing_exp in survivors,
                "expired-publishing was wrongly deleted (it's in flight!)")
        _assert(id_published_exp in survivors,
                "expired-published was wrongly deleted (it's terminal)")

        _assert(deleted_processed >= 3,
                f"expected deleted_processed >= 3, got {deleted_processed}")
        print(f"  PASS  test_processed_sweep_deletes_expired_ready_failed_skipped "
              f"(deleted_processed={deleted_processed})")
    finally:
        _cleanup(conn, TEST_PREFIX)
        conn.close()


def test_processed_sweep_keeps_future_expiry() -> None:
    """ready/failed/skipped with future expires_at must NOT be deleted."""
    conn = sqlite3.connect(PIPE.db_path)
    try:
        future = (_utcnow_naive() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        id_ready_future    = _seed_candidate(conn, status="ready",  expires_at=future,
                                             title="future-ready")
        id_failed_future   = _seed_candidate(conn, status="failed", expires_at=future,
                                             title="future-failed")
        conn.commit()

        _, _, deleted_processed, _ = janitor.run_once()

        survivors = {r[0] for r in conn.execute(
            "SELECT id FROM candidates WHERE id IN (?, ?)",
            (id_ready_future, id_failed_future),
        ).fetchall()}
        _assert(id_ready_future in survivors, "future-ready was wrongly deleted")
        _assert(id_failed_future in survivors, "future-failed was wrongly deleted")
        _assert(deleted_processed == 0,
                f"expected deleted_processed=0, got {deleted_processed}")
        print(f"  PASS  test_processed_sweep_keeps_future_expiry (dp={deleted_processed})")
    finally:
        _cleanup(conn, TEST_PREFIX)
        conn.close()


def test_heal_failed_only_retries_non_expired() -> None:
    """heal_stuck_posts() must retry failed→draft only if candidate's
    expires_at is in the future. Expired failed posts must NOT be
    retried (they will be deleted by run_once() instead)."""
    conn = sqlite3.connect(PIPE.db_path)
    try:
        # Setup state: PIPE_TICKS.failed_retry_after_minutes=60 by
        # default. We need a draft_posts row updated_at more than 60
        # minutes ago, with status='failed'.
        # Two draft_posts, sharing the same candidate_id semantics:
        # - one with a non-expired candidate
        # - one with an expired candidate
        # We create separate candidates + separate draft_posts.
        far_past_update = (
            _utcnow_naive() - timedelta(hours=2)  # 120min > retry_min=60
        ).strftime("%Y-%m-%d %H:%M:%S")

        # Candidate 1: still alive (expires_at in future)
        cand_alive_id = _seed_candidate(
            conn, status="failed", expires_at=
            (_utcnow_naive() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            title="alive-candidate",
            guid_prefix=HEAL_TEST_PREFIX,
        )
        # Candidate 2: expired
        cand_expired_id = _seed_candidate(
            conn, status="failed", expires_at=
            (_utcnow_naive() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            title="expired-candidate",
            guid_prefix=HEAL_TEST_PREFIX,
        )
        # Two draft_posts: one per candidate, both failed, both old.
        # NOTE: schema column is 'updated_at' for last-bumped timestamp.
        # _claim sets it via DEFAULT (datetime('now')) so this is the
        # "stuck for 2h" signal we need.
        conn.execute(
            """
            INSERT INTO draft_posts (
                candidate_id, status, error_reason, created_at, updated_at
            ) VALUES (?, 'failed', 'transient_test_reason_a', ?, ?),
                     (?, 'failed', 'transient_test_reason_b', ?, ?)
            """,
            (cand_alive_id, far_past_update, far_past_update,
             cand_expired_id, far_past_update, far_past_update),
        )
        # Re-fetch both ids.
        rows = conn.execute(
            """
            SELECT id FROM draft_posts
             WHERE candidate_id IN (?, ?)
             ORDER BY candidate_id
            """,
            (cand_alive_id, cand_expired_id),
        ).fetchall()
        dp_alive_id = rows[0][0]
        dp_expired_id = rows[1][0]
        conn.commit()

        healed, retried = janitor.heal_stuck_posts()

        dp_alive_status = conn.execute(
            "SELECT status FROM draft_posts WHERE id=?", (dp_alive_id,),
        ).fetchone()[0]
        dp_expired_status = conn.execute(
            "SELECT status FROM draft_posts WHERE id=?", (dp_expired_id,),
        ).fetchone()[0]

        _assert(dp_alive_status == "draft",
                f"alive-candidate draft_posts status was '{dp_alive_status}', expected 'draft'")
        _assert(retried >= 1,
                f"expected retried >= 1, got {retried}")
        print(f"  PASS  test_heal_failed_only_retries_non_expired "
              f"(retried={retried}, alive_status={dp_alive_status!r}, "
              f"expired_status={dp_expired_status!r})")
    finally:
        # Cleanup both candidates + their draft_posts.
        _cleanup(conn, HEAL_TEST_PREFIX)
        conn.close()


def test_processed_sweep_includes_dry_run_preview() -> None:
    """list_candidates(include_processed=True) must include failed-with-past
    candidates too (not just 'new')."""
    conn = sqlite3.connect(PIPE.db_path)
    try:
        past = (_utcnow_naive() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        id_new = _seed_candidate(conn, status="new",    expires_at=past,
                                 title="dry-new")
        id_failed = _seed_candidate(conn, status="failed", expires_at=past,
                                    title="dry-failed",
                                    guid_prefix=EXPIRE_TEST_PREFIX)
        conn.commit()

        rows = janitor.list_candidates(limit=10_000, include_processed=True)
        ids = {r["id"] for r in rows}
        _assert(id_new in ids,    "dry-list missing our expired 'new'")
        _assert(id_failed in ids, "dry-list missing our expired 'failed' "
                                  "(include_processed=True must include processed)")

        rows_new_only = janitor.list_candidates(limit=10_000, include_processed=False)
        ids_new_only = {r["id"] for r in rows_new_only}
        _assert(id_failed not in ids_new_only,
                "dry-list (include_processed=False) unexpectedly contains 'failed'")
        print(f"  PASS  test_processed_sweep_includes_dry_run_preview "
              f"(with_proc_size={len(ids)}, new_only_size={len(ids_new_only)})")
    finally:
        _cleanup(conn, TEST_PREFIX)
        _cleanup(conn, EXPIRE_TEST_PREFIX)
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Sprint 5.2.2: janitor smoke runs against an isolated DB so
    # janitor.run_once() can't accidentally touch production rows.
    # The temp directory is auto-removed at process exit.
    db_path, conn = make_isolated_db(label="janitor")
    conn.close()
    original_db_path = patch_db_path(db_path)
    try:
        tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
        failures: list[tuple[str, BaseException]] = []
        for fn in tests:
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001
                failures.append((fn.__name__, exc))
                print(f"  FAIL  {fn.__name__}: {exc}")
        print("-" * 60)
        pass_n = len(tests) - len(failures)
        print(f"  {pass_n}/{len(tests)} passed")
        if failures:
            sys.exit(1)
        print("  ALL GREEN \u2713")
    finally:
        restore_db_path(original_db_path)
