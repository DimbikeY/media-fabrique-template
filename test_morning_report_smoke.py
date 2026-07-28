"""Smoke tests for morning_report.py (Sprint 6.5.2).

Background: the 6.5 cron tick (media<deploy-user>-morning-report) used to swallow
TG push failures — `tg_bridge` returns None on retry exhaustion, and
morning_report.py exited 0 anyway. On 2026-07-09 morning the cron
finished "ok" but neither #morning-report nor #ideas got the message
(network: api.telegram.org unreachable without VPN). DD noticed only
because he opened the chat and saw nothing. The fix:

  * main() returns 2 when BOTH pushes returned None.
  * cron failureAlert.after is dropped from 3 to 1 for this job.

These tests verify the exit-code contract. We patch tg_bridge._send
so we don't need a real bot / VPN — dry-mode is for the printed path,
not for the exit-code path.

Run: .venv/bin/python test_morning_report_smoke.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import _smoke_lib


def _force_dry_mode() -> None:
    """Empty env BEFORE importing config so TG.* is empty.

    In dry-mode, tg_bridge._send returns None without any HTTP call —
    that's exactly the "failed push" signal we want to test for exit 2.
    For the partial-success / full-success cases we monkeypatch _send
    explicitly so dry-mode alone isn't enough.
    """
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TG_CHAT_ID",
        "TG_THREAD_PUBLISHED",
        "TG_THREAD_FEEDBACK",
        "TG_THREAD_MORNING_REPORT",
        # "TG_THREAD_IDEAS" removed 2026-07-20 09:26 MSK
    ):
        os.environ[key] = ""
    for mod in ("config", "tg_bridge", "morning_report"):
        sys.modules.pop(mod, None)


def _seed_minimum_rows(db_path: Path) -> None:
    """Insert one ready item + one published post so build_report()
    produces non-empty output. Counts themselves don't matter for the
    exit-code contract — we only care that build_report() didn't raise
    and that main() reaches the push_* lines."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT INTO sources(name, feed_url, kind, enabled)
           VALUES (?, ?, 'rss', 1)""",
        ("smoke-mr", "https://example.com/mr.xml"),
    )
    conn.execute(
        """INSERT INTO candidates(source_id, guid, url, title,
                             published_at, status, safety_status,
                             category, base_score, weight)
           VALUES (1, 'mr-guid-1', 'https://example.com/x',
                   'Smoke MR', datetime('now','-1h'),
                   'ready', 'review', 'tech', 7.5, 7.0)"""
    )
    conn.execute(
        """INSERT INTO draft_posts(candidate_id, title, slug, status, wp_post_id,
                             wp_post_url, created_at, updated_at)
           VALUES (1, 'Smoke MR', 'smoke-mr', 'published', 1,
                   'https://wp.example/p/mr',
                   datetime('now'), datetime('now'))"""
    )
    conn.commit()
    conn.close()


def test_dry_mode_prints_and_exits_zero():
    """--dry prints both blocks and exits 0, never touches tg_bridge."""
    import morning_report
    rc = morning_report.main(["--dry"])
    assert rc == 0, rc
    print("  dry-mode prints + exit 0: OK")


def test_dry_with_isolated_db(db_path: Path):
    """--dry + --db PATH must read ONLY the isolated DB, not prod.

    Regression test for the morning_report anti-pattern where dry-mode
    pulled aggregates from prod even when smoke tests had seeded an
    isolated DB. Now `published: 1` in dry output, not the prod number.
    """
    import io
    import contextlib
    import morning_report
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = morning_report.main(["--dry", "--db", str(db_path)])
    assert rc == 0, rc
    out = buf.getvalue()
    assert "published: 1" in out, (
        f"--dry didn't use isolated DB; got:\n{out}"
    )
    print("  --dry --db uses isolated DB: OK")


def test_both_pushes_failed_returns_2(db_path: Path):
    """Simulate a full TG outage: both _send calls return None.
    main() must exit 2 so OpenClaw cron flips lastRunStatus to error.
    """
    import morning_report
    import tg_bridge
    token = _smoke_lib.patch_db_path(db_path)
    try:
        with mock.patch.object(tg_bridge, "_send", return_value=None):
            rc = morning_report.main([])
        assert rc == 2, f"expected 2 on double-fail, got {rc}"
    finally:
        _smoke_lib.restore_db_path(token)
    print("  both pushes failed -> exit 2: OK")


def test_one_push_succeeded_returns_0(db_path: Path):
    """Partial success (one topic delivered, the other didn't) should
    NOT page DD — at least the user-facing digest arrived. Exit 0.

    DD 2026-07-20 09:26 MSK: morning_report now only pushes one
    topic (#morning-report), so this test verifies the same exit-0
    behaviour with a single push instead of two.
    """
    import morning_report
    import tg_bridge
    token = _smoke_lib.patch_db_path(db_path)
    try:
        with mock.patch.object(
            tg_bridge, "_send",
            side_effect=[{"ok": True}],  # morning_report ok
        ):
            rc = morning_report.main([])
        assert rc == 0, f"expected 0 on partial success, got {rc}"
    finally:
        _smoke_lib.restore_db_path(token)
    print("  one push succeeded -> exit 0: OK")


def test_both_pushes_succeeded_returns_0(db_path: Path):
    """Happy path: both pushes delivered, exit 0."""
    import morning_report
    import tg_bridge
    token = _smoke_lib.patch_db_path(db_path)
    try:
        with mock.patch.object(
            tg_bridge, "_send", return_value={"ok": True}
        ):
            rc = morning_report.main([])
        assert rc == 0, f"expected 0 on full success, got {rc}"
    finally:
        _smoke_lib.restore_db_path(token)
    print("  both pushes succeeded -> exit 0: OK")


def main():
    _force_dry_mode()
    db_path, _conn = _smoke_lib.make_isolated_db(label="morning_report_smoke")
    _seed_minimum_rows(db_path)
    print("== Sprint 6.5.2 morning_report smoke ==")
    test_dry_mode_prints_and_exits_zero()
    test_dry_with_isolated_db(db_path)
    test_both_pushes_failed_returns_2(db_path)
    test_one_push_succeeded_returns_0(db_path)
    test_both_pushes_succeeded_returns_0(db_path)
    print("OK")


if __name__ == "__main__":
    sys.exit(main() or 0)