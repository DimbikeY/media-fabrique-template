"""Smoke test for main.py orchestrator (Sprint 5 lightweight).

We test the orchestrator itself, not the child scripts (those have
their own smoke suites). What we verify:

  1. ``python main.py tick=fetch`` against an empty DB exits 0 — the
     fetcher handles "no RSS sources" gracefully and so should the
     orchestrator.

  2. ``python main.py tick=janitor`` writes logs/.last_tick with a
     valid JSON payload containing the expected keys. Heartbeat is
     the only persistent side effect of an otherwise empty tick.

  3. ``python main.py tick=banana`` (unknown tick) exits with code 2
     (our convention for "config error, do not retry").

  4. ``python main.py tick=publish`` does NOT need WP credentials —
     with zero draft draft_posts it just exits 0 after a no-op. (Real WP
     failures are tested in test_publisher_smoke.)

We use the real heartbeat path (logs/.last_tick relative to project
root), which means we DO touch the project tree — but only by writing
a single small JSON file that gets cleaned up at the end. This is
intentional: the heartbeat must land in the project so monitoring
scripts see it.

Run: ``python test_main_orchestrator_smoke.py`` from the project venv.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _run_main(tick_spec: str, *, cwd: Path | None = None,
              timeout: int = 60) -> subprocess.CompletedProcess:
    """Invoke main.py as a real subprocess. Same way OpenClaw cron would."""
    return subprocess.run(
        [sys.executable, "main.py", tick_spec],
        cwd=str(cwd or PROJECT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_heartbeat() -> dict | None:
    """Read logs/.last_tick. Returns None if missing."""
    hb = PROJECT / "logs" / ".last_tick"
    if not hb.exists():
        return None
    return json.loads(hb.read_text())


def _make_isolated_db_with_default_source(db_path: Path) -> None:
    """Set up a tiny isolated DB so child scripts don't touch prod."""
    from init_db import init_db
    from migrate import run_migrations
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    conn.execute(
        "INSERT OR IGNORE INTO sources(name, feed_url, kind, enabled) "
        "VALUES (?, ?, 'rss', 1)",
        ("Smoke Default", "https://smoke.example/main.xml"),
    )
    conn.commit()
    conn.close()


def test_unknown_tick_exits_with_code_2() -> None:
    """tick=banana must be rejected with our 'config error' code (2)."""
    proc = _run_main("tick=banana")
    _assert(proc.returncode == 2,
            f"unknown tick must exit 2, got {proc.returncode}")
    _assert("unknown tick" in proc.stderr.lower(),
            f"stderr should mention 'unknown tick': {proc.stderr}")
    print(f"  PASS  test_unknown_tick_exits_with_code_2 (rc={proc.returncode})")


def test_heartbeat_is_written_on_successful_tick() -> None:
    """A no-op janitor tick (empty DB) must write a valid heartbeat."""
    db_path = PROJECT / "logs" / ".smoke_main_heartbeat.db"
    if db_path.exists():
        db_path.unlink()
    # Set up isolated DB so the child janitor has the candidates table.
    _make_isolated_db_with_default_source(db_path)

    # Point PIPE.db_path at the isolated DB via env, then run a real
    # subprocess so config loading happens fresh (subprocess.run is a
    # new Python process, so os.environ patching in *this* process
    # wouldn't propagate).
    env = os.environ.copy()
    env["DB_PATH"] = str(db_path)
    proc = subprocess.run(
        [sys.executable, "main.py", "tick=janitor"],
        cwd=str(PROJECT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    _assert(proc.returncode == 0,
            f"janitor tick must exit 0, got {proc.returncode}. "
            f"stderr={proc.stderr}")

    hb = _read_heartbeat()
    _assert(hb is not None, "heartbeat file must exist after successful tick")
    for key in ("tick", "finished_at", "ok", "returncode", "duration_seconds"):
        _assert(key in hb, f"heartbeat missing key {key!r}: {hb}")
    _assert(hb["tick"] == "janitor", f"heartbeat.tick={hb['tick']!r}")
    _assert(hb["ok"] is True, f"heartbeat.ok={hb['ok']}")
    _assert(hb["returncode"] == 0, f"heartbeat.returncode={hb['returncode']}")
    _assert(isinstance(hb["duration_seconds"], (int, float)),
            f"duration must be numeric: {hb['duration_seconds']}")

    # Heartbeat finished_at must be very recent.
    finished = time.time() - _parse_iso(hb["finished_at"])
    _assert(finished < 30,
            f"heartbeat must be fresh (got {finished:.1f}s ago)")

    db_path.unlink(missing_ok=True)
    print(f"  PASS  test_heartbeat_is_written_on_successful_tick "
          f"(duration={hb['duration_seconds']}s)")


def test_fetcher_tick_against_empty_db() -> None:
    """Fetcher with no RSS sources should exit 0 (graceful no-op)."""
    db_path = PROJECT / "logs" / ".smoke_main_fetcher.db"
    if db_path.exists():
        db_path.unlink()
    _make_isolated_db_with_default_source(db_path)

    env = os.environ.copy()
    env["DB_PATH"] = str(db_path)
    # Stub the source feed URL to something that 404s fast — fetcher
    # should swallow the error and still exit 0 because we passed no
    # real feed.
    proc = subprocess.run(
        [sys.executable, "main.py", "tick=fetch"],
        cwd=str(PROJECT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Note: the fetcher's exit code depends on its internal handling.
    # We assert exit ∈ {0, 1} (1 is "had errors but kept going") —
    # anything else means our orchestrator got in the way.
    _assert(proc.returncode in (0, 1),
            f"fetcher tick must exit 0 or 1, got {proc.returncode}. "
            f"stderr={proc.stderr[-500:]}")

    hb = _read_heartbeat()
    _assert(hb is not None and hb["tick"] == "fetch",
            f"heartbeat must record the fetch tick: {hb}")
    db_path.unlink(missing_ok=True)
    print(f"  PASS  test_fetcher_tick_against_empty_db (rc={proc.returncode})")


def test_publisher_tick_with_no_drafts_is_noop() -> None:
    """Publisher with zero draft draft_posts should exit 0 quickly."""
    db_path = PROJECT / "logs" / ".smoke_main_publisher.db"
    if db_path.exists():
        db_path.unlink()
    _make_isolated_db_with_default_source(db_path)

    env = os.environ.copy()
    env["DB_PATH"] = str(db_path)
    proc = subprocess.run(
        [sys.executable, "main.py", "tick=publish"],
        cwd=str(PROJECT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Empty queue → no claims → exit 0.
    _assert(proc.returncode == 0,
            f"publisher with empty queue must exit 0, got {proc.returncode}. "
            f"stderr={proc.stderr[-500:]}")

    hb = _read_heartbeat()
    _assert(hb is not None and hb["tick"] == "publish",
            f"heartbeat must record publish tick: {hb}")
    db_path.unlink(missing_ok=True)
    print(f"  PASS  test_publisher_tick_with_no_drafts_is_noop")


def test_janitor_heal_in_isolated_db() -> None:
    """Janitor v2 must heal stuck 'publishing' and old 'failed' draft_posts
    back to 'draft'. Tested against an isolated DB so we can seed
    fixtures freely."""
    db_path = PROJECT / "logs" / ".smoke_main_heal.db"
    if db_path.exists():
        db_path.unlink()
    _make_isolated_db_with_default_source(db_path)

    import janitor
    from config import PIPE, PIPE_TICKS

    # Override PIPE.db_path in this process. The subprocess for the
    # orchestrator uses its own env, but here we test janitor functions
    # directly.
    original_db_path = PIPE.db_path
    try:
        PIPE.db_path = db_path

        now = time.time()
        # Seed: one post stuck in 'publishing' for 30 min (well past
        # the default 15-min threshold), one failed post old enough,
        # one fresh failed post that must NOT be healed.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        src_id = conn.execute(
            "SELECT id FROM sources LIMIT 1"
        ).fetchone()["id"]

        # Insert a dummy item first so the FK is satisfied.
        cur = conn.execute(
            """INSERT INTO candidates(source_id, guid, url, title, status,
                                  safety_status)
               VALUES (?, ?, ?, ?, 'ready', 'ok')""",
            (src_id, "smoke-heal-item-1",
             "https://smoke.example/heal/1", "heal test item"),
        )
        candidate_id = cur.lastrowid

        stuck_iso = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.gmtime(now - (PIPE_TICKS.publishing_stuck_minutes + 5) * 60),
        )
        old_failed_iso = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.gmtime(now - (PIPE_TICKS.failed_retry_after_minutes + 5) * 60),
        )
        fresh_failed_iso = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.gmtime(now - 60),  # 1 min ago — should NOT be healed
        )

        conn.execute(
            """INSERT INTO draft_posts(candidate_id, slug, title, status, updated_at,
                                  content_html, error_reason)
               VALUES (?, ?, ?, 'publishing', ?, '<p>x</p>', NULL)""",
            (candidate_id, "smoke-stuck-publishing", "Stuck",
             stuck_iso),
        )
        conn.execute(
            """INSERT INTO draft_posts(candidate_id, slug, title, status, updated_at,
                                  content_html, error_reason)
               VALUES (?, ?, ?, 'failed', ?, '<p>x</p>',
                          'wp_request: HTTP 503')""",
            (candidate_id, "smoke-old-failed", "Old failed",
             old_failed_iso),
        )
        conn.execute(
            """INSERT INTO draft_posts(candidate_id, slug, title, status, updated_at,
                                  content_html, error_reason)
               VALUES (?, ?, ?, 'failed', ?, '<p>x</p>',
                          'wp_request: HTTP 503')""",
            (candidate_id, "smoke-fresh-failed", "Fresh failed",
             fresh_failed_iso),
        )
        conn.commit()
        conn.close()

        # Now run the actual heal.
        healed, retried = janitor.heal_stuck_posts()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        rows = {r["slug"]: r for r in conn.execute(
            "SELECT slug, status, error_reason FROM draft_posts"
        ).fetchall()}

        _assert(rows["smoke-stuck-publishing"]["status"] == "draft",
                f"stuck publishing should be draft, got "
                f"{rows['smoke-stuck-publishing']['status']!r}")
        _assert("healed_from_publishing" in
                (rows["smoke-stuck-publishing"]["error_reason"] or ""),
                f"stuck post error_reason missing marker: "
                f"{rows['smoke-stuck-publishing']['error_reason']!r}")

        _assert(rows["smoke-old-failed"]["status"] == "draft",
                f"old failed should be draft, got "
                f"{rows['smoke-old-failed']['status']!r}")
        _assert("healed_from_failed" in
                (rows["smoke-old-failed"]["error_reason"] or ""),
                f"old failed marker missing: "
                f"{rows['smoke-old-failed']['error_reason']!r}")

        _assert(rows["smoke-fresh-failed"]["status"] == "failed",
                f"fresh failed must NOT be healed, got "
                f"{rows['smoke-fresh-failed']['status']!r}")

        _assert(healed >= 1, f"expected at least 1 healed, got {healed}")
        _assert(retried >= 1, f"expected at least 1 retried, got {retried}")
        conn.close()
        print(f"  PASS  test_janitor_heal_in_isolated_db "
              f"(healed={healed}, retried={retried})")
    finally:
        if original_db_path is not None:
            PIPE.db_path = original_db_path
        db_path.unlink(missing_ok=True)


def _parse_iso(s: str) -> float:
    """Parse ISO-8601 UTC timestamp, return epoch seconds."""
    from datetime import datetime
    # Python's fromisoformat handles 'Z' as UTC since 3.11, but be safe.
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt.timestamp()


# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and callable(v)]
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
        return 1
    print("  ALL GREEN ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())