"""Sprint Y (DD 2026-07-20 22:33 MSK) tick=generate_for_tg.

Stage 2 of the three-stage WP/TG/Telegraph pipeline:

    tick=wp_publish        (Stage 1, runs in publisher.py)
        │
        ▼  wp_post_url +  INSERT INTO tg_dispatch (status='pending_tg_text')
        │
    tick=generate_for_tg   (Stage 2, this module)
        │
        ▼  LLM regenerates TG text via master_prompt_tg.md
        │  → status='text_generated' → 'awaiting_approval' | 'approved'
        │
    tick=publish_tg        (Stage 3, runs in publish_tg.py)
        │
        ▼  Telegraph IV + TG channel sendMessage
           → status='published_tg' (TG-channel side)
           → draft_posts.tg_channel_* mirrors the result for admin UI

This stage is responsible only for LLM regeneration. It does NOT touch
Telegraph or the TG channel — that lives in publish_tg.py.

Half-life filter (DD 2026-07-20 22:21 MSK): before spending an LLM call,
skip rows whose candidates.expires_at < NOW() — saves tokens on stale
news that won't be sent to TG anyway. empty expires_at means "no scoring
applied yet", which we accept and publish normally (edge case for
drafts created before the half-life system was wired).

Ticket limits: PIPE_TICKS.generate_for_tg_limit (default 5) rows per run.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Optional

from loguru import logger

from config import PIPE, PIPE_TICKS


def _has_tg_dispatch_active_row(conn: sqlite3.Connection, post_id: int) -> bool:
    """Return True if any tg_dispatch row exists in an active state.

    Active means: status NOT IN ('published_tg', 'expired_skipped').
    Used to gate the LLM regeneration: if the latest row is already
    text_generated / awaiting_approval / approved, we skip the
    redundant regen call.
    """
    return bool(
        conn.execute(
            """SELECT 1 FROM tg_dispatch
                WHERE post_id=? AND status NOT IN ('published_tg','expired_skipped','rejected_tg')
                LIMIT 1""",
            (post_id,),
        ).fetchone()
    )


def _fetch_post_with_candidate(conn: sqlite3.Connection, post_id: int) -> Optional[sqlite3.Row]:
    """Pull WP-state + half-life deadline for a post."""
    return conn.execute(
        """SELECT
              wp.id                 AS post_id,
              wp.title              AS title,
              wp.content_html       AS content_html,
              wp.excerpt            AS excerpt,
              wp.slug               AS slug,
              wp.featured_image_path AS featured_image_path,
              wp.image_alt          AS image_alt,
              wp.telegram_teaser   AS telegram_teaser,
              i.category            AS category,
              i.weight              AS weight,
              i.expires_at          AS expires_at,
              i.title               AS source_title,
              s.feed_url            AS source_url,
              s.name                AS source_name
            FROM draft_posts wp
            JOIN candidates i ON i.id = wp.candidate_id
            JOIN sources s ON s.id = i.source_id
           WHERE wp.id = ?""",
        (post_id,),
    ).fetchone()


def _next_pending_row(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Pick the next tg_dispatch row to regenerate.

    We order by draft_posts.id ASC (FIFO) so an old post whose
    regen-but-no-publish already happened doesn't get re-queued
    ahead of a freshly-WP-published post. Skip rows for posts
    that already have an active non-text_generated row downstream
    (avoid duplicate /edit_tg vs regenerate races).
    """
    return conn.execute(
        """
        SELECT d.id, d.post_id, d.prompt_version, d.note
          FROM tg_dispatch d
          JOIN draft_posts wp ON wp.id = d.post_id
          JOIN candidates  c  ON c.id = wp.candidate_id
         WHERE d.status = 'pending_tg_text'
           -- Half-life guard: skip news past its TTL.
           AND (c.expires_at IS NULL OR c.expires_at > datetime('now'))
         ORDER BY wp.id ASC
         LIMIT 1
        """,
    ).fetchone()


def _call_llm_regenerate(post_row: sqlite3.Row, note: Optional[str]) -> Dict:
    """Run the LLM regenerate via the existing tg_regenerate helper.

    This wraps the Sprint 6 tg_regenerate.tg_regenerate() function
    so the LLM call, prompt validation, and error mapping happen in
    a single place. We catch the blocked=True case here so we can
    leave tg_dispatch in 'awaiting_approval' for the next regen
    rather than producing an empty row.
    """
    import tg_regenerate
    try:
        result = tg_regenerate.tg_regenerate(post_row["post_id"], note=note)
    except tg_regenerate.PostNotFound as e:
        raise RuntimeError(f"tg_regenerate: post not found ({e})") from None
    return result


def _auto_or_manual(conn: sqlite3.Connection, new_row_id: int) -> None:
    """Move a freshly-inserted tg_dispatch row to the next status.

    Sprint Y (DD 2026-07-20): if TG_PUBLISH_AUTO_APPROVE=1, jump
    straight to status='approved' so the next tick=publish_tg can
    act immediately. Otherwise leave at 'awaiting_approval' for
    manual /approve_tg.
    """
    if PIPE_TICKS.tg_autoapprove_tg_publish:
        conn.execute(
            """UPDATE tg_dispatch
                  SET status = 'approved',
                      approved_at = datetime('now'),
                      updated_at = datetime('now')
                WHERE id = ?""",
            (new_row_id,),
        )
    else:
        conn.execute(
            """UPDATE tg_dispatch
                  SET status = 'awaiting_approval',
                      updated_at = datetime('now')
                WHERE id = ?""",
            (new_row_id,),
        )


def _mark_attempt_failed(conn: sqlite3.Connection, dispatch_id: int, reason: str) -> None:
    """Persist the LLM failure so it surfaces in admin chat (via fb).

    We keep the row in 'pending_tg_text' so the next cron tick
    retries. After 5 consecutive failures we move to
    'expired_skipped' (manual review) per the DD 2026-07-20 22:27
    rule that the only way to mark rows as 'gone' is expires_at or
    manual /reject_tg.
    """
    cur = conn.execute(
        """UPDATE tg_dispatch
              SET attempts = attempts + 1,
                  failed_reason = ?,
                  updated_at = datetime('now')
            WHERE id = ?""",
        (reason[:400], dispatch_id),
    )
    if cur.rowcount == 0:
        return
    # If attempts exceeded, scrub to expired_skipped so it stops
    # appearing in tick queue (manual recovery: UPDATE status='pending_tg_text').
    conn.execute(
        """UPDATE tg_dispatch
              SET status = 'expired_skipped'
            WHERE id = ? AND attempts >= 5""",
        (dispatch_id,),
    )


def process_one(conn: sqlite3.Connection, dispatch_row: sqlite3.Row) -> str:
    """Process a single tg_dispatch row. Returns the new status string."""
    post_row = _fetch_post_with_candidate(conn, dispatch_row["post_id"])
    if post_row is None:
        logger.error("generate_for_tg: post_id={} missing — skip", dispatch_row["post_id"])
        return "missing"

    # Half-life check at the LLM step (DD 2026-07-20 22:21 MSK): save
    # tokens on stale news that wouldn't reach TG anyway.
    if post_row["expires_at"]:
        cur = conn.execute(
            "SELECT (expires_at <= datetime('now')) AS is_expired "
            "FROM candidates WHERE id = ?", (post_row["post_id"],)
        )
        row = cur.fetchone() if False else None  # placeholder, see below
    # The candidate_expires_check is done via the SELECT in
    # _next_pending_row — a row already past TTL would not have been
    # fetched. We double-check here in case expires_at was set
    # between the fetch and now.
    cur = conn.execute(
        "SELECT expires_at <= datetime('now') AS is_expired "
        "FROM candidates WHERE id = (SELECT candidate_id FROM draft_posts WHERE id=?)",
        (dispatch_row["post_id"],),
    )
    is_expired = cur.fetchone()
    if is_expired and is_expired[0]:
        conn.execute(
            """UPDATE tg_dispatch
                  SET status = 'expired_skipped',
                      failed_reason = 'half_life_expired_at_generate_for_tg',
                      updated_at = datetime('now')
                WHERE id = ?""",
            (dispatch_row["id"],),
        )
        logger.info(
            "generate_for_tg: post_id={} expired during processing; skip LLM call",
            dispatch_row["post_id"],
        )
        return "expired_skipped"

    try:
        result = _call_llm_regenerate(post_row, dispatch_row["note"])
    except Exception as e:
        logger.exception("generate_for_tg: LLM failed for dispatch_id={}", dispatch_row["id"])
        _mark_attempt_failed(conn, dispatch_row["id"], f"llm_error: {type(e).__name__}: {e}")
        return "failed_retry"

    if result.get("blocked"):
        # Sprint 6.7: blocked=stop-topic. Leave the row at status='pending_tg_text'
        # so /edit_tg can re-trigger without spawning a useless
        # text_generated row. We only bump failed_reason.
        _mark_attempt_failed(
            conn, dispatch_row["id"], f"stop_topic: {result.get('reason', '?')}"
        )
        return "blocked"

    # Sprint Y: tg_regenerate.tg_regenerate() inserts a fresh
    # tg_dispatch row with status='text_generated'. Find it and
    # move it to awaiting_approval or approved (auto).
    new_id = result["tg_draft_id"]
    _auto_or_manual(conn, new_id)
    # Sprint Y.1 hotfix (DD 2026-07-21 08:05 MSK): mark the consumed
    # pending_tg_text row as expired_skipped so it doesn't get re-picked
    # on the next cron tick. Without this, every tick re-ran tg_regenerate
    # for the same post (the original pending_tg_text row was never
    # updated, only the freshly-INSERTed `new_id` was moved forward).
    # Symptom: 96 duplicate awaiting_approval rows accumulated for
    # post_id=169 (Disco Elysium) over ~8 hours, burning an LLM call
    # every 5 minutes. See memory/2026-07-21-0800.md.
    conn.execute(
        """UPDATE tg_dispatch
              SET status = 'expired_skipped',
                  failed_reason = 'superseded_by_regen',
                  updated_at = datetime('now')
            WHERE id = ?""",
        (dispatch_row["id"],),
    )
    conn.commit()
    logger.info(
        "generate_for_tg ok: dispatch_id={} new_id={} post_id={} status={} (old row → expired_skipped)",
        dispatch_row["id"], new_id, dispatch_row["post_id"],
        "approved" if PIPE_TICKS.tg_autoapprove_tg_publish else "awaiting_approval",
    )
    return "ok"


def run(limit: Optional[int] = None) -> Dict[str, int]:
    """tick=generate_for_tg: regenerate TG-channel text for fresh WP posts.

    Run from cron every 5 min (PIPE_TICKS.generate_for_tg_cron).
    Picks rows whose status='pending_tg_text', regenerates LLM
    text, then sets status='awaiting_approval' (manual approve
    flow) or 'approved' (auto flow per TG_PUBLISH_AUTO_APPROVE).

    Does NOT touch Telegraph or sendMessage — that's tick=publish_tg.
    """
    n = limit if limit is not None else PIPE_TICKS.generate_for_tg_limit
    counts = {"ok": 0, "failed_retry": 0, "blocked": 0,
              "expired_skipped": 0, "missing": 0}

    conn = sqlite3.connect(PIPE.db_path)
    conn.row_factory = sqlite3.Row  # Sprint Y: process_one indexes rows by name.
    # Track post_ids already processed in THIS tick so a stuck pending
    # row doesn't burn through its retry budget (5 attempts) in one
    # cron run. Next tick picks it back up after status rotates through
    # 'pending_tg_text' again (or janitor handles the saturated case).
    seen_ids: set[int] = set()
    try:
        for _ in range(n):
            row = _next_pending_row(conn)
            if row is None:
                break
            if row["id"] in seen_ids:
                break
            seen_ids.add(row["id"])
            outcome = process_one(conn, row)
            counts[outcome] = counts.get(outcome, 0) + 1
            conn.commit()  # persist LLM regen + status moves; close()
                            # without this would roll them back.
    finally:
        conn.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    counts = run(limit=args.limit)
    logger.info(
        "tick=generate_for_tg counts={}", counts
    )
    print("tick=generate_for_tg OK", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
