"""Smoke test for Sprint 5.1 scoring integration in rewrite_and_score.

Verifies:
  - After a successful rewrite, candidates.base_score / category / half_life_h
    / weight / expires_at / scored_at are populated.
  - After a blocked LLM response, scoring columns are NOT populated.
  - _fetch_candidates ordering: high-weight candidates come before NULL-weight
    ones (NULL goes last, emulating NULLS LAST).

We do NOT exercise the LLM here. We directly poke rewrite_and_score._fetch_candidates
and rewrite_and_score._process with a mocked LLMClient that returns
canned rewrite output that includes priority+category.

Run: ``python test_rewrite_and_score_scoring_smoke.py`` from the project venv.

Each scenario seeds fresh candidates with a unique guid (smoke-prefix), then
cleans them up at the end so the smoke is idempotent and safe to re-run.
"""
from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).parent
sys.path.insert(0, str(PROJECT))

import rewrite_and_score  # noqa: E402
from config import PIPE  # noqa: E402
from models import RewriteOutput  # noqa: E402
from llm_client import RewriteResult  # noqa: E402
from _smoke_lib import (  # noqa: E402
    make_isolated_db,
    patch_db_path,
    restore_db_path,
)


TEST_PREFIX = "[scoring-smoke-"


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- Mock LLM that returns rewrite WITH scoring metadata --------------------
class MockLLMWithScore:
    """Returns a RewriteOutput including priority and category. The values
    are chosen so the test can predict what expires_at will look like."""

    def rewrite(self, article: dict):
        # Pull URL; we pick priority/category by substring like the
        # existing rewrite_and_score smoke does.
        url = article.get("url") or ""
        if "high-priority-tech" in url:
            priority, category = 9.0, "tech"
        elif "medium-priority-sports" in url:
            priority, category = 5.0, "sports"
        else:
            priority, category = 7.0, "tech"
        data = {
            "blocked": False,
            "title": f"Тестовый заголовок {priority}",
            "slug": f"scoring-test-{priority}",
            "excerpt": "Пересказ для проверки scoring smoke.",
            "content": "<p>Тело</p><p>Источник: " + (article.get("source_url") or "") + "</p>",
            "image_alt": "alt",
            "image_prompt": "A neutral stock image, no text",
            "categories": ["Tech"],
            "tags": ["smoke"],
            "meta_title": "meta",
            "meta_description": "desc",
            "telegram_teaser": "teaser",
            "priority": priority,
            "category": category,
        }
        return RewriteResult(
            data=data,
            usage={"prompt_tokens_provider": 50, "completion_tokens_provider": 30,
                   "thinking_tokens_provider": 0},
            metrics={"response_chars_local": 100, "reasoning_chars_local": 0,
                     "prompt_chars_local": 1000},
        )


# --- Mock LLM that returns BLOCKED (no scoring writes) ----------------------
class MockLLMBlocked:
    def rewrite(self, article: dict):
        return RewriteResult(
            data={"blocked": True, "reason": "violent", "title": "", "content": ""},
            usage={}, metrics={},
        )


# --- DB helpers --------------------------------------------------------------
def _seed_item(
    conn: sqlite3.Connection,
    *,
    title: str,
    url: str | None = None,
) -> int:
    guid = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
    cur = conn.execute(
        """
        INSERT INTO candidates (
            source_id, guid, url, title, status, safety_status, fetched_at
        ) VALUES (
            (SELECT id FROM sources LIMIT 1),
            ?, ?, ?, 'new', 'review', ?
        )
        RETURNING id
        """,
        (guid, url or guid,
         title,
         _utcnow_naive().strftime("%Y-%m-%d %H:%M:%S")),
    )
    return cur.fetchone()[0]


def _row(conn: sqlite3.Connection, candidate_id: int) -> sqlite3.Row | None:
    # SQLite.Row requires the connection factory; this helper makes its
    # own short-lived connection so we don't have to thread the factory
    # through every test.
    c = sqlite3.connect(PIPE.db_path)
    c.row_factory = sqlite3.Row
    try:
        cur = c.execute(
            """
            SELECT id, status, safety_status, base_score, category, half_life_h,
                   weight, expires_at, scored_at
              FROM candidates WHERE id=?
            """,
            (candidate_id,),
        )
        return cur.fetchone()
    finally:
        c.close()


def _cleanup(conn: sqlite3.Connection) -> None:
    n = conn.execute(
        "DELETE FROM candidates WHERE guid LIKE ?", (TEST_PREFIX + "%",)
    ).rowcount
    # Also delete any draft draft_posts we created in the test
    n_posts = conn.execute(
        """
        DELETE FROM draft_posts WHERE candidate_id NOT IN (SELECT id FROM candidates)
        """
    ).rowcount
    if n or n_posts:
        print(f"  cleanup: removed {n} candidates, {n_posts} orphan draft_posts")
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────

def test_rewrite_writes_score_columns() -> None:
    """Successful LLM → all 6 scoring columns get populated."""
    conn = sqlite3.connect(PIPE.db_path)
    try:
        candidate_id = _seed_item(conn, title="high-priority-tech test",
                             url="http://test/high-priority-tech-1")
        conn.commit()

        with rewrite_and_score._connect() as c2:
            # Use the production _fetch_candidates so the row shape
            # (with JOIN columns like source_url/source_name) matches.
            rows = rewrite_and_score._fetch_candidates(c2, limit=100)
            row_in = next(r for r in rows if r["id"] == candidate_id)
            _ = rewrite_and_score.process_one(MockLLMWithScore(), c2, row_in)
            c2.commit()

        row = _row(conn, candidate_id)
        _assert(row is not None, "row vanished")
        _assert(row["status"] == "ready", f"status: {row['status']}")
        # All 6 columns populated.
        _assert(row["base_score"] is not None and abs(row["base_score"] - 9.0) < 1e-9,
                f"base_score: {row['base_score']}")
        _assert(row["category"] == "tech",
                f"category: {row['category']}")
        _assert(row["half_life_h"] == 24.0,
                f"half_life_h for tech: {row['half_life_h']}")
        _assert(row["weight"] is not None and abs(row["weight"] - 9.0) < 1e-9,
                f"weight: {row['weight']}")
        _assert(row["expires_at"] is not None,
                "expires_at must be set")
        _assert(row["scored_at"] is not None,
                "scored_at must be set")

        # expires_at must be in the future for a 9.0 tech item.
        # Math check: lifetime_h = 24 * log2(0.5/9.0) ≈ 24 * 4.193 ≈ 100.6h.
        # We allow a minute of rounding jitter (Math.ceil to whole minute).
        exp = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
        delta_h = (exp - _utcnow_naive()).total_seconds() / 3600
        _assert(99 < delta_h < 102,
                f"tech 9.0 should live ~100.6h, got {delta_h:.1f}h")
        print(f"  PASS  test_rewrite_writes_score_columns (Δ={delta_h:.1f}h)")
    finally:
        _cleanup(conn)
        conn.close()


def test_blocked_does_not_write_score() -> None:
    """Blocked candidates must end up 'skipped' with safety_status='violent' and
    scoring columns all NULL."""
    conn = sqlite3.connect(PIPE.db_path)
    try:
        candidate_id = _seed_item(conn, title="blocked test")
        conn.commit()

        with rewrite_and_score._connect() as c2:
            rows = rewrite_and_score._fetch_candidates(c2, limit=100)
            row_in = next(r for r in rows if r["id"] == candidate_id)
            _ = rewrite_and_score.process_one(MockLLMBlocked(), c2, row_in)
            c2.commit()

        row = _row(conn, candidate_id)
        _assert(row["status"] == "skipped", f"status: {row['status']}")
        _assert(row["safety_status"] == "violent",
                f"safety_status: {row['safety_status']}")
        _assert(row["base_score"] is None,
                f"base_score must be NULL on blocked, got {row['base_score']}")
        _assert(row["category"] is None,
                "category must be NULL on blocked")
        _assert(row["expires_at"] is None,
                "expires_at must be NULL on blocked")
        _assert(row["weight"] is None,
                f"weight must be NULL on blocked, got {row['weight']}")
        print("  PASS  test_blocked_does_not_write_score")
    finally:
        _cleanup(conn)
        conn.close()


def test_fetch_candidates_orders_by_weight_then_nulls_last() -> None:
    """After a few rewrites, _fetch_candidates returns scored candidates first
    in weight-DESC order, NULL-weight candidates last."""
    conn = sqlite3.connect(PIPE.db_path)
    try:
        # Three scored candidates with predictable weights:
        #   weight=9.0 tech      →   highest, should be #1
        #   weight=5.0 sports    →   middle
        #   weight=7.0 tech      →   middle, but above 5.0
        #
        # Plus two unscored candidates (NULL weight) that should land last
        # regardless of published_at — they're behind all scored candidates.

        # We sidestep the LLM by writing scoring columns directly to keep
        # this test fast and deterministic.
        ids = []
        for title, weight, category, hl in [
            ("9-weight",   9.0, "tech",   24.0),
            ("5-weight",   5.0, "sports", 12.0),
            ("7-weight",   7.0, "tech",   24.0),
            ("NULL-1",     None, None,    None),
            ("NULL-2",     None, None,    None),
        ]:
            guid = f"{TEST_PREFIX}{uuid.uuid4().hex[:8]}"
            cur = conn.execute(
                """
                INSERT INTO candidates (
                    source_id, guid, url, title, status, safety_status,
                    fetched_at, base_score, category, half_life_h, weight
                ) VALUES (
                    (SELECT id FROM sources LIMIT 1),
                    ?, ?, ?, 'new', 'review',
                    ?, ?, ?, ?, ?
                )
                RETURNING id
                """,
                (guid, guid, title,
                 _utcnow_naive().strftime("%Y-%m-%d %H:%M:%S"),
                 weight, category, hl, weight),
            )
            ids.append((title, weight, cur.fetchone()[0]))
        conn.commit()

        with rewrite_and_score._connect() as c2:
            rows = rewrite_and_score._fetch_candidates(c2, limit=10)

        # Filter to just OUR seeded rows so other candidates in the DB
        # (real production data) don't pollute the ordering test.
        seeded_ids = {iid for _, _, iid in ids}
        ours = [r for r in rows if r["id"] in seeded_ids]
        _assert(len(ours) == 5,
                f"expected our 5 candidates in candidates, got {len(ours)}: {[r['title'] for r in ours]}")

        # The first three positions must be scored candidates in weight-DESC
        # order; the last two must be NULLs.
        head = ours[:3]
        tail = ours[3:]
        head_weights = [r["weight"] for r in head]
        _assert(head_weights == sorted(head_weights, reverse=True),
                f"scored candidates must be in weight DESC, got {head_weights}")
        _assert(all(r["weight"] is None for r in tail),
                f"unscored must come last, got weights: {[r['weight'] for r in tail]}")

        # Sanity: 9-weight is row #1, 7-weight is #2, 5-weight is #3.
        expected_order = ["9-weight", "7-weight", "5-weight", "NULL-1", "NULL-2"]
        actual_order = [r["title"] for r in ours]
        _assert(actual_order == expected_order,
                f"ordering wrong. expected {expected_order}, got {actual_order}")
        print(f"  PASS  test_fetch_candidates_orders_by_weight_then_nulls_last  {actual_order}")
    finally:
        _cleanup(conn)
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Sprint 5.2.2: scoring smoke runs against an isolated DB. The temp
    # directory is auto-removed at process exit. Without this, every
    # run risked polluting the production ``new`` queue with rows that
    # had `safety_status='review' but base_score=NULL` after a failed
    # cleanup.
    db_path, conn = make_isolated_db(label="scoring")
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
        print("  ALL GREEN ✓")
    finally:
        restore_db_path(original_db_path)


if __name__ == "__main__":
    main()
