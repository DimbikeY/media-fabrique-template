"""Sprint Y e2e smoke (DD 2026-07-20 22:50 MSK).

Walks the FULL state machine after WordPress across the TG/Telegraph
pipeline, with all external dependencies mocked:

    tick=wp_publish        (Stage 1, publisher.process_one → INSERT pending_tg_text)
        │
        ▼
    tick=generate_for_tg   (Stage 2, generate_for_tg.run → LLM regen)
        │ status='text_generated' → 'awaiting_approval' | 'approved'
        ▼
    tick=publish_tg        (Stage 3, publish_tg.run → Telegraph + TG sendMessage)
        │
        ▼ status='published_tg'

State machine invariants under test:

  ★ generate_for_tg SKIPS rows whose candidates.expires_at < NOW()
    (half-life filter, DD 2026-07-20 22:21 MSK — saves LLM tokens).
  ★ publish_tg SKIPS rows whose candidates.expires_at < NOW() at the
    moment of publish (defensive double-check; same rule).
  ★ publish_tg on Telegraph/TG exception:
      attempts += 1, status stays 'approved' for retry
      if attempts >= TG_MAX_TELEGRAPH_ATTEMPTS → 'telegram_blocked_exhausted'
  ★ generate_for_tg on LLM failure:
      attempts += 1, status stays 'pending_tg_text' for retry
      if attempts >= 5 → 'expired_skipped' (manual review)

External deps are all stubbed via unittest.mock.patch:

  WPClient     → fake Posts class
  tg_regenerate.tg_regenerate → fake LLM result row inserter
  tg_channel_publisher.publish → fake Telegraph+TG results
  PIPE.db_path → isolated temp DB (via _smoke_lib.make_isolated_db)

No real Telegram channel, Telegraph account, OpenAI call, or production
DB is touched. The test_y.db path DD used for snapshot backfill is not
used here either — this is greenfield.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

PROJECT = Path(__file__).resolve().parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from _smoke_lib import make_isolated_db


# ----------------------------------------------------------------------
# Tiny assertion helpers so the body reads top-down.
# ----------------------------------------------------------------------
def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _row_status_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM tg_dispatch GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def _seed_post_and_dispatch(
    conn: sqlite3.Connection,
    *,
    marker: str,
    expires_at: Optional[str] = None,
    dispatch_status: str = "pending_tg_text",
) -> int:
    """Insert a candidate + draft_posts + optional tg_dispatch row.

    Returns draft_posts.id. Used as the starting point for every test.
    Sets expires_at on candidates (None means "no scoring applied").
    """
    source_id = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO candidates (source_id, guid, url, title, body, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_id, marker, f"https://example.com/{marker}",
         f"Test post {marker}", "Body", expires_at),
    )
    candidate_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO draft_posts (candidate_id, title, slug, status, wp_post_url) "
        "VALUES (?, ?, ?, 'published', ?)",
        (candidate_id, f"T-{marker}", f"t-{marker}",
         f"https://media-<deploy-user>.local/{marker}"),
    )
    post_id = cur.lastrowid
    conn.execute(
        "INSERT INTO tg_dispatch (post_id, status, prompt_version, note) "
        "VALUES (?, ?, ?, NULL)",
        (post_id, dispatch_status, "master_prompt_tg.md@v1.0"),
    )
    conn.commit()
    return post_id


def _patch_pipe_db(db_path: Path):
    from config import PIPE
    return patch.object(PIPE, "db_path", str(db_path))


def _patch_tg_attrs(**kwargs):
    from config import TG
    return patch.multiple(TG, **kwargs)


# ----------------------------------------------------------------------
# Stage 3 (publish_tg) — no-op + half-life + retry + exhausted
# ----------------------------------------------------------------------
def test_publish_tg_no_op_when_nothing_approved() -> None:
    """Empty pipeline: publish_tg.run() returns counts with no work."""
    db_path, conn = make_isolated_db(label="e2e_pub_noop")
    try:
        # _seed_post_and_dispatch would create pending_tg_text;
        # here we want zero rows in tg_dispatch — confirm cleanup.
        counts = None
        with _patch_pipe_db(db_path):
            import publish_tg
            counts = publish_tg.run(limit=10)
        _assert(
            counts == {"published": 0, "blocked_retry": 0,
                       "blocked_exhausted": 0, "expired_skipped": 0,
                       "config_error": 0},
            f"unexpected counts on empty pipeline: {counts!r}",
        )
        print("  PASS  publish_tg.run() on empty pipeline returns zero counts")
    finally:
        conn.close()


def test_publish_tg_half_life_at_publish_filtered_out() -> None:
    """expires_at < NOW() at publish_tg → _next_approved_row filters the
    row out BEFORE process_one runs. The defensive expired_skipped path
    inside process_one only catches the rare race where expires_at
    ticked over mid-tick.

    Verifies (a) Telegraph is NOT called and (b) row stays 'approved'
    so /edit_tg or janitor can move it out manually.
    """
    db_path, conn = make_isolated_db(label="e2e_pub_hl")
    post_id = _seed_post_and_dispatch(
        conn, marker="hl-1",
        expires_at="2000-01-01 00:00:00",  # already past
        dispatch_status="approved",
    )
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now') WHERE post_id = ?",
        (post_id,),
    )
    conn.commit()
    conn.close()

    publish_calls = []

    def fake_publish(pid):
        publish_calls.append(pid)
        return {"message_id": 1, "message_url": "x", "telegra_url": "t", "blocked": False}

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch("tg_channel_publisher.publish", side_effect=fake_publish), \
             patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5):
            import publish_tg
            counts = publish_tg.run(limit=10)

    _assert(publish_calls == [], f"telegraph must NOT be called when expired: {publish_calls!r}")
    # Counters should be all zero — the row was filtered at SELECT time.
    for k, v in counts.items():
        _assert(v == 0, f"counts[{k}]={v} should be 0 (row filtered pre-fetch)")

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        row = c.execute(
            "SELECT status, attempts FROM tg_dispatch WHERE post_id=?",
            (post_id,),
        ).fetchone()
        _assert(row["status"] == "approved",
                f"expired row stays 'approved' (not transitioned): {row['status']!r}")
        _assert(row["attempts"] == 0,
                f"attempts stays 0 (no process_one call): {row['attempts']}")
    finally:
        c.close()
    print("  PASS  publish_tg: half-life filter at SELECT excludes expired (no telegraph call)")


def test_publish_tg_failure_retries_within_limit() -> None:
    """Telegraph/TG exception → attempts += 1, status stays 'approved'."""
    db_path, conn = make_isolated_db(label="e2e_pub_retry")
    post_id = _seed_post_and_dispatch(conn, marker="retry-1", dispatch_status="approved")
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now'), attempts=0 "
        "WHERE post_id=?",
        (post_id,),
    )
    conn.commit()
    conn.close()

    publish_calls = []

    def fake_publish(pid):
        publish_calls.append(pid)
        raise RuntimeError("telegraph 502")

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch("tg_channel_publisher.publish", side_effect=fake_publish), \
             patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5):
            import publish_tg
            counts = publish_tg.run(limit=10)

    _assert(publish_calls == [post_id], f"should retry once: {publish_calls!r}")
    _assert(counts["blocked_retry"] == 1, f"counts: {counts!r}")
    _assert(counts["blocked_exhausted"] == 0, f"counts: {counts!r}")

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        row = c.execute(
            "SELECT status, attempts FROM tg_dispatch WHERE post_id=?",
            (post_id,),
        ).fetchone()
        _assert(row["status"] == "approved", f"status after retry: {row['status']!r}")
        _assert(row["attempts"] == 1, f"attempts: {row['attempts']}")
    finally:
        c.close()
    print("  PASS  publish_tg: telegraph failure → attempts=1, status='approved'")


def test_publish_tg_attempts_exhausted_marks_blocked_exhausted() -> None:
    """attempts >= TG_MAX_TELEGRAPH_ATTEMPTS → 'telegram_blocked_exhausted'."""
    db_path, conn = make_isolated_db(label="e2e_pub_exh")
    post_id = _seed_post_and_dispatch(conn, marker="exh-1", dispatch_status="approved")
    # Start at attempts = MAX-1 so a single failure pushes over the edge.
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now'), attempts=4 "
        "WHERE post_id=?",
        (post_id,),
    )
    conn.commit()
    conn.close()

    def fake_publish(pid):
        raise RuntimeError("telegraph 502 again")

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch("tg_channel_publisher.publish", side_effect=fake_publish), \
             patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5):
            import publish_tg
            counts = publish_tg.run(limit=10)

    _assert(counts["blocked_exhausted"] == 1, f"counts: {counts!r}")
    _assert(counts["blocked_retry"] == 0, f"counts: {counts!r}")

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        status = c.execute(
            "SELECT status FROM tg_dispatch WHERE post_id=?", (post_id,)
        ).fetchone()["status"]
        _assert(status == "telegram_blocked_exhausted",
                f"status after exhaust: {status!r}")
    finally:
        c.close()
    print("  PASS  publish_tg: attempts>=MAX → telegram_blocked_exhausted")


def test_publish_tg_happy_path_marks_published_tg() -> None:
    """Successful Telegraph+TG call → 'published_tg', attempts=0."""
    db_path, conn = make_isolated_db(label="e2e_pub_ok")
    post_id = _seed_post_and_dispatch(conn, marker="ok-1", dispatch_status="approved")
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now'), attempts=0 "
        "WHERE post_id=?",
        (post_id,),
    )
    conn.commit()
    conn.close()

    def fake_publish(pid):
        return {
            "message_id": 12345,
            "message_url": f"https://t.me/your_channel/{12345}",
            "telegra_url": "https://telegra.ph/some-page-07-20",
            "blocked": False,
        }

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch("tg_channel_publisher.publish", side_effect=fake_publish), \
             patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5):
            import publish_tg
            counts = publish_tg.run(limit=10)

    _assert(counts["published"] == 1, f"counts: {counts!r}")

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        row = c.execute(
            "SELECT status, attempts FROM tg_dispatch WHERE post_id=?", (post_id,)
        ).fetchone()
        _assert(row["status"] == "published_tg",
                f"status: {row['status']!r}")
        _assert(row["attempts"] == 0, f"attempts: {row['attempts']}")
        # draft_posts mirrors TG success for admin observability.
        m = c.execute(
            "SELECT tg_channel_message_id, tg_channel_telegra_url, "
            "tg_channel_published_at FROM draft_posts WHERE id=?", (post_id,)
        ).fetchone()
        _assert(m["tg_channel_message_id"] == 12345,
                f"message_id persisted: {m['tg_channel_message_id']!r}")
        _assert("telegra.ph" in (m["tg_channel_telegra_url"] or ""),
                f"telegra_url: {m['tg_channel_telegra_url']!r}")
        _assert(m["tg_channel_published_at"] is not None,
                f"published_at should be set")
    finally:
        c.close()
    print("  PASS  publish_tg happy path → published_tg + draft_posts mirror")


# ----------------------------------------------------------------------
# Sprint Z (DD 2026-07-21 20:59 MSK): #published_tg per-row + per-tick push.
# ----------------------------------------------------------------------
def test_publish_tg_per_row_push_fires_on_published() -> None:
    """Happy path: process_one fires tg_bridge.push_published_tg()
    ONCE with all the new fields (dispatch_id, attempts, latency,
    category/weight from candidates). The previous Sprint Y version
    only fired push_published (WP-side) and a per-tick summary that
    went to the WRONG topic — DD caught it and asked for per-row
    observability in the right topic.
    """
    db_path, conn = make_isolated_db(label="e2e_z_row_ok")
    post_id = _seed_post_and_dispatch(conn, marker="zok-1", dispatch_status="approved")
    # Add category + weight to candidates (would normally be set by
    # rewrite_and_score.py; tests need it explicit for the new SELECT).
    conn.execute(
        "UPDATE candidates SET category='tech', weight=7.4 WHERE id = "
        "(SELECT candidate_id FROM draft_posts WHERE id=?)",
        (post_id,),
    )
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now'), attempts=0 "
        "WHERE post_id=?",
        (post_id,),
    )
    conn.commit()
    conn.close()

    def fake_publish(pid):
        return {
            "message_id": 999,
            "message_url": "https://t.me/your_channel/999",
            "telegra_url": "https://telegra.ph/zok-1",
            "blocked": False,
        }

    per_row_calls: list[dict] = []
    summary_calls: list[dict] = []

    def fake_push_row(**kwargs):
        per_row_calls.append(kwargs)

    def fake_push_summary(**kwargs):
        summary_calls.append(kwargs)

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch("tg_channel_publisher.publish", side_effect=fake_publish), \
             patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5), \
             patch("tg_bridge.push_published_tg", side_effect=fake_push_row), \
             patch("tg_bridge.push_published_tg_summary", side_effect=fake_push_summary):
            import publish_tg
            counts = publish_tg.run(limit=10)

    _assert(counts["published"] == 1, f"counts: {counts!r}")
    _assert(len(per_row_calls) == 1,
            f"per-row push must fire once per row: {len(per_row_calls)}")
    row = per_row_calls[0]
    _assert(row["outcome"] == "published",
            f"outcome: {row['outcome']!r}")
    _assert(row["dispatch_id"] >= 1, f"dispatch_id: {row['dispatch_id']!r}")
    _assert(row["post_id"] == post_id, f"post_id: {row['post_id']!r}")
    _assert(row["category"] == "tech", f"category: {row['category']!r}")
    _assert(abs(row["weight"] - 7.4) < 0.01, f"weight: {row['weight']!r}")
    _assert(row["max_attempts"] == 5, f"max_attempts: {row['max_attempts']!r}")
    _assert(row["attempts"] == 0, f"attempts: {row['attempts']!r}")
    _assert(row["tg_message_url"] == "https://t.me/your_channel/999",
            f"tg_message_url: {row['tg_message_url']!r}")
    _assert(row["telegra_url"] == "https://telegra.ph/zok-1",
            f"telegra_url: {row['telegra_url']!r}")
    _assert(row["latency_seconds"] is not None
            and row["latency_seconds"] >= 0,
            f"latency: {row['latency_seconds']!r}")
    # Summary fires too (we processed a row).
    _assert(len(summary_calls) == 1, f"summary must fire: {len(summary_calls)}")
    sm = summary_calls[0]
    _assert(sm["counts"]["published"] == 1, f"summary counts: {sm['counts']!r}")
    _assert(sm["runtime_seconds"] >= 0, f"runtime: {sm['runtime_seconds']!r}")
    _assert(sm["queue_remaining"] == 0, f"queue after happy: {sm['queue_remaining']!r}")
    print("  PASS  publish_tg: per-row push on published with all fields")


def test_publish_tg_per_row_push_includes_failed_reason_on_retry() -> None:
    """Telegraph failure → per-row push fires with outcome='blocked_retry',
    attempts=N+1 (after the UPDATE), and failed_reason populated. DD
    specifically asked for the failure reason to be visible — previously
    it lived only in DB and only the first character of the count was
    shown in the summary.
    """
    db_path, conn = make_isolated_db(label="e2e_z_row_retry")
    post_id = _seed_post_and_dispatch(conn, marker="zretry-1", dispatch_status="approved")
    # 'gaming' is NOT in the Sprint 6.7 category whitelist — use 'tech'.
    conn.execute(
        "UPDATE candidates SET category='tech', weight=6.8 WHERE id = "
        "(SELECT candidate_id FROM draft_posts WHERE id=?)",
        (post_id,),
    )
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now'), attempts=2 "
        "WHERE post_id=?",
        (post_id,),
    )
    conn.commit()
    conn.close()

    def fake_publish(pid):
        raise RuntimeError("telegraph 502 server error")

    per_row_calls: list[dict] = []
    summary_calls: list[dict] = []

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch("tg_channel_publisher.publish", side_effect=fake_publish), \
             patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5), \
             patch("tg_bridge.push_published_tg",
                   side_effect=lambda **kw: per_row_calls.append(kw)), \
             patch("tg_bridge.push_published_tg_summary",
                   side_effect=lambda **kw: summary_calls.append(kw)):
            import publish_tg
            counts = publish_tg.run(limit=10)

    _assert(counts["blocked_retry"] == 1, f"counts: {counts!r}")
    _assert(len(per_row_calls) == 1, f"per-row push count: {len(per_row_calls)}")
    row = per_row_calls[0]
    _assert(row["outcome"] == "blocked_retry",
            f"outcome: {row['outcome']!r}")
    _assert(row["attempts"] == 3,
            f"attempts should be 3 after UPDATE (was 2): {row['attempts']!r}")
    _assert(row["max_attempts"] == 5,
            f"max_attempts: {row['max_attempts']!r}")
    _assert("publish_error" in (row["failed_reason"] or ""),
            f"failed_reason must mention publish_error: {row['failed_reason']!r}")
    _assert("RuntimeError" in (row["failed_reason"] or ""),
            f"failed_reason must mention exception type: {row['failed_reason']!r}")
    # Summary aggregates failed_reason prefix.
    _assert(len(summary_calls) == 1, f"summary must fire: {len(summary_calls)}")
    sm = summary_calls[0]
    _assert("publish_error" in sm["unique_failed_reasons"],
            f"summary must aggregate 'publish_error': {sm['unique_failed_reasons']!r}")
    _assert(sm["unique_failed_reasons"]["publish_error"] == 1,
            f"summary count: {sm['unique_failed_reasons']!r}")
    print("  PASS  publish_tg: per-row push on blocked_retry with failed_reason + attempts (N/M)")


def test_publish_tg_summary_topic_is_published_tg_not_morning_report() -> None:
    """Sprint Y regression test: the per-tick summary used to go to
    topic='morning_report' (DD caught it). Sprint Z fixes the topic to
    'published_tg'. We assert by patching tg_bridge._send and inspecting
    which thread_id was used.
    """
    db_path, conn = make_isolated_db(label="e2e_z_topic")
    post_id = _seed_post_and_dispatch(conn, marker="zt-1", dispatch_status="approved")
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now') WHERE post_id=?",
        (post_id,),
    )
    conn.commit()
    conn.close()

    def fake_publish(pid):
        return {
            "message_id": 1,
            "message_url": "https://t.me/your_channel/1",
            "telegra_url": "https://telegra.ph/zt-1",
            "blocked": False,
        }

    sent_topics: list[str] = []

    def fake_send(topic, text, **kwargs):
        sent_topics.append(topic)
        return {"ok": True, "result": {"message_id": 1}}

    import tg_bridge as _tb

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch("tg_channel_publisher.publish", side_effect=fake_publish), \
             patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5), \
             patch("tg_bridge._send", side_effect=fake_send), \
             patch("tg_bridge.push_published_tg") as _row, \
             patch("tg_bridge.push_published_tg_summary") as _sum:
            # Force both per-row and per-tick pushes through _send so
            # we observe their topics. (push_published_tg internally
            # calls _send("published_tg", ...); we delegate here.)
            def _real_row(**kw):
                _tb._send("published_tg", "row")
            _row.side_effect = _real_row
            def _real_summary(**kw):
                _tb._send("published_tg", "summary")
            _sum.side_effect = _real_summary
            import publish_tg
            counts = publish_tg.run(limit=10)

    _assert(counts["published"] == 1, f"counts: {counts!r}")
    # Both per-row push and per-tick summary must use topic='published_tg'.
    # Sprint Y regression: the OLD code did _send('morning_report', ...).
    _assert(all(t == "published_tg" for t in sent_topics),
            f"all pushes must target 'published_tg', got: {sent_topics!r}")
    _assert(len(sent_topics) >= 2,
            f"expected per-row + summary sends: {sent_topics!r}")
    print("  PASS  publish_tg: per-row + per-tick summary target 'published_tg' (was 'morning_report' bug)")


def test_publish_tg_summary_includes_queue_remaining_and_runtime() -> None:
    """Summary message must include runtime, queue_remaining, and the
    iso_timestamp DD requested (so tick latency is observable from the
    admin chat without SSHing to VPS-B).
    """
    db_path, conn = make_isolated_db(label="e2e_z_qrun")
    post_id = _seed_post_and_dispatch(conn, marker="zq-1", dispatch_status="approved")
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now') WHERE post_id=?",
        (post_id,),
    )
    # Seed a second row still in queue so queue_remaining > 0.
    post_id2 = _seed_post_and_dispatch(conn, marker="zq-2", dispatch_status="approved")
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now') WHERE post_id=?",
        (post_id2,),
    )
    conn.commit()
    conn.close()

    def fake_publish(pid):
        return {
            "message_id": 1, "message_url": "x", "telegra_url": "y",
            "blocked": False,
        }

    summary_calls: list[dict] = []

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch("tg_channel_publisher.publish", side_effect=fake_publish), \
             patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5), \
             patch("tg_bridge.push_published_tg"), \
             patch("tg_bridge.push_published_tg_summary",
                   side_effect=lambda **kw: summary_calls.append(kw)):
            import publish_tg
            # limit=1 so we process only one of the two approved rows;
            # the second one should show up in queue_remaining.
            counts = publish_tg.run(limit=1)

    _assert(counts["published"] == 1, f"counts: {counts!r}")
    _assert(len(summary_calls) == 1, f"summary must fire: {len(summary_calls)}")
    sm = summary_calls[0]
    _assert(sm["queue_remaining"] == 1,
            f"queue_remaining must reflect second row still waiting: {sm['queue_remaining']!r}")
    _assert(sm["runtime_seconds"] >= 0, f"runtime: {sm['runtime_seconds']!r}")
    _assert("T" in sm["iso_timestamp"],
            f"iso_timestamp should be ISO-8601: {sm['iso_timestamp']!r}")
    print("  PASS  publish_tg: per-tick summary includes runtime + queue_remaining + iso_timestamp")


def test_publish_tg_summary_includes_unique_failed_reasons_breakdown() -> None:
    """When 3 rows fail with the same 'publish_error: <different message>'
    reason, the summary aggregates them under one bucket ('publish_error')
    so DD doesn't see 3 duplicate lines. Verified the Sprint Z prefix
    classifier — the key is the prefix before ': '.

    Implementation note: _next_approved_row keeps returning the OLDEST
    approved_at first (FIFO) + status='approved' persists after retry,
    so a single run() naturally only processes one row (seen_ids dedup).
    We patch _next_approved_row to cycle through 3 distinct rows so we
    can verify aggregation in a single summary.
    """
    db_path, conn = make_isolated_db(label="e2e_z_reasons")
    # Seed 3 approved rows that will all fail.
    post_ids = []
    for i in (1, 2, 3):
        pid = _seed_post_and_dispatch(conn, marker=f"zr-{i}", dispatch_status="approved")
        conn.execute(
            "UPDATE tg_dispatch SET approved_at = datetime('now') WHERE post_id=?",
            (pid,),
        )
        post_ids.append(pid)
    conn.commit()
    conn.close()

    # Pre-build the rows that _next_approved_row will cycle through.
    rows: list[sqlite3.Row] = []
    for pid in post_ids:
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        r = c.execute(
            "SELECT d.id, d.post_id, d.tg_title, d.attempts, d.approved_at, "
            "d.failed_reason, d.tg_message_url, d.telegraph_url, "
            "wp.wp_post_url, c.category, c.weight FROM tg_dispatch d "
            "JOIN draft_posts wp ON wp.id=d.post_id "
            "JOIN candidates c ON c.id=wp.candidate_id WHERE d.post_id=?",
            (pid,),
        ).fetchone()
        c.close()
        rows.append(r)
    iter_rows = iter(rows)

    def fake_next_row(_conn):
        try:
            return next(iter_rows)
        except StopIteration:
            return None

    def fake_publish(pid):
        raise RuntimeError(f"telegraph 502 (variant {pid})")

    summary_calls: list[dict] = []

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch("publish_tg._next_approved_row", side_effect=fake_next_row), \
             patch("tg_channel_publisher.publish", side_effect=fake_publish), \
             patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5), \
             patch("tg_bridge.push_published_tg"), \
             patch("tg_bridge.push_published_tg_summary",
                   side_effect=lambda **kw: summary_calls.append(kw)):
            import publish_tg
            counts = publish_tg.run(limit=10)

    _assert(counts["blocked_retry"] == 3, f"counts: {counts!r}")
    _assert(len(summary_calls) == 1, f"summary must fire: {len(summary_calls)}")
    sm = summary_calls[0]
    # 3 different RuntimeError messages → 3 different full reasons, but
    # all share the 'publish_error: ' prefix → 1 aggregated bucket.
    _assert(len(sm["unique_failed_reasons"]) == 1,
            f"unique reasons should collapse under prefix: {sm['unique_failed_reasons']!r}")
    _assert(sm["unique_failed_reasons"]["publish_error"] == 3,
            f"publish_error count: {sm['unique_failed_reasons']!r}")
    print("  PASS  publish_tg: summary aggregates unique_failed_reasons by prefix")


def test_publish_tg_summary_silent_when_idle_and_empty_queue() -> None:
    """No rows processed AND queue_remaining=0 → no summary message.
    Saves the admin topic from spam during quiet hours. Sprint Z kept
    the Sprint Y 'stay silent on idle' contract but added the
    'queue_remaining > 0' exception (so DD sees backlogs even when
    no rows were processed in this tick).
    """
    db_path, conn = make_isolated_db(label="e2e_z_idle")
    conn.close()  # no rows at all

    summary_calls: list[dict] = []

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5), \
             patch("tg_bridge.push_published_tg"), \
             patch("tg_bridge.push_published_tg_summary",
                   side_effect=lambda **kw: summary_calls.append(kw)):
            import publish_tg
            counts = publish_tg.run(limit=10)

    _assert(counts == {"published": 0, "blocked_retry": 0,
                       "blocked_exhausted": 0, "expired_skipped": 0,
                       "config_error": 0},
            f"counts: {counts!r}")
    _assert(len(summary_calls) == 0,
            f"summary must NOT fire on idle+empty: {len(summary_calls)}")
    print("  PASS  publish_tg: summary silent when idle and queue empty")


def test_publish_tg_summary_fires_when_queue_has_backlog() -> None:
    """queue_remaining > 0 even with no rows processed → summary fires.
    Sprint Z added this so DD can spot a stuck backlog from the admin
    chat. (e.g. if Telegraph is down, the queue grows but the tick
    processes 0 rows — the summary should still tell DD about it.)
    """
    db_path, conn = make_isolated_db(label="e2e_z_backlog")
    # Seed 2 approved rows but DON'T set approved_at — without
    # approved_at the row still appears in the SELECT (it filters only
    # on expires_at + attempts<MAX). Actually it does require status
    # but approved_at isn't in WHERE. Let me re-check...
    # _next_approved_row SELECT: WHERE d.status='approved' AND d.attempts<?
    # AND (expires_at IS NULL OR expires_at > NOW()). approved_at NOT in
    # WHERE — but we want the row to be picked up so process_one runs.
    post_id = _seed_post_and_dispatch(conn, marker="zb-1", dispatch_status="approved")
    conn.execute(
        "UPDATE candidates SET expires_at=NULL WHERE id = "
        "(SELECT candidate_id FROM draft_posts WHERE id=?)",
        (post_id,),
    )
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now') WHERE post_id=?",
        (post_id,),
    )
    # Second row still in queue (no process_one will pick it because
    # of seen_ids set + limit=1).
    post_id2 = _seed_post_and_dispatch(conn, marker="zb-2", dispatch_status="approved")
    conn.execute(
        "UPDATE candidates SET expires_at=NULL WHERE id = "
        "(SELECT candidate_id FROM draft_posts WHERE id=?)",
        (post_id2,),
    )
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now') WHERE post_id=?",
        (post_id2,),
    )
    conn.commit()
    conn.close()

    def fake_publish(pid):
        return {"message_id": 1, "message_url": "x",
                "telegra_url": "y", "blocked": False}

    summary_calls: list[dict] = []

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch("tg_channel_publisher.publish", side_effect=fake_publish), \
             patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5), \
             patch("tg_bridge.push_published_tg"), \
             patch("tg_bridge.push_published_tg_summary",
                   side_effect=lambda **kw: summary_calls.append(kw)):
            import publish_tg
            # limit=1 so we process only one of the two approved rows;
            # the second one should show up in queue_remaining.
            counts = publish_tg.run(limit=1)

    # Only 1 row processed due to limit=1; 1 row remains in queue.
    _assert(counts["published"] == 1, f"counts: {counts!r}")
    _assert(len(summary_calls) == 1, f"summary must fire (queue>0): {len(summary_calls)}")
    _assert(summary_calls[0]["queue_remaining"] == 1,
            f"queue_remaining: {summary_calls[0]['queue_remaining']!r}")
    print("  PASS  publish_tg: summary fires when queue has backlog (even if idle)")


def test_publish_tg_expired_skipped_fires_per_row_push() -> None:
    """Half-life expired → per-row push fires with outcome='expired_skipped'
    and the defensive half-life reason. The pre-fetch filter (the SELECT
    in _next_approved_row) is the primary path, but if a row races past
    it the defensive check inside process_one still surfaces to the
    admin via the same per-row push contract.

    Implementation note: the SELECT filter would normally exclude the
    expired row BEFORE process_one runs. We bypass it by patching
    _next_approved_row to return the expired row anyway — simulating the
    rare race where expires_at ticked over mid-tick.
    """
    db_path, conn = make_isolated_db(label="e2e_z_expired")
    post_id = _seed_post_and_dispatch(
        conn, marker="zexp-1",
        expires_at="2000-01-01 00:00:00",  # past
        dispatch_status="approved",
    )
    conn.execute(
        "UPDATE candidates SET category='politics', weight=5.0 WHERE id = "
        "(SELECT candidate_id FROM draft_posts WHERE id=?)",
        (post_id,),
    )
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now') WHERE post_id=?",
        (post_id,),
    )
    conn.commit()
    conn.close()

    def fake_next_row(_conn):
        # Force return the expired row (simulate the rare race where
        # expires_at ticked over between _next_approved_row and the
        # defensive re-check inside process_one).
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT d.id, d.post_id, d.tg_title, d.attempts, d.approved_at, "
            "d.failed_reason, d.tg_message_url, d.telegraph_url, "
            "wp.wp_post_url, c.category, c.weight FROM tg_dispatch d "
            "JOIN draft_posts wp ON wp.id=d.post_id "
            "JOIN candidates c ON c.id=wp.candidate_id WHERE d.post_id=?",
            (post_id,),
        ).fetchone()
        c.close()
        return row

    per_row_calls: list[dict] = []

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch("publish_tg._next_approved_row", side_effect=fake_next_row), \
             patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5), \
             patch("tg_bridge.push_published_tg",
                   side_effect=lambda **kw: per_row_calls.append(kw)), \
             patch("tg_bridge.push_published_tg_summary"):
            import publish_tg
            counts = publish_tg.run(limit=10)

    _assert(counts["expired_skipped"] == 1, f"counts: {counts!r}")
    _assert(len(per_row_calls) == 1, f"per-row push count: {len(per_row_calls)}")
    row = per_row_calls[0]
    _assert(row["outcome"] == "expired_skipped",
            f"outcome: {row['outcome']!r}")
    _assert("half_life_expired" in (row["failed_reason"] or ""),
            f"failed_reason: {row['failed_reason']!r}")
    print("  PASS  publish_tg: defensive expired_skipped → per-row push fires")


def test_publish_tg_config_error_fires_per_row_push() -> None:
    """TGChannelConfigError (missing env vars) → per-row push with
    outcome='config_error' and the actual error in failed_reason.
    Sprint Z: same per-row contract as other errors so DD doesn't need
    to ssh to VPS-B to see what's misconfigured.
    """
    db_path, conn = make_isolated_db(label="e2e_z_cfg")
    post_id = _seed_post_and_dispatch(conn, marker="zcfg-1", dispatch_status="approved")
    # 'misc' is NOT in the Sprint 6.7 whitelist — use 'other'.
    conn.execute(
        "UPDATE candidates SET category='other', weight=4.0 WHERE id = "
        "(SELECT candidate_id FROM draft_posts WHERE id=?)",
        (post_id,),
    )
    conn.execute(
        "UPDATE tg_dispatch SET approved_at = datetime('now') WHERE post_id=?",
        (post_id,),
    )
    conn.commit()
    conn.close()

    def fake_publish(pid):
        # Inject the TGChannelConfigError exception from tg_channel_publisher.
        import tg_channel_publisher
        raise tg_channel_publisher.TGChannelConfigError(
            "TG.your_channel_channel_id is empty"
        )

    per_row_calls: list[dict] = []
    summary_calls: list[dict] = []

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch("tg_channel_publisher.publish", side_effect=fake_publish), \
             patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5), \
             patch("tg_bridge.push_published_tg",
                   side_effect=lambda **kw: per_row_calls.append(kw)), \
             patch("tg_bridge.push_published_tg_summary",
                   side_effect=lambda **kw: summary_calls.append(kw)):
            import publish_tg
            counts = publish_tg.run(limit=10)

    _assert(counts["config_error"] == 1, f"counts: {counts!r}")
    _assert(len(per_row_calls) == 1, f"per-row push count: {len(per_row_calls)}")
    row = per_row_calls[0]
    _assert(row["outcome"] == "config_error",
            f"outcome: {row['outcome']!r}")
    _assert("config_error" in (row["failed_reason"] or ""),
            f"failed_reason: {row['failed_reason']!r}")
    _assert("your_channel_channel_id" in (row["failed_reason"] or ""),
            f"failed_reason must include real config message: {row['failed_reason']!r}")
    _assert(summary_calls[0]["unique_failed_reasons"].get("config_error") == 1,
            f"summary aggregates config_error: {summary_calls[0]['unique_failed_reasons']!r}")
    print("  PASS  publish_tg: TGChannelConfigError → per-row push + summary bucket")


# ----------------------------------------------------------------------
# Stage 2 (generate_for_tg) — half-life + LLM regen
# ----------------------------------------------------------------------
def test_generate_for_tg_skips_expired_pre_llm() -> None:
    """expires_at < NOW() pre-LLM call → row stays pending_tg_text, no LLM call."""
    db_path, conn = make_isolated_db(label="e2e_gen_hl")
    post_id = _seed_post_and_dispatch(
        conn, marker="gen-hl-1", expires_at="2000-01-01 00:00:00",
    )
    conn.close()

    regen_calls = []
    def fake_regen(pid, **kw):
        regen_calls.append(pid)
        return {"post_id": pid, "tg_draft_id": 99, "blocked": False,
                "tg_title": "T", "tg_teaser": "s", "tg_hashtags": [],
                "prompt_version": "v1.0", "note": None}

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        # AUTOAPPROVE off so we can clearly observe the state transition.
        with patch.object(PIPE_TICKS, "tg_autoapprove_tg_publish", False), \
             patch("tg_regenerate.tg_regenerate", side_effect=fake_regen):
            import generate_for_tg
            counts = generate_for_tg.run(limit=10)

    _assert(regen_calls == [],
            f"LLM must NOT be called for expired news: {regen_calls!r}")
    # When expired at fetch time, _next_pending_row returns None —
    # so counts remain zero and the row stays pending_tg_text (unchanged).
    _assert(counts["ok"] == 0 and counts["expired_skipped"] == 0,
            f"counts: {counts!r}")

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        row = c.execute(
            "SELECT status FROM tg_dispatch WHERE post_id=?", (post_id,)
        ).fetchone()
        _assert(row["status"] == "pending_tg_text",
                f"expired row should stay pending_tg_text: {row['status']!r}")
    finally:
        c.close()
    print("  PASS  generate_for_tg: half-life pre-LLM → no regen, row untouched")


def test_generate_for_tg_happy_manual_approval() -> None:
    """Mocked LLM, TG_AUTOAPPROVE=0 → new row with status='awaiting_approval'.

    Sprint Y.1 hotfix (DD 2026-07-21 08:05): the consumed
    pending_tg_text row is marked expired_skipped to prevent the
    infinite-regen loop (96 duplicate rows for post_id=169). The
    new row carries the awaiting_approval status for /approve_tg.
    """
    db_path, conn = make_isolated_db(label="e2e_gen_man")
    post_id = _seed_post_and_dispatch(conn, marker="gen-man-1")
    # Reuse the same connection inside fake_regen (avoids SQLite 'database is
    # locked' when generate_for_tg's SELECT contends with a separate INSERT).
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # allow concurrent reader+writer
    conn.commit()

    # Stub save_tg_dispatch so we know exactly what the second row is.
    def fake_regen(pid, **kw):
        from tg_regenerate import save_tg_dispatch
        new_id = save_tg_dispatch(
            conn, pid,
            tg_title="🔴 Manual-mode title",
            tg_teaser="Manual-mode teaser.",
            tg_hashtags=["ai", "test"],
            note=kw.get("note"),
        )
        conn.commit()  # release write tx so generate_for_tg.run()'s UPDATE can proceed
        return {"post_id": pid, "tg_draft_id": new_id, "blocked": False,
                "tg_title": "🔴 Manual-mode title",
                "tg_teaser": "Manual-mode teaser.",
                "tg_hashtags": ["ai", "test"],
                "prompt_version": "v1.0", "note": kw.get("note")}

    try:
        with _patch_pipe_db(db_path):
            from config import PIPE_TICKS
            with patch.object(PIPE_TICKS, "tg_autoapprove_tg_publish", False), \
                 patch("tg_regenerate.tg_regenerate", side_effect=fake_regen):
                import generate_for_tg
                counts = generate_for_tg.run(limit=10)
    finally:
        conn.close()

    _assert(counts["ok"] == 1, f"counts: {counts!r}")

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            "SELECT id, status, failed_reason FROM tg_dispatch WHERE post_id=? ORDER BY id",
            (post_id,),
        ).fetchall()
        statuses = [r["status"] for r in rows]
        _assert(statuses.count("awaiting_approval") == 1,
                f"expected exactly 1 awaiting_approval row, got {statuses!r}")
        # Sprint Y.1: the consumed pending_tg_text row is now expired_skipped
        # (failed_reason='superseded_by_regen'), NOT still pending_tg_text.
        expired = [r for r in rows if r["status"] == "expired_skipped"
                   and (r["failed_reason"] or "").startswith("superseded_by_regen")]
        _assert(len(expired) == 1,
                f"expected exactly 1 superseded-by-regen row, got {statuses!r}")
    finally:
        c.close()
    print("  PASS  generate_for_tg: TG_AUTOAPPROVE=0 → awaiting_approval (old row consumed)")


def test_generate_for_tg_does_not_repick_consumed_row() -> None:
    """Sprint Y.1 regression test: running tick=generate_for_tg twice
    must NOT create duplicate awaiting_approval rows for the same post.

    Without the consumed-mark, a second tick would re-select the original
    pending_tg_text row and create a SECOND awaiting_approval row (the
    96-row symptom on 2026-07-21).
    """
    db_path, conn = make_isolated_db(label="e2e_gen_dedup")
    post_id = _seed_post_and_dispatch(conn, marker="gen-dedup-1")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()

    def fake_regen(pid, **kw):
        from tg_regenerate import save_tg_dispatch
        new_id = save_tg_dispatch(
            conn, pid,
            tg_title="🔴 Dedup title",
            tg_teaser="Dedup teaser.",
            tg_hashtags=[],
            note=kw.get("note"),
        )
        conn.commit()
        return {"post_id": pid, "tg_draft_id": new_id, "blocked": False,
                "tg_title": "🔴 Dedup title", "tg_teaser": "Dedup teaser.",
                "tg_hashtags": [], "prompt_version": "v1.0", "note": kw.get("note")}

    try:
        with _patch_pipe_db(db_path):
            from config import PIPE_TICKS
            with patch.object(PIPE_TICKS, "tg_autoapprove_tg_publish", False), \
                 patch("tg_regenerate.tg_regenerate", side_effect=fake_regen):
                import generate_for_tg
                counts_1 = generate_for_tg.run(limit=10)
                counts_2 = generate_for_tg.run(limit=10)  # second tick — should be no-op
    finally:
        conn.close()

    _assert(counts_1["ok"] == 1, f"first run counts: {counts_1!r}")
    _assert(counts_2["ok"] == 0, f"second run should be no-op, got {counts_2!r}")

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            "SELECT id, status FROM tg_dispatch WHERE post_id=? ORDER BY id",
            (post_id,),
        ).fetchall()
        statuses = [r["status"] for r in rows]
        # Exactly one awaiting_approval (from the first run) + one expired_skipped
        # (the original pending_tg_text row consumed by the first run).
        # Second run found nothing pending, so no second row was created.
        _assert(statuses.count("awaiting_approval") == 1,
                f"expected 1 awaiting_approval row, got {statuses!r}")
        _assert(statuses.count("expired_skipped") == 1,
                f"expected 1 expired_skipped row (consumed), got {statuses!r}")
    finally:
        c.close()
    print("  PASS  generate_for_tg: second tick is no-op (dedup via consumed mark)")


def test_generate_for_tg_happy_auto_approval() -> None:
    """Mocked LLM, TG_AUTOAPPROVE=1 → new row jumps to 'approved'."""
    db_path, conn = make_isolated_db(label="e2e_gen_auto")
    post_id = _seed_post_and_dispatch(conn, marker="gen-auto-1")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()

    def fake_regen(pid, **kw):
        from tg_regenerate import save_tg_dispatch
        new_id = save_tg_dispatch(
            conn, pid,
            tg_title="🔴 Auto-mode title",
            tg_teaser="Auto-mode teaser.",
            tg_hashtags=["ai"],
            note=kw.get("note"),
        )
        conn.commit()  # release write tx so generate_for_tg.run()'s UPDATE can proceed
        return {"post_id": pid, "tg_draft_id": new_id, "blocked": False,
                "tg_title": "🔴 Auto-mode title",
                "tg_teaser": "Auto-mode teaser.",
                "tg_hashtags": ["ai"], "prompt_version": "v1.0",
                "note": kw.get("note")}

    try:
        with _patch_pipe_db(db_path):
            from config import PIPE_TICKS
            with patch.object(PIPE_TICKS, "tg_autoapprove_tg_publish", True), \
                 patch("tg_regenerate.tg_regenerate", side_effect=fake_regen):
                import generate_for_tg
                counts = generate_for_tg.run(limit=10)
    finally:
        conn.close()

    _assert(counts["ok"] == 1, f"counts: {counts!r}")

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        approved = c.execute(
            "SELECT COUNT(*) FROM tg_dispatch WHERE post_id=? AND status='approved'",
            (post_id,),
        ).fetchone()[0]
        _assert(approved == 1, f"expected 1 approved row, got {approved}")
    finally:
        c.close()
    print("  PASS  generate_for_tg: TG_AUTOAPPROVE=1 → approved (auto)")


# ----------------------------------------------------------------------
# FULL CHAIN: WP mocked → generate_for_tg → publish_tg
# ----------------------------------------------------------------------
def test_full_chain_wp_to_published_tg() -> None:
    """End-to-end walk through all three ticks, with every external
    dependency mocked. Verifies the state machine for a single post:
      pending_tg_text
      → text_generated → approved  (TG_AUTOAPPROVE=1)
      → published_tg

    No real WordPress / OpenAI / Telegraph / Telegram API hits.
    """
    db_path, conn = make_isolated_db(label="e2e_full_chain")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    try:
        # --- Stage 0: pre-seed a candidate that publisher.process_one would pick up.
        source_id = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO candidates (source_id, guid, url, title, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_id, "full-chain-1", "https://example.com/full-chain-1",
             "Full chain test", "Body"),
        )
        candidate_id = conn.execute(
            "SELECT id FROM candidates WHERE guid='full-chain-1'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO draft_posts (candidate_id, title, slug, status, "
            "wp_post_url, image_alt, telegram_teaser) "
            "VALUES (?, ?, ?, 'published', ?, ?, ?)",
            (candidate_id, "Full chain test", "full-chain-test",
             "https://media-<deploy-user>.local/full-chain-test", "alt text", "teaser"),
        )
        post_id = conn.execute(
            "SELECT id FROM draft_posts WHERE slug='full-chain-test'"
        ).fetchone()[0]
        # Insert the Stage-1 outcome directly: publisher.process_one's WP
        # dance is owned by test_publisher_smoke; this e2e focuses on the
        # state machine AROUND telegram (DD's request).
        conn.execute(
            "INSERT INTO tg_dispatch (post_id, status, prompt_version, note) "
            "VALUES (?, 'pending_tg_text', 'master_prompt_tg.md@v1.0', NULL)",
            (post_id,),
        )
        conn.commit()

        disp = conn.execute(
            "SELECT status FROM tg_dispatch WHERE post_id=? ORDER BY id",
            (post_id,),
        ).fetchall()
        _assert(any(r["status"] == "pending_tg_text" for r in disp),
                f"Stage 1 should leave a pending_tg_text row: "
                f"{[r['status'] for r in disp]!r}")
    except Exception:
        conn.close()
        raise

    # --- Stage 2 (generate_for_tg): mock LLM, AUTOAPPROVE=1.
    def fake_regen(pid, **kw):
        from tg_regenerate import save_tg_dispatch
        new_id = save_tg_dispatch(
            conn, pid,  # reuse the same conn to avoid SQLite 'database is locked'
            tg_title="🔴 Live chain title", tg_teaser="Live chain teaser.",
            tg_hashtags=["chain"], note=kw.get("note"),
        )
        conn.commit()  # release write tx so generate_for_tg.run()'s UPDATE can proceed
        return {"post_id": pid, "tg_draft_id": new_id, "blocked": False,
                "tg_title": "🔴 Live chain title", "tg_teaser": "Live chain teaser.",
                "tg_hashtags": ["chain"], "prompt_version": "v1.0",
                "note": kw.get("note")}

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch.object(PIPE_TICKS, "tg_autoapprove_tg_publish", True), \
             patch("tg_regenerate.tg_regenerate", side_effect=fake_regen):
            import generate_for_tg
            counts = generate_for_tg.run(limit=10)

    _assert(counts["ok"] == 1, f"Stage 2 counts: {counts!r}")

    # --- Stage 3 (publish_tg): mock Telegraph+TG channel.
    def fake_publish_tg(pid):
        return {
            "message_id": 4242,
            "message_url": "https://t.me/your_channel/4242",
            "telegra_url": "https://telegra.ph/full-chain-07-20",
            "blocked": False,
        }

    with _patch_pipe_db(db_path):
        from config import PIPE_TICKS
        with patch.object(PIPE_TICKS, "tg_max_telegraph_attempts", 5), \
             patch("tg_channel_publisher.publish", side_effect=fake_publish_tg):
            import publish_tg
            counts = publish_tg.run(limit=10)

    _assert(counts["published"] == 1, f"Stage 3 counts: {counts!r}")

    # --- Final assertions on the full state machine outcome (reuse conn).
    all_rows = conn.execute(
        "SELECT id, status FROM tg_dispatch WHERE post_id=? ORDER BY id",
        (post_id,),
    ).fetchall()
    conn.close()
    statuses = [r["status"] for r in all_rows]
    _assert("published_tg" in statuses,
            f"final state should include published_tg: {statuses!r}")
    # Sprint Y.1 (DD 2026-07-21 08:05): the original pending_tg_text row
    # is marked 'expired_skipped' (failed_reason='superseded_by_regen') by
    # tick=generate_for_tg so it isn't re-picked next tick. The NEW row
    # walks the pipeline 'text_generated' → 'approved' → 'published_tg'.
    _assert(any(s == "expired_skipped" for s in statuses),
            f"original pending_tg_text should be consumed (expired_skipped): {statuses!r}")
    # Count the rows that walked the pipeline: should be exactly 1
    # 'published_tg' (not 2 or more, not 0).
    published_count = statuses.count("published_tg")
    _assert(published_count == 1,
            f"expected exactly 1 published_tg row, got {published_count}: {statuses!r}")

    final = sqlite3.connect(str(db_path))
    final.row_factory = sqlite3.Row
    try:
        fin = final.execute(
            "SELECT tg_channel_message_id, tg_channel_telegra_url "
            "FROM draft_posts WHERE id=?",
            (post_id,),
        ).fetchone()
        _assert(fin["tg_channel_message_id"] == 4242,
                f"message_id: {fin['tg_channel_message_id']!r}")
        _assert("telegra.ph" in (fin["tg_channel_telegra_url"] or ""),
                f"telegra_url: {fin['tg_channel_telegra_url']!r}")
    finally:
        final.close()
    print(f"  PASS  full chain WP→TG: pending_tg_text → approved → published_tg "
          f"(TG-channel mock message_id=4242)")


# ----------------------------------------------------------------------
def main() -> int:
    tests = [
        test_publish_tg_no_op_when_nothing_approved,
        test_publish_tg_half_life_at_publish_filtered_out,
        test_publish_tg_failure_retries_within_limit,
        test_publish_tg_attempts_exhausted_marks_blocked_exhausted,
        test_publish_tg_happy_path_marks_published_tg,
        # Sprint Z (DD 2026-07-21 20:59 MSK): per-row + per-tick push
        test_publish_tg_per_row_push_fires_on_published,
        test_publish_tg_per_row_push_includes_failed_reason_on_retry,
        test_publish_tg_summary_topic_is_published_tg_not_morning_report,
        test_publish_tg_summary_includes_queue_remaining_and_runtime,
        test_publish_tg_summary_includes_unique_failed_reasons_breakdown,
        test_publish_tg_summary_silent_when_idle_and_empty_queue,
        test_publish_tg_summary_fires_when_queue_has_backlog,
        test_publish_tg_expired_skipped_fires_per_row_push,
        test_publish_tg_config_error_fires_per_row_push,
        test_generate_for_tg_skips_expired_pre_llm,
        test_generate_for_tg_happy_manual_approval,
        test_generate_for_tg_happy_auto_approval,
        test_generate_for_tg_does_not_repick_consumed_row,
        test_full_chain_wp_to_published_tg,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
    total = len(tests)
    print(f"\n{total - failed}/{total} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
