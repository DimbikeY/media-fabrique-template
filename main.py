"""Sprint 5 lightweight: pipeline orchestrator.

A single ``main.py tick=<name>`` invocation runs ONE step of the pipeline
in a child Python process and exits with that child's exit code.

Design notes (DD-approved 2026-07-07):

  * We do NOT add cross-process locking. State machine + atomic claim
    patterns (Sprint 2/4) already make parallel ticks safe. Lock files
    would just be belt-and-suspenders that bring their own bugs (stale
    locks, TTL tuning, who breaks them when).

  * We DO write a heartbeat file (logs/.last_tick) on every successful
    run. It's a forensic tool — ``ls -lt logs/.last_tick`` answers
    "did anything run in the last hour?" without parsing logs.

  * We DO surface the child's exit code as our own exit code, so
    OpenClaw cron ``failureAlert`` works correctly: a tick that crashed
    propagates a non-zero exit, the cron job records a failure, the
    alert triggers after N consecutive failures.

  * Each tick is just a subprocess.run() against an existing CLI
    script. We don't import fetcher/rewriter/publisher directly into
    main.py — keeping them as standalone CLIs means they remain
    usable by humans (``python publisher.py --limit 5``), smoke tests,
    and the cron job in identical ways.

Usage:
    python main.py tick=fetch
    python main.py tick=rewrite --limit 5
    python main.py tick=publish --limit 3
    python main.py tick=janitor

Cron-style entry: ``python main.py tick=<name> [--limit N]``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from config import LOGS_DIR, PIPE_TICKS

# --- Tick registry ----------------------------------------------------------
# Each entry: (cli_module_name, default_args). ``cli_module_name`` is
# invoked as ``python -m <module>`` from the project directory. Keeping
# this table flat (vs a fancy class hierarchy) makes it obvious what
# each tick actually runs when you read the file.

TICK_REGISTRY: dict[str, dict] = {
    "fetch": {
        "module": "rss_fetcher",
        "args": [
            "--max", str(PIPE_TICKS.fetcher_max),
        ],
    },
    "rewrite": {
        "module": "rewrite_and_score",
        "args": [
            "--limit", str(PIPE_TICKS.rewriter_limit),
        ],
    },
    "publish": {
        "module": "publisher",
        "args": [
            "--limit", str(PIPE_TICKS.publisher_limit),
        ],
    },
    # Sprint Y (DD 2026-07-20 22:33 MSK): two new ticks for the three-stage
    # WP/TG/Telegraph pipeline. tick=publish is WP-only now (no Telegraph
    # or TG channel call). tick=generate_for_tg regenerates TG text via
    # master_prompt_tg.md. tick=publish_tg does Telegraph IV + TG channel
    # sendMessage and is fully retry-safe (Telegraph failure doesn't roll
    # back the WP post).
    "generate_for_tg": {
        "module": "generate_for_tg",
        "args": [
            "--limit", str(PIPE_TICKS.generate_for_tg_limit),
        ],
    },
    "publish_tg": {
        "module": "publish_tg",
        "args": [
            "--limit", str(PIPE_TICKS.publish_tg_limit),
        ],
    },
    "janitor": {
        "module": "janitor",
        "args": [],  # janitor runs without flags; deletes + heals
    },
    # Sprint cleanup 2026-07-21: feedback_digest tick removed.
    # Analysis of feedback notes is now manual; /feedback and /feedback_tg
    # write note columns directly with no digest pipeline.
}

# Where to write the heartbeat. Relative to project root. We don't use
# PIPE.log_level for logger config because each child script already
# sets up loguru — main just logs a couple of orchestration lines.
HEARTBEAT_PATH = LOGS_DIR / ".last_tick"


# --- Heartbeat --------------------------------------------------------------

def _write_heartbeat(tick: str, *, ok: bool, returncode: int,
                     duration_s: float, extras: dict | None = None) -> None:
    """Write a small JSON file with the last tick's outcome.

    Why a file (not a DB row, not an HTTP ping): a file is observable
    from any context — humans with `cat`, monitoring scripts with
    `stat -f %m`, the next session of me via `read`. It's also
    trivially rotation-safe: just overwrite."""
    payload = {
        "tick": tick,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "returncode": returncode,
        "duration_seconds": round(duration_s, 2),
    }
    if extras:
        payload.update(extras)
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically: write to .tmp then rename. A half-written
        # heartbeat is worse than a missing one (monitoring would lie).
        tmp = HEARTBEAT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        os.replace(tmp, HEARTBEAT_PATH)
    except OSError as exc:
        # Heartbeat is best-effort. Don't fail the whole tick because
        # the disk is full or the dir is read-only.
        logger.warning("heartbeat write failed: {}", exc)


# --- Orchestrator -----------------------------------------------------------

def _resolve_project_root() -> Path:
    """main.py lives at <project>/main.py — return the project dir.
    Used as cwd for the child process so 'data/news_memory.db' etc.
    resolve relative to the project, not to whatever cron started us."""
    return Path(__file__).resolve().parent


def run_tick(tick: str, extra_args: list[str], *,
             timeout_seconds: int = 0) -> int:
    """Spawn the child process for ``tick``, stream its output to our
    loguru, return its exit code.

    timeout_seconds=0 means no timeout (subprocess.run default).
    We default to no timeout because publisher (image_plan C: image
    generation API call) and rewriter (LLM CoT) can legitimately take
    1-3 minutes on a busy host. The cron scheduler enforces overall
    cadence, not per-tick timeouts.
    """
    if tick not in TICK_REGISTRY:
        logger.error("unknown tick: {!r} (valid: {})",
                     tick, sorted(TICK_REGISTRY.keys()))
        return 2

    entry = TICK_REGISTRY[tick]
    project_root = _resolve_project_root()
    cmd = [sys.executable, "-m", entry["module"], *entry["args"], *extra_args]
    logger.info("tick={} cmd={}", tick, " ".join(cmd))

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(project_root),
            timeout=timeout_seconds if timeout_seconds > 0 else None,
            # Don't capture stdout/stderr — let the child loguru config
            # stream directly to the same loguru sink. That way one
            # tail of logs/pipeline.log shows everything.
        )
    except subprocess.TimeoutExpired:
        logger.error("tick={} TIMEOUT after {}s", tick, timeout_seconds)
        _write_heartbeat(tick, ok=False, returncode=124,
                         duration_s=time.monotonic() - started,
                         extras={"error": "timeout"})
        return 124  # conventional timeout exit code
    except Exception as exc:  # pragma: no cover (subprocess failures)
        logger.exception("tick={} crashed at orchestrator level: {}", tick, exc)
        _write_heartbeat(tick, ok=False, returncode=1,
                         duration_s=time.monotonic() - started,
                         extras={"error": str(exc)})
        return 1

    duration = time.monotonic() - started
    ok = proc.returncode == 0
    _write_heartbeat(tick, ok=ok, returncode=proc.returncode,
                     duration_s=duration)
    if ok:
        logger.info("tick={} OK in {:.2f}s", tick, duration)
    else:
        logger.error("tick={} FAILED exit={} in {:.2f}s",
                     tick, proc.returncode, duration)
    return proc.returncode


# --- CLI --------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="Sprint 5 lightweight orchestrator. Runs ONE pipeline "
                    "tick as a child process and exits with the child's "
                    "exit code. Designed to be called from OpenClaw cron.",
    )
    p.add_argument(
        "tick_spec",
        help="tick=<name> where name is one of: " +
             ", ".join(sorted(TICK_REGISTRY.keys())),
    )
    # Allow arbitrary pass-through flags (e.g. ``--limit 10``) to the
    # child script. Anything after the tick_spec that isn't parsed here
    # is forwarded.
    p.add_argument(
        "--limit", type=int, default=None,
        help="override per-tick limit (rewriter/publisher only)",
    )
    return p


def main() -> int:
    args = _build_parser().parse_args()

    # Parse tick=fetch style positional.
    raw = args.tick_spec.strip()
    if "=" in raw:
        _, _, value = raw.partition("=")
        tick = value.strip()
    else:
        tick = raw

    extra: list[str] = []
    if args.limit is not None and tick in ("rewrite", "publish"):
        extra = ["--limit", str(args.limit)]

    return run_tick(tick, extra)


if __name__ == "__main__":
    sys.exit(main())