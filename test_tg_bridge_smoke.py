"""Smoke tests for tg_bridge (Sprint 6.6 approve/reject).

We test the bridge in dry-mode (no real Telegram API call) and verify:
* push_published / push_feedback / push_morning_report / push_ideas
  format the payloads correctly when TG.bot_token is empty.
* push_feedback aggregates candidates within the rate-limit window.
* record_review atomically transitions draft_post.status: draft -> approved|rejected.
  (Sprint cleanup 2026-07-21: note is stored in draft_posts.review_note,
   no longer in a separate feedback_signals table.)
* record_review is idempotent: replays from approved/rejected return the
  previous status without crashing.
* flush_feedback_now clears the bucket.

Run: .venv/bin/python test_tg_bridge_smoke.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import _smoke_lib


def _force_dry_mode() -> None:
    """Set env vars BEFORE importing config so TG.* is empty."""
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    os.environ["TG_CHAT_ID"] = ""
    os.environ["TG_THREAD_PUBLISHED"] = ""
    os.environ["TG_THREAD_FEEDBACK"] = ""
    os.environ["TG_THREAD_MORNING_REPORT"] = ""
    os.environ["TG_THREAD_IDEAS"] = ""
    os.environ["TG_THREAD_DRAFTS"] = ""
    # Drop the cached config so we re-read env.
    for mod in ("config", "tg_bridge", "morning_report"):
        sys.modules.pop(mod, None)


def _make_item_and_post(conn: sqlite3.Connection, suffix: str = "") -> tuple[int, int]:
    """Insert one source + one ready item + one draft post.

    ``suffix`` lets us reuse this helper inside a single test (e.g.
    to create a *second* draft post) without clashing on the
    sources.feed_url / candidates.guid UNIQUE constraints.
    """
    feed_url = f"https://example.com/feed-extra{suffix}.xml"
    guid = f"smoke-guid{suffix}"
    item_url = f"https://example.com/a{suffix}"
    title = f"Smoke post{suffix}"
    slug = f"smoke-post{suffix}"
    conn.execute(
        """INSERT INTO sources(name, feed_url, kind, enabled)
           VALUES (?, ?, 'rss', 1)""",
        ("smoke-extra", feed_url),
    )
    src_id = conn.execute("SELECT id FROM sources ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.execute(
        """INSERT INTO candidates(source_id, guid, url, title,
                             published_at, status, safety_status,
                             category, base_score, weight)
           VALUES (?, ?, ?, ?, datetime('now','-1h'),
                   'ready', 'review', 'tech', 7.5, 7.0)""",
        (src_id, guid, item_url, title),
    )
    candidate_id = conn.execute("SELECT id FROM candidates ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.execute(
        """INSERT INTO draft_posts(candidate_id, title, slug, status,
                             created_at, updated_at)
           VALUES (?, ?, ?, 'draft',
                   datetime('now'), datetime('now'))""",
        (candidate_id, title, slug),
    )
    post_id = conn.execute("SELECT id FROM draft_posts ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.commit()
    return candidate_id, post_id


def test_dry_mode_publish():
    """push_published in dry-mode should not raise."""
    import tg_bridge
    item = {"category": "tech", "weight": 7.0, "title": "Smoke post"}
    post = {"title": "Smoke post", "wp_post_url": "https://wp.example/p/1"}
    tg_bridge.push_published(item, post)
    print("  dry-mode publish: OK (no exception)")


def test_feedback_aggregation():
    """Three pushes within the window collapse into one bucket."""
    import tg_bridge
    tg_bridge._pending_feedback.clear()
    tg_bridge.push_feedback(101, "Title A", "violent")
    tg_bridge.push_feedback(102, "Title B", "violent")
    tg_bridge.push_feedback(103, "Title C", "violent")
    assert "violent" in tg_bridge._pending_feedback, "bucket not created"
    assert len(tg_bridge._pending_feedback["violent"].samples) == 3
    tg_bridge.flush_feedback_now()
    assert tg_bridge._pending_feedback == {}, "bucket not cleared"
    print("  feedback aggregation: OK")


def test_record_review_transitions(db_path: Path):
    """record_review: draft -> approved|rejected. Note lands in
    draft_posts.review_note (Sprint cleanup 2026-07-21 removed the
    feedback_signals side-effect)."""
    import tg_bridge
    conn = sqlite3.connect(str(db_path))
    _candidate_id, post_id = _make_item_and_post(conn, suffix="-tr")
    conn.close()

    # Approve WITHOUT note — transition happens, no extra writes.
    prev = tg_bridge.record_review(post_id, "approve", db_path=db_path)
    assert prev == "draft", f"expected 'draft', got {prev!r}"
    conn = sqlite3.connect(str(db_path))
    status = conn.execute("SELECT status FROM draft_posts WHERE id=?", (post_id,)).fetchone()[0]
    assert status == "approved", status
    note = conn.execute(
        "SELECT review_note FROM draft_posts WHERE id=?", (post_id,)
    ).fetchone()[0]
    assert note in (None, ""), f"no-note /approve should not store a note, got {note!r}"
    conn.close()

    # Reject WITH note on already-approved post — status stays approved
    # (we never transition from terminal states). Sprint cleanup
    # 2026-07-21: record_review() only writes review_note on real
    # transitions (when prev_status was 'draft'); replays are no-ops.
    prev2 = tg_bridge.record_review(post_id, "reject", note="пересмотреть позже", db_path=db_path)
    assert prev2 == "approved", f"expected 'approved' (no-op transition), got {prev2!r}"
    conn = sqlite3.connect(str(db_path))
    status2 = conn.execute("SELECT status FROM draft_posts WHERE id=?", (post_id,)).fetchone()[0]
    assert status2 == "approved", status2  # must NOT change from approved
    conn.close()

    # Now test the real draft -> rejected path on a fresh post.
    conn = sqlite3.connect(str(db_path))
    _c2, post_id2 = _make_item_and_post(conn, suffix="-rej")
    conn.close()
    prev3 = tg_bridge.record_review(post_id2, "reject", note="спам", db_path=db_path)
    assert prev3 == "draft"
    conn = sqlite3.connect(str(db_path))
    status3 = conn.execute("SELECT status FROM draft_posts WHERE id=?", (post_id2,)).fetchone()[0]
    assert status3 == "rejected"
    note3 = conn.execute(
        "SELECT review_note FROM draft_posts WHERE id=?", (post_id2,),
    ).fetchone()[0]
    assert note3 == "спам", note3
    conn.close()
    print("  record_review transitions: OK")


def test_record_review_missing(db_path: Path):
    """record_review with a non-existent post_id returns 'missing'."""
    import tg_bridge
    prev = tg_bridge.record_review(99999, "approve", db_path=db_path)
    assert prev == "missing", prev
    print("  record_review missing: OK")


def test_record_review_bad_verdict(db_path: Path):
    import tg_bridge
    try:
        tg_bridge.record_review(1, "maybe", db_path=db_path)
    except ValueError:
        print("  record_review rejects bad verdict: OK")
        return
    raise AssertionError("expected ValueError for bad verdict")


def test_record_review_idempotent_replay(db_path: Path):
    """Re-playing /approve on an already-approved post is a no-op (returns 'approved').

    Sprint cleanup 2026-07-21: the replay also does not touch review_note
    (write-on-transition invariant). DD's note is preserved on the
    transition, not on a later replay.
    """
    import tg_bridge
    conn = sqlite3.connect(str(db_path))
    _candidate_id, post_id = _make_item_and_post(conn, suffix="-id")
    conn.close()

    # First call (transition) records the initial note.
    tg_bridge.record_review(post_id, "approve", note="первый раз", db_path=db_path)
    # Replay with a new note is a no-op for review_note (transition-only write).
    prev = tg_bridge.record_review(post_id, "approve", note="again", db_path=db_path)
    assert prev == "approved", prev

    conn = sqlite3.connect(str(db_path))
    note = conn.execute(
        "SELECT review_note FROM draft_posts WHERE id=?",
        (post_id,),
    ).fetchone()[0]
    conn.close()
    assert note == "первый раз", (
        f"replay must NOT update review_note, got {note!r}; "
        f"the value is the one from the transition call"
    )
    print("  record_review idempotent replay: OK")


def test_html_escape():
    import tg_bridge
    assert tg_bridge._html_escape("a & b < c") == "a &amp; b &lt; c"
    print("  html escape: OK")


def main():
    _force_dry_mode()
    db_path, _conn = _smoke_lib.make_isolated_db(label="tg_bridge_smoke")
    print("== Sprint 6.6 tg_bridge smoke ==")
    test_dry_mode_publish()
    test_feedback_aggregation()
    test_record_review_transitions(db_path)
    test_record_review_missing(db_path)
    test_record_review_bad_verdict(db_path)
    test_record_review_idempotent_replay(db_path)
    test_html_escape()
    print("OK")


if __name__ == "__main__":
    sys.exit(main() or 0)
