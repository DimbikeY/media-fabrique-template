"""Daily morning report (Sprint 6.5).

One entry point — `python -m morning_report` or via cron
`mf-morning-report` at 07:00 Europe/Moscow. Pulls aggregates from
SQLite for the last 24 hours, formats them into a single HTML
message, and pushes one message:

* #morning-report — pipeline metrics (the numbers)

DD 2026-07-20 09:26 MSK removed the #ideas side-push: ideas digest
was sending a placeholder bullet to an admin topic nobody was
reading. The TG #ideas topic, the TG_THREAD_IDEAS env var,
push_ideas() and _fmt_ideas() all go away with this sprint. Real
ideas now live in `notes/ideas.md` as plain markdown and are
reviewed during sprint review, not pushed daily.

One message per run; the cron schedule is the gatekeeper. If you
run this twice in a day, you'll see two reports — that's by design,
useful for backfill / testing.

Usage:
    python morning_report.py             # normal run, last 24h
    python morning_report.py --dry       # print to stdout, no TG push
    python morning_report.py --since 48h # look back further
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable

import tg_bridge
from config import PIPE, TG

def _esc(s: object) -> str:
    """Local HTML escape — TG parse_mode='HTML' rejects raw '<', '>', '&'.

    DD 2026-07-20 09:14 MSK: error_reason strings from draft_posts can
    contain things like '<urlopen error timed out>' or '<a href=...>'
    substrings. Without escaping TG bot API returns HTTP 400 "can't
    parse entities: Unsupported start tag 'urlopen' at byte offset N"
    and pushes silently fail. We mirror tg_bridge._html_escape here
    (don't import to avoid a runtime cycle).
    """
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

# Order matters: it's how the report renders. Keep it stable.
_STATUS_ORDER = ("published", "failed", "skipped", "ready", "rewriting")
_REASON_ORDER = ("violent", "political", "vpn", "inoagent", "meta", "review")

def _connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or PIPE.db_path)
    conn.row_factory = sqlite3.Row
    return conn

def _since(window_h: int) -> str:
    """Return the ISO timestamp `window_h` hours ago — passed to SQL."""
    # SQLite's datetime('now', '-N hours') is fine and tz-naive; for the
    # morning report we trust the DB clock matches the host clock (it
    # does — both write datetime('now')).
    return f"-{int(window_h)} hours"

# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #

def aggregate_publish(conn: sqlite3.Connection, since_h: int) -> dict:
    """Counts of draft_posts by status, plus category breakdown for published."""
    rows = conn.execute(
        """SELECT status, COUNT(*) AS c
             FROM draft_posts
            WHERE updated_at >= datetime('now', ?)
            GROUP BY status""",
        (_since(since_h),),
    ).fetchall()
    by_status = {r["status"]: r["c"] for r in rows}
    cat_rows = conn.execute(
        """SELECT i.category AS category, COUNT(*) AS c
             FROM draft_posts p
             JOIN candidates i ON p.candidate_id = i.id
            WHERE p.status = 'published'
              AND p.updated_at >= datetime('now', ?)
            GROUP BY i.category
            ORDER BY c DESC""",
        (_since(since_h),),
    ).fetchall()
    return {
        "by_status": by_status,
        "by_category": [(r["category"] or "—", r["c"]) for r in cat_rows],
    }

def aggregate_skips(conn: sqlite3.Connection, since_h: int) -> dict:
    rows = conn.execute(
        """SELECT safety_status, COUNT(*) AS c
             FROM candidates
            WHERE status = 'skipped'
              AND scored_at >= datetime('now', ?)
            GROUP BY safety_status""",
        (_since(since_h),),
    ).fetchall()
    by_reason = {r["safety_status"]: r["c"] for r in rows}
    failed_rows = conn.execute(
        """SELECT error_reason, COUNT(*) AS c
             FROM draft_posts
            WHERE status = 'failed'
              AND updated_at >= datetime('now', ?)
            GROUP BY error_reason""",
        (_since(since_h),),
    ).fetchall()
    return {
        "by_reason": by_reason,
        "failed_by_reason": [(r["error_reason"], r["c"]) for r in failed_rows],
    }

def aggregate_tokens(conn: sqlite3.Connection, since_h: int) -> dict:
    row = conn.execute(
        """SELECT
              COUNT(*) AS runs,
              COALESCE(SUM(prompt_tokens_provider), 0)      AS p_tok,
              COALESCE(SUM(completion_tokens_provider), 0)  AS c_tok,
              COALESCE(SUM(thinking_tokens_provider), 0)    AS th_tok,
              ROUND(AVG(duration_ms), 0)                    AS avg_ms
             FROM llm_runs
            WHERE started_at >= datetime('now', ?)""",
        (_since(since_h),),
    ).fetchone()
    p = row["p_tok"] or 0
    c = row["c_tok"] or 0
    th = row["th_tok"] or 0
    # thinking tokens are billed differently per provider; we treat them
    # as completion-rate by default (cheap, conservative).
    usd = (p / 1000.0) * TG.llm_cost_per_1k_prompt_usd + \
          ((c + th) / 1000.0) * TG.llm_cost_per_1k_completion_usd
    return {
        "runs": row["runs"],
        "p_tok": p, "c_tok": c, "th_tok": th,
        "avg_ms": row["avg_ms"] or 0,
        "usd": round(usd, 3),
    }

# --------------------------------------------------------------------------- #
# Formatters
# --------------------------------------------------------------------------- #
# Sprint cleanup 2026-07-21: aggregate_feedback + _fmt_feedback removed.
# feedback_signals is gone; feedback notes are write-only (manual review).
# Morning report no longer carries a Feedback section.

def _fmt_publish(section: dict) -> str:
    parts = ["📊 <b>Posts</b>"]
    for status in _STATUS_ORDER:
        if status in section["by_status"]:
            parts.append(f"  · {status}: {section['by_status'][status]}")
    if section["by_category"]:
        parts.append("  <i>by category (published):</i>")
        for cat, c in section["by_category"][:6]:
            parts.append(f"    {cat}: {c}")
    return "\n".join(parts)

def _fmt_skips(section: dict) -> str:
    parts = ["🚫 <b>Skipped / Failed</b>"]
    for reason in _REASON_ORDER:
        if reason in section["by_reason"]:
            parts.append(f"  · skipped.{_esc(reason)}: {section['by_reason'][reason]}")
    if section["failed_by_reason"]:
        parts.append("  <i>draft_posts failed:</i>")
        for reason, c in section["failed_by_reason"][:6]:
            parts.append(f"    {_esc(reason)}: {c}")
    return "\n".join(parts)

def _fmt_tokens(section: dict) -> str:
    p, c, th = section["p_tok"], section["c_tok"], section["th_tok"]
    avg = section["avg_ms"]
    runs = section["runs"]
    return (
        "🧠 <b>LLM usage</b>\n"
        f"  · runs: {runs} (avg {avg} ms)\n"
        f"  · prompt: {p:,} tok · completion: {c:,} tok · thinking: {th:,} tok\n"
        f"  · ≈ ${section['usd']:.2f} USD"
    )

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def build_report(conn: sqlite3.Connection, since_h: int) -> str:
    """Return the morning report HTML text (one message)."""
    pub = _fmt_publish(aggregate_publish(conn, since_h))
    skp = _fmt_skips(aggregate_skips(conn, since_h))
    tok = _fmt_tokens(aggregate_tokens(conn, since_h))
    header = f"📅 <b>Morning report</b> · last {since_h}h\n"
    return "\n\n".join([header, pub, skp, tok])

def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily morning report")
    ap.add_argument("--since", default="24h", help="Look-back window, e.g. 24h, 48h")
    ap.add_argument("--dry", action="store_true", help="Print instead of push to TG")
    ap.add_argument("--db", default=None,
                    help="Override DB path (default: PIPE.db_path). "
                         "Useful for smoke tests with isolated DB.")
    args = ap.parse_args(list(argv) if argv is not None else None)
    since_h = int(str(args.since).rstrip("h"))
    conn = _connect(args.db)
    try:
        morning = build_report(conn, since_h)
    finally:
        conn.close()
    if args.dry:
        print("=== #morning-report ===")
        print(morning)
        return 0
    mr = tg_bridge.push_morning_report(morning)
    # Best-effort push — observational; one failure is fine. Exit 2 lets
    # OpenClaw cron flip lastRunStatus to "error" if the TG push gives up
    # after retries. None == tg_bridge gave up after HTTP_RETRIES (3).
    if mr is None:
        print("[morning_report] TG push FAILED", file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())