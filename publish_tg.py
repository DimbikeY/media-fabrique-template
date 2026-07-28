"""Sprint Y (DD 2026-07-20 22:33 MSK) tick=publish_tg.

Stage 3 of the three-stage WP/TG/Telegraph pipeline:

    tick=wp_publish           (Stage 1, publisher.py)
        │
    tick=generate_for_tg      (Stage 2, generate_for_tg.py)
        │
        ▼  status='approved'
        │
    tick=publish_tg           (Stage 3, this module)
        │
        ▼  Telegraph IV + TG channel sendMessage
           → tg_dispatch.status='published_tg'

This stage is responsible only for:
  1) Picking rows from tg_dispatch WHERE status='approved'
     AND attempts < TG_MAX_TELEGRAPH_ATTEMPTS (default 5).
  2) Checking candidates.expires_at — if expired before we got here,
     mark row as 'expired_skipped' and skip (don't burn LLM tokens or
     Telegraph API calls on stale news).
  3) Calling tg_channel_publisher.publish() which does:
       - Telegraph createPage (Instant View mirror)
       - TG channel sendMessage
  4) On success: tg_dispatch.status='published_tg' + persist Telegraph
     URL/message into draft_posts.tg_channel_* (kept for the admin
     observability preview).
  5) On Telegraph/TG failure: tg_dispatch.attempts += 1 — we keep
     status='approved' so the next cron tick will retry. Only after
     attempts >= TG_MAX_TELEGRAPH_ATTEMPTS do we move to
     status='telegram_blocked_exhausted' (manual review by DD).

Critical: this stage NEVER mutates draft_posts.status. WP posts stay
'published' regardless of TG-channel state. That was the root cause
of the Sprint X ghost-URL bug (rollback deleted WP posts, slugs
mutated to __trashed-N, reader hit 404).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from config import PIPE, PIPE_TICKS


def _next_approved_row(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Pick the next tg_dispatch row to publish.

    Filter:
      - status='approved'
      - attempts < TG_MAX_TELEGRAPH_ATTEMPTS (default 5, env-configurable)
      - candidates.expires_at IS NULL OR expires_at > NOW()

    Order: oldest approved_at first (FIFO so we don't starve anyone).

    Sprint Z (DD 2026-07-21 20:59 MSK): also SELECT c.category + c.weight
    so process_one can build the per-row #published_tg push without an
    extra JOIN roundtrip.
    """
    return conn.execute(
        """
        SELECT d.id, d.post_id, d.tg_title, d.tg_teaser, d.tg_hashtags_json,
               d.attempts, d.approved_at, d.created_at,
               d.failed_reason, d.tg_message_url, d.telegraph_url,
               wp.wp_post_url,
               c.category, c.weight
          FROM tg_dispatch d
          JOIN draft_posts wp ON wp.id = d.post_id
          JOIN candidates  c  ON c.id = wp.candidate_id
         WHERE d.status = 'approved'
           AND d.attempts < ?
           AND (c.expires_at IS NULL OR c.expires_at > datetime('now'))
         ORDER BY d.approved_at ASC
         LIMIT 1
        """,
        (PIPE_TICKS.tg_max_telegraph_attempts,),
    ).fetchone()


def _mark_published(
    conn: sqlite3.Connection,
    dispatch_id: int,
    *,
    telegraph_url: str,
    tg_message_id: int,
    tg_message_url: str,
    post_id: int,
) -> None:
    """Mark successful publish + mirror to draft_posts for admin UI."""
    # Sprint Y: tg_dispatch carries telegraph_url/tg_message_id/tg_message_url;
    # tg_channel_telegra_url lives ONLY on draft_posts (admin observability).
    # (Copy-paste bug from migration 020 caught by test_sprint_y_e2e_smoke.)
    conn.execute(
        """UPDATE tg_dispatch
              SET status = 'published_tg',
                  telegraph_url = ?,
                  tg_message_id = ?,
                  tg_message_url = ?,
                  attempted_at = datetime('now'),
                  updated_at = datetime('now')
            WHERE id = ?""",
        (telegraph_url, tg_message_id, tg_message_url, dispatch_id),
    )
    conn.execute(
        """UPDATE draft_posts
              SET tg_channel_published_at = datetime('now'),
                  tg_channel_message_id = ?,
                  tg_channel_message_url = ?,
                  tg_channel_telegra_url = ?,
                  telegram_sent = 1,
                  updated_at = datetime('now')
            WHERE id = ?""",
        (tg_message_id, tg_message_url, telegraph_url, post_id),
    )


def _mark_blocked_retry(
    conn: sqlite3.Connection, dispatch_id: int, reason: str
) -> None:
    """Telegraph or TG failed; bump attempts. Status stays 'approved'
    so the next tick retries. Janitor is the only thing that moves
    rows out of 'approved' once attempts hits the cap (see
    mark_attempts_exhausted).
    """
    cur = conn.execute(
        """UPDATE tg_dispatch
              SET attempts = attempts + 1,
                  failed_reason = ?,
                  attempted_at = datetime('now'),
                  updated_at = datetime('now')
            WHERE id = ?""",
        (reason[:400], dispatch_id),
    )
    if cur.rowcount == 0:
        return
    # If we've hit the max, park as exhausted for manual review by DD.
    conn.execute(
        """UPDATE tg_dispatch
              SET status = 'telegram_blocked_exhausted'
            WHERE id = ? AND attempts >= ?""",
        (dispatch_id, PIPE_TICKS.tg_max_telegraph_attempts),
    )


def _mark_expired(conn: sqlite3.Connection, dispatch_id: int, reason: str) -> None:
    """Half-life expired between generate_for_tg and now.

    We mark 'expired_skipped' — the post stays on WP (draft_posts.status
    unchanged). Manual recovery: DD can update the candidate to extend
    expires_at, then UPDATE tg_dispatch SET status='approved',
    attempts=0 to retry.
    """
    conn.execute(
        """UPDATE tg_dispatch
              SET status = 'expired_skipped',
                  failed_reason = ?,
                  updated_at = datetime('now')
            WHERE id = ?""",
        (reason[:400], dispatch_id),
    )


def _latency_seconds(approved_at: Optional[str]) -> Optional[int]:
    """Seconds between approved_at and NOW(). None if approved_at missing.

    Sprint Z (DD 2026-07-21 20:59 MSK): used in per-row #published_tg push
    and per-tick summary to show how long a row waited between approval
    and publish_tg pickup. Stored as TEXT (SQLite datetime('now')), so
    we parse with fromisoformat (Python 3.11+ handles 'Z' suffix and
    fractional seconds).
    """
    if not approved_at:
        return None
    try:
        approved_dt = _dt.fromisoformat(approved_at.replace("Z", ""))
    except ValueError:
        return None
    now = _dt.utcnow()
    delta = (now - approved_dt).total_seconds()
    return max(0, int(delta))


def _row_to_outcome_dict(
    *,
    outcome: str,
    dispatch_row: sqlite3.Row,
    failed_reason: Optional[str] = None,
    tg_message_url: Optional[str] = None,
    telegra_url: Optional[str] = None,
    attempts: Optional[int] = None,
) -> Dict[str, Any]:
    """Pack a per-row outcome dict for tg_bridge.push_published_tg().

    Sprint Z (DD 2026-07-21 20:59 MSK): extracts the fields process_one
    already has on hand (dispatch_row + new state after UPDATE) into the
    flat shape push_published_tg expects.
    """
    return {
        "outcome": outcome,
        "dispatch_id": int(dispatch_row["id"]),
        "post_id": int(dispatch_row["post_id"]),
        "title": (dispatch_row["tg_title"] or "").strip() or "(без заголовка)",
        "category": (dispatch_row["category"] or "—") if "category" in dispatch_row.keys() else "—",
        "weight": dispatch_row["weight"] if "weight" in dispatch_row.keys() else None,
        "attempts": int(attempts if attempts is not None else dispatch_row["attempts"]),
        "max_attempts": int(PIPE_TICKS.tg_max_telegraph_attempts),
        "failed_reason": failed_reason,
        "tg_message_url": tg_message_url,
        "telegra_url": telegra_url,
        "wp_url": dispatch_row["wp_post_url"] or "",
        "latency_seconds": _latency_seconds(dispatch_row["approved_at"]),
    }


def process_one(
    conn: sqlite3.Connection, dispatch_row: sqlite3.Row
) -> Dict[str, Any]:
    """Process one tg_dispatch row in 'approved' state.

    Pipeline:
      1. Check candidates.expires_at (filter stale news)
      2. Try tg_channel_publisher.publish() — does Telegraph+TG send
      3. On success: mark_published (mirror to draft_posts for admin)
      4. On telegraph failure: mark_blocked_retry (attempts+=1, retry next)

    Sprint Z (DD 2026-07-21 20:59 MSK): returns a flat outcome dict (see
    _row_to_outcome_dict) instead of just a status string. run() uses
    these fields to fire tg_bridge.push_published_tg() per row + to
    aggregate failed_reasons for the per-tick summary.

    Keys: outcome, dispatch_id, post_id, title, category, weight,
          attempts, max_attempts, failed_reason, tg_message_url,
          telegra_url, wp_url, latency_seconds.
    Outcome values:
      'published' | 'blocked_retry' | 'blocked_exhausted'
      | 'expired_skipped' | 'config_error'
    """
    # Double-check the half-life filter (it was already applied by
    # _next_approved_row, but defensively re-check before we burn
    # a network call).
    cur = conn.execute(
        "SELECT expires_at <= datetime('now') AS is_expired "
        "FROM candidates WHERE id = (SELECT candidate_id FROM draft_posts WHERE id=?)",
        (dispatch_row["post_id"],),
    )
    row = cur.fetchone()
    if row and row[0]:
        _mark_expired(
            conn, dispatch_row["id"], "half_life_expired_at_publish_tg"
        )
        logger.info(
            "publish_tg: post_id={} expired before publish; skip",
            dispatch_row["post_id"],
        )
        return _row_to_outcome_dict(
            outcome="expired_skipped",
            dispatch_row=dispatch_row,
            failed_reason="half_life_expired_at_publish_tg",
            attempts=dispatch_row["attempts"],
        )

    # Delegate the actual TG + Telegraph work to the existing
    # tg_channel_publisher.publish() function. It already handles:
    # - featured_media fetch
    # - Telegraph createPage (with retry/backoff)
    # - TG channel sendMessage
    # - BOT-API error mapping
    import tg_channel_publisher
    try:
        result = tg_channel_publisher.publish(int(dispatch_row["post_id"]))
    except tg_channel_publisher.AlreadyPublished:
        # Sprint Y: if the legacy tg_channel_published_at was set (Sprint X
        # era) we'll get AlreadyPublished. Treat as success because the
        # previous send was already done.
        logger.info(
            "publish_tg: post_id={} already published (Sprint X legacy)",
            dispatch_row["post_id"],
        )
        conn.execute(
            """UPDATE tg_dispatch
                  SET status = 'published_tg',
                      updated_at = datetime('now')
                WHERE id = ?""",
            (dispatch_row["id"],),
        )
        return _row_to_outcome_dict(
            outcome="published",
            dispatch_row=dispatch_row,
            attempts=dispatch_row["attempts"],
        )
    except tg_channel_publisher.TGChannelConfigError as e:
        # Configuration error: missing TG bot_token or channel_id. Bail out
        # without bumping attempts so DD can fix config and rerun.
        logger.error("publish_tg: config error for post_id={}: {}",
                     dispatch_row["post_id"], e)
        reason = f"config_error: {e}"[:400]
        conn.execute(
            """UPDATE tg_dispatch
                  SET failed_reason = ?, updated_at = datetime('now')
                WHERE id = ?""",
            (reason, dispatch_row["id"]),
        )
        return _row_to_outcome_dict(
            outcome="config_error",
            dispatch_row=dispatch_row,
            failed_reason=reason,
            attempts=dispatch_row["attempts"],
        )
    except Exception as e:
        # Telegraph or TG-channel exception. Treat as blocked_retry
        # unless attempts already exhausted.
        logger.warning(
            "publish_tg: telegraph/tg exception for post_id={}: {}",
            dispatch_row["post_id"], e,
        )
        reason = f"publish_error: {type(e).__name__}: {e}"
        _mark_blocked_retry(conn, dispatch_row["id"], reason)
        # Read current attempts+status after the UPDATE.
        cur = conn.execute(
            "SELECT attempts, status FROM tg_dispatch WHERE id=?",
            (dispatch_row["id"],),
        )
        cur_row = cur.fetchone()
        new_attempts = int(cur_row["attempts"]) if cur_row else int(dispatch_row["attempts"])
        new_status = cur_row["status"] if cur_row else "approved"
        outcome = "blocked_exhausted" if new_status == "telegram_blocked_exhausted" else "blocked_retry"
        return _row_to_outcome_dict(
            outcome=outcome,
            dispatch_row=dispatch_row,
            failed_reason=reason,
            attempts=new_attempts,
        )

    # Result is a dict from Sprint 6 publish() — its schema is unchanged.
    message_id = result.get("message_id")
    message_url = result.get("message_url")
    telegra_url = result.get("telegra_url")
    blocked = result.get("blocked", False)

    if blocked or not message_id:
        # blocked=True: telegraph_required, stop-topic, etc.
        # We mirror this as 'blocked_retry' (attempts++) so the operator
        # can see the cadence in admin chat. True 'expired' (per DD
        # 2026-07-20 22:21) is handled separately up top.
        reason = f"publish_blocked: telegram_url={telegra_url!r}"
        _mark_blocked_retry(conn, dispatch_row["id"], reason)
        cur = conn.execute(
            "SELECT attempts, status FROM tg_dispatch WHERE id=?",
            (dispatch_row["id"],),
        )
        cur_row = cur.fetchone()
        new_attempts = int(cur_row["attempts"]) if cur_row else int(dispatch_row["attempts"])
        new_status = cur_row["status"] if cur_row else "approved"
        outcome = "blocked_exhausted" if new_status == "telegram_blocked_exhausted" else "blocked_retry"
        return _row_to_outcome_dict(
            outcome=outcome,
            dispatch_row=dispatch_row,
            failed_reason=reason,
            attempts=new_attempts,
        )

    _mark_published(
        conn,
        dispatch_row["id"],
        telegraph_url=telegra_url or "",
        tg_message_id=int(message_id),
        tg_message_url=message_url or "",
        post_id=int(dispatch_row["post_id"]),
    )
    logger.success(
        "publish_tg ok: dispatch_id={} post_id={} message_id={}",
        dispatch_row["id"], dispatch_row["post_id"], message_id,
    )
    return _row_to_outcome_dict(
        outcome="published",
        dispatch_row=dispatch_row,
        tg_message_url=message_url,
        telegra_url=telegra_url,
        attempts=dispatch_row["attempts"],
    )


def run(limit: Optional[int] = None) -> Dict[str, int]:
    """tick=publish_tg orchestrator.

    Run from cron every 10 min (PIPE_TICKS.publish_tg_cron).
    Picks 'approved' rows with attempts<MAX, publishes them, persists
    the result. Telegraph/TG failures are tolerated — the row stays
    approved and the next tick retries.

    Sprint Z (DD 2026-07-21 20:59 MSK):
      - process_one returns a rich outcome dict (not just a string).
      - run() fires tg_bridge.push_published_tg() per row (one message
        per dispatch outcome — see #published_tg topic).
      - run() aggregates unique_failed_reasons across all rows in this
        tick so the per-tick summary shows a classified breakdown.
      - summary topic FIXED: was "morning_report" (Sprint Y bug), now
        "published_tg" (TG_THREAD_PUBLISHED_TG=977, set in .env).
    """
    n = limit if limit is not None else PIPE_TICKS.publish_tg_limit
    counts = {
        "published": 0, "blocked_retry": 0, "blocked_exhausted": 0,
        "expired_skipped": 0, "config_error": 0,
    }
    per_row_outcomes: List[Dict[str, Any]] = []
    unique_failed_reasons: Dict[str, int] = {}
    start = time.monotonic()
    conn = sqlite3.connect(PIPE.db_path)
    conn.row_factory = sqlite3.Row  # Sprint Y: process_one indexes rows by name.
    # Track dispatch_ids already processed in THIS tick so we don't
    # burn through a row's retry budget in a single cron run. The
    # cron will pick it up on its next tick (because status='approved'
    # and attempts still < MAX). This is the Sprint Y fix that prevents
    # a single Telegraph failure from exhausting the row's 5 attempts
    # within ~5 seconds.
    seen_ids: set[int] = set()
    queue_remaining: int = 0
    try:
        for _ in range(n):
            row = _next_approved_row(conn)
            if row is None:
                break
            if row["id"] in seen_ids:
                # _next_approved_row will keep returning the same row as
                # long as attempts < MAX. Break out so we don't retry
                # within the same tick — the next cron run handles that.
                break
            seen_ids.add(row["id"])
            outcome_dict = process_one(conn, row)
            counts[outcome_dict["outcome"]] = counts.get(outcome_dict["outcome"], 0) + 1
            per_row_outcomes.append(outcome_dict)
            # Aggregate failed_reason prefix for the summary breakdown.
            fr = outcome_dict.get("failed_reason")
            if fr:
                # Classify by prefix before ': ' so 'publish_error: ...',
                # 'publish_blocked: ...', 'config_error: ...' collapse into
                # one bucket each — keeps the summary readable even when
                # the same error fires 10× with different timestamps.
                prefix = fr.split(": ", 1)[0] if ": " in fr else fr[:60]
                unique_failed_reasons[prefix] = unique_failed_reasons.get(prefix, 0) + 1
            conn.commit()  # persist attempts/status updates; close()
                            # without this would roll them back.
        # Snapshot queue size AFTER the tick (status='approved' AND attempts<MAX).
        # Sprint Z: queue_remaining is the most useful number for spotting
        # a backlog. If it grows while published=0 for many ticks in a
        # row, something is wedged (Telegraph down, etc.).
        qcur = conn.execute(
            "SELECT COUNT(*) AS n FROM tg_dispatch d "
            "JOIN draft_posts wp ON wp.id = d.post_id "
            "JOIN candidates    c ON c.id  = wp.candidate_id "
            "WHERE d.status = 'approved' AND d.attempts < ? "
            "AND (c.expires_at IS NULL OR c.expires_at > datetime('now'))",
            (PIPE_TICKS.tg_max_telegraph_attempts,),
        )
        qrow = qcur.fetchone()
        queue_remaining = int(qrow["n"]) if qrow else 0
    finally:
        conn.close()
    runtime_seconds = time.monotonic() - start
    iso_ts = _dt.utcnow().isoformat(timespec="seconds")

    # Best-effort admin notifications: per-row push + per-tick rollup.
    # Both go to #published_tg topic (TG_THREAD_PUBLISHED_TG=977).
    # DD caught the Sprint Y bug where summary went to "morning_report"
    # — topic fix lives here.
    try:
        import tg_bridge
        # Per-row pushes (one message per dispatch outcome).
        for od in per_row_outcomes:
            try:
                tg_bridge.push_published_tg(
                    dispatch_id=od["dispatch_id"],
                    post_id=od["post_id"],
                    title=od["title"],
                    category=od["category"],
                    weight=od["weight"],
                    outcome=od["outcome"],
                    attempts=od["attempts"],
                    max_attempts=od["max_attempts"],
                    failed_reason=od.get("failed_reason"),
                    tg_message_url=od.get("tg_message_url"),
                    telegra_url=od.get("telegra_url"),
                    wp_url=od.get("wp_url", ""),
                    latency_seconds=od.get("latency_seconds"),
                )
            except Exception as per_row_e:
                logger.warning(
                    "publish_tg: per-row push failed for dispatch_id={} (non-fatal): {}",
                    od.get("dispatch_id"), per_row_e,
                )
        # Per-tick rollup. Sprint Z: fire ONLY when we actually processed
        # rows OR when there's a backlog (queue_remaining > 0). If tick
        # was idle and queue is empty, stay silent — same contract as
        # Sprint Y but in the right topic now.
        if counts != {"published": 0, "blocked_retry": 0, "blocked_exhausted": 0,
                      "expired_skipped": 0, "config_error": 0} or queue_remaining > 0:
            tg_bridge.push_published_tg_summary(
                counts=counts,
                runtime_seconds=runtime_seconds,
                queue_remaining=queue_remaining,
                unique_failed_reasons=unique_failed_reasons,
                iso_timestamp=iso_ts,
            )
    except Exception as bridge_e:
        logger.warning("publish_tg: admin summary failed (non-fatal): {}", bridge_e)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    counts = run(limit=args.limit)
    logger.info("tick=publish_tg counts={}", counts)
    print("tick=publish_tg OK", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
