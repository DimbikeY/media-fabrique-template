"""Sprint 6 (channel-prompt): TG-channel "<your_channel>" regeneration.

Public API:
  - tg_regenerate(post_id, note=None) -> dict
        Main entry point. Reads the WP-post from draft_posts, calls the LLM
        with master_prompt_tg.md, persists the result in tg_dispatch.

  - fetch_latest_tg_draft(post_id) -> Optional[Row]
        Returns the most recent tg_dispatch row for a post (by created_at DESC,
        id DESC tiebreaker). None if no drafts yet.

  - mark_tg_channel_published(post_id, message_id, message_url) -> bool
        Idempotently marks the post as published to the channel. Returns
        False if already published (no-op).

  - mark_tg_rejected(post_id, reason="") -> None
        Records that DD rejected the TG-channel preview.

  - add_tg_feedback(post_id, text) -> None
        Stores a free-form comment from DD about the TG-channel draft.

DB tables touched (see migrate.py #018_tg_channel_drafts):
  - draft_posts.tg_channel_published_at / _message_id / _message_url
  - tg_dispatch (history of /edit_tg regenerations)

This module is intentionally NOT coupled to the TG bot — it only handles
the LLM + DB half. Publishing to @your_channel lives in
tg_channel_publisher.py (Sprint 6 step 3).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from config import PIPE
from llm_client import LLMClient
from prompts import TG_PROMPT_VERSION


# --- Exceptions -------------------------------------------------------------
class TGRegenError(Exception):
    """Base for tg_regenerate errors that the caller should surface to the user."""


class PostNotFound(TGRegenError):
    """No draft_posts row with the given id (or it's not yet published)."""


# --- DB plumbing ------------------------------------------------------------
def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection with FK enforced and row-factory set.

    Honors an explicit ``db_path`` for tests (so a smoke can point at an
    isolated DB). Defaults to PIPE.db_path so production code does not have
    to think about it.
    """
    path = Path(db_path) if db_path is not None else PIPE.db_path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _row_to_post_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Map a draft_posts Row to the dict shape build_tg_user_payload expects.

    Tags are stored as JSON string in draft_posts.tags_json — we parse and
    default to [] so a missing/empty column never crashes the LLM call.
    """
    tags_json = row["tags_json"] if "tags_json" in row.keys() else "[]"
    try:
        tags = json.loads(tags_json) if tags_json else []
    except (TypeError, ValueError):
        logger.warning("post {} has invalid tags_json, defaulting to []", row["id"])
        tags = []
    return {
        "id": row["id"],
        "candidate_id": row["candidate_id"],
        "title": row["title"] or "",
        "content": row["content_html"] or "",
        "excerpt": row["excerpt"] or "",
        "category": row["category"] if "category" in row.keys() else "",
        "tags": tags,
        "priority": None,  # We could join candidates.base_score, but not needed for the prompt.
        "source_name": "",  # Source attribution isn't required for TG-channel posts.
        "source_url": "",
        "wp_url": row["wp_post_url"] or "",
        "wp_post_id": row["wp_post_id"],
        "telegram_teaser": row["telegram_teaser"] or "",
    }


def _fetch_post(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row:
    """Fetch the draft_posts row. Raises PostNotFound if missing."""
    row = conn.execute(
        "SELECT * FROM draft_posts WHERE id = ?", (post_id,)
    ).fetchone()
    if row is None:
        raise PostNotFound(f"draft_posts id={post_id} not found")
    return row


def _fetch_candidate_priority(conn: sqlite3.Connection, candidate_id: int) -> Optional[float]:
    """Best-effort priority lookup. None if not scored yet or column missing."""
    try:
        row = conn.execute(
            "SELECT base_score FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row["base_score"] is None:
        return None
    return float(row["base_score"])


# --- Public API -------------------------------------------------------------
def fetch_latest_tg_dispatched(
    post_id: int, db_path: Optional[Path] = None
) -> Optional[sqlite3.Row]:
    """Most recent tg_dispatch row for a post, by created_at DESC then id DESC.

    Returns None if no dispatch yet. The composite index
    idx_tg_dispatch_post_created makes this O(log n) at scale.

    Sprint Y (DD 2026-07-20 22:33 MSK): the table was renamed from
    tg_dispatch (now tg_dispatch) to tg_dispatch to reflect that we now track the full
    status of TG-channel dispatch (status, attempts, failed_reason),
    not just LLM-generated text. Naming follows the new spec in
    notes/technical/sprint-Y-three-stage.md.
    """
    conn = _connect(db_path)
    try:
        return conn.execute(
            """
            SELECT * FROM tg_dispatch
             WHERE post_id = ?
             ORDER BY created_at DESC, id DESC
             LIMIT 1
            """,
            (post_id,),
        ).fetchone()
    finally:
        conn.close()


def save_tg_dispatch(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    tg_title: str,
    tg_teaser: str,
    tg_hashtags: List[str],
    prompt_version: str = TG_PROMPT_VERSION,
    note: Optional[str] = None,
    status: str = "text_generated",
    approved_at: Optional[str] = None,
) -> int:
    """Insert a new row in tg_dispatch. Returns the row id.

    Each /edit_tg creates a new row (history kept); tick=generate_for_tg
    picks the latest by created_at DESC. Caller is responsible for the
    connection's transaction lifecycle (we do not commit here).

    Sprint Y (DD 2026-07-20 22:33 MSK): the row now carries an explicit
    status. The default 'text_generated' is what LLM regeneration uses
    once the model returns text; tick=generate_for_tg rewrites it to
    'awaiting_approval' (manual) or 'approved' (auto) after insert.
    """
    cur = conn.execute(
        """
        INSERT INTO tg_dispatch (
            post_id, tg_title, tg_teaser, tg_hashtags_json,
            prompt_version, note, status, generated_at, approved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
        """,
        (
            post_id,
            tg_title,
            tg_teaser,
            json.dumps(tg_hashtags, ensure_ascii=False),
            prompt_version,
            note,
            status,
            approved_at,
        ),
    )
    return int(cur.lastrowid)


def mark_tg_channel_published(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    message_id: int,
    message_url: str,
) -> bool:
    """Idempotent: marks draft_posts.tg_channel_* with the published info.

    Returns True if state changed (we updated the row), False if already
    published (no-op). Callers should treat False as "already done" and
    not raise — it's the normal /approve_tg idempotency path.
    """
    cur = conn.execute(
        """
        UPDATE draft_posts
           SET tg_channel_published_at = datetime('now'),
               tg_channel_message_id = ?,
               tg_channel_message_url = ?
         WHERE id = ?
           AND tg_channel_published_at IS NULL
        """,
        (message_id, message_url, post_id),
    )
    return cur.rowcount > 0


def mark_tg_rejected(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    reason: str = "",
) -> None:
    """Record that DD rejected the TG-channel preview.

    We currently don't have a dedicated column for this (would be premature
    for v1). For now: log + best-effort write to error_reason. The point is
    to leave a trace so a future Sprint 7 analytics pass can show the
    rejection rate.
    """
    logger.info("tg_channel rejected: post_id={} reason={!r}", post_id, reason)
    # Best effort — do not crash if error_reason column is missing in some
    # legacy DB shape (it has been in the schema since v1, but defensive).
    try:
        conn.execute(
            "UPDATE draft_posts SET error_reason = ? WHERE id = ?",
            (f"tg_channel_rejected: {reason}" if reason else "tg_channel_rejected",
             post_id),
        )
    except sqlite3.OperationalError:
        pass


def add_tg_feedback(
    conn: sqlite3.Connection,
    post_id: int,
    text: str,
) -> None:
    """Record a free-form comment from DD about the TG-channel draft.

    Mirrors the WP-flow's feedback handling: write to error_reason with a
    tag prefix so it's discoverable later. Full feedback UI is Sprint 7+.
    """
    if not text or not text.strip():
        return
    logger.info("tg_channel feedback: post_id={} text={!r}", post_id, text.strip()[:200])
    try:
        conn.execute(
            "UPDATE draft_posts SET error_reason = ? WHERE id = ?",
            (f"tg_channel_feedback: {text.strip()}", post_id),
        )
    except sqlite3.OperationalError:
        pass


# --- Main entry point -------------------------------------------------------
def tg_regenerate(
    post_id: int,
    *,
    note: Optional[str] = None,
    client: Optional[LLMClient] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Regenerate the TG-channel draft for a published WP-post.

    Args:
        post_id: draft_posts.id (the post must exist; we don't gate on
            tg_channel_published_at because /edit_tg should work even after
            publishing — that's the whole point of edit history).
        note: Optional editorial note from DD (via /edit_tg). None = no note,
            treat as a fresh generation.
        client: Optional LLMClient instance (for tests). If None, a real
            client is constructed from config.
        db_path: Optional DB path override (for tests).

    Returns:
        A dict with keys: post_id, tg_draft_id, blocked, tg_title, tg_teaser,
        tg_hashtags, prompt_version, note. blocked=True means the LLM flagged
        a stop-topic; the title/teaser/hashtags are empty in that case.

    Raises:
        PostNotFound: if no draft_posts row exists for post_id.
        RuntimeError: on LLM failure (propagated from LLMClient.tg_rewrite).
    """
    llm = client or LLMClient()
    conn = _connect(db_path)
    try:
        row = _fetch_post(conn, post_id)
        post = _row_to_post_dict(row)
        # Pull priority from candidates if available — TG prompt accepts it
        # but it's optional, so None is fine.
        post["priority"] = _fetch_candidate_priority(conn, row["candidate_id"])
        if note:
            post["note"] = note

        logger.info(
            "tg_regenerate: post_id={} title={!r} has_note={}",
            post_id, post["title"][:60], bool(note),
        )
        result = llm.tg_rewrite(post)
        data = result.data

        # Persist. We commit here because tg_regenerate is an atomic
        # operation from the caller's perspective (one draft per call).
        if data.get("blocked") is True:
            tg_draft_id = save_tg_dispatch(
                conn, post_id,
                tg_title="",
                tg_teaser="",
                tg_hashtags=[],
                note=note,
            )
            conn.commit()
            logger.warning(
                "tg_regenerate blocked: post_id={} reason={!r} draft_id={}",
                post_id, data.get("reason"), tg_draft_id,
            )
            return {
                "post_id": post_id,
                "tg_draft_id": tg_draft_id,
                "blocked": True,
                "reason": data.get("reason", ""),
                "tg_title": "",
                "tg_teaser": "",
                "tg_hashtags": [],
                "prompt_version": TG_PROMPT_VERSION,
                "note": note,
            }

        # Success path: validate against TGChannelOutput once more (defense
        # in depth — llm_client.tg_rewrite already validated, but
        # tg_regenerate callers shouldn't have to trust that path).
        from models import TGChannelOutput
        out = TGChannelOutput.model_validate(data)

        tg_draft_id = save_tg_dispatch(
            conn, post_id,
            tg_title=out.tg_title,
            tg_teaser=out.tg_teaser,
            tg_hashtags=out.tg_hashtags,
            note=note,
        )
        conn.commit()
        logger.info(
            "tg_regenerate ok: post_id={} draft_id={} title={!r}",
            post_id, tg_draft_id, out.tg_title[:60],
        )
        return {
            "post_id": post_id,
            "tg_draft_id": tg_draft_id,
            "blocked": False,
            "reason": "",
            "tg_title": out.tg_title,
            "tg_teaser": out.tg_teaser,
            "tg_hashtags": out.tg_hashtags,
            "prompt_version": TG_PROMPT_VERSION,
            "note": note,
        }
    finally:
        conn.close()


# --- Sprint Y status mutations (DD 2026-07-20 22:33 MSK) -------------------
# These functions keep the tg_dispatch.status enum aligned with the
# /approve_tg, /reject_tg, /edit_tg and /preview_tg command paths. The
# older mark_tg_rejected() above is still useful for tracing but the
# status enum is now the source of truth for whether a row is allowed to
# be picked up by tick=publish_tg.
def mark_tg_dispatch_approved(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    reason: str = "",
) -> int:
    """Mark the latest tg_dispatch row for this post as approved.

    Called from /approve_tg. Sets status='approved' and
    approved_at=NOW() for the most recent tg_dispatch row (the one the
    user is approving after seeing /preview_tg). Returns the rowcount
    so the caller can distinguish success from "no TG preview exists".

    Sprint Y: this is the gate that lets tick=publish_tg pick the row
    up on its next cron run. Before status is 'approved' the row is
    invisible to that tick.
    """
    cur = conn.execute(
        """
        UPDATE tg_dispatch
           SET status = 'approved', approved_at = datetime('now'),
               updated_at = datetime('now')
         WHERE id = (
               SELECT id FROM tg_dispatch
                WHERE post_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
         )
           AND status IN ('awaiting_approval', 'text_generated', 'approved')
        """,
        (post_id,),
    )
    return cur.rowcount


def mark_tg_dispatch_rejected(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    reason: str = "",
) -> int:
    """Mark the latest tg_dispatch row as rejected_tg.

    Called from /reject_tg. Sets status='rejected_tg' on the most
    recent row that's still in a rejectable state
    (awaiting_approval / approved / text_generated / pending_tg_text).
    Janitor will DELETE the row on its next hourly sweep (per
    DD 2026-07-20 22:27 MSK rule: rejected = delete immediately on
    next janitor sweep, no retention period).

    Already-published rows (published_tg) and already-rejected rows
    (rejected_tg) are NOT touched — the caller distinguishes those
    cases via the returned rowcount and surfaces a more informative
    reply to DD. Returns 0 if no row was updated.
    """
    cur = conn.execute(
        """
        UPDATE tg_dispatch
           SET status = 'rejected_tg', updated_at = datetime('now'),
               failed_reason = ?
         WHERE id = (
               SELECT id FROM tg_dispatch
                WHERE post_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
         )
           AND status IN ('awaiting_approval', 'approved',
                           'text_generated', 'pending_tg_text')
        """,
        (f"rejected: {reason}" if reason else "rejected", post_id),
    )
    return cur.rowcount


def mark_tg_dispatch_text_generated(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    prompt_version: str,
    tg_title: str,
    tg_teaser: str,
    tg_hashtags: List[str],
    note: Optional[str],
    status: str,
) -> int:
    """Insert (or upsert) the LLM output for a /edit_tg regeneration.

    Sprint Y: /edit_tg points the most-recent tg_dispatch row to
    'text_generated' so that tick=generate_for_tg re-runs the LLM
    flow on its next pass and produces a new tg_dispatch row. We
    *append* a new row so the old /edit_tg history is preserved
    (read-only audit).
    """
    from datetime import datetime as _dt
    cur = conn.execute(
        """
        INSERT INTO tg_dispatch (
            post_id, tg_title, tg_teaser, tg_hashtags_json,
            prompt_version, note, status, generated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            post_id,
            tg_title,
            tg_teaser,
            json.dumps(tg_hashtags, ensure_ascii=False),
            prompt_version,
            note,
            status,
            _dt.utcnow().isoformat(timespec="seconds") + "Z",
        ),
    )
    return int(cur.lastrowid)