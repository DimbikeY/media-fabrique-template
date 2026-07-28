"""Sprint 6 (channel-prompt): trigger after WP /approve.

When DD runs ``/approve <post_id>`` (WP-flow), after a successful WP
publication we automatically:

  1. Generate a TG-channel draft via master_prompt_tg.md.
  2. Push the preview to the TG #tg-validation topic (thread id 760 by
     default, configured via TG_THREAD_TG_VALIDATION).
  3. DD sees the preview with the standard set of TG commands:
       /approve_tg <post_id>            — publish to @your_channel
       /reject_tg  <post_id>            — refuse TG-publication
       /edit_tg    <post_id> <note>     — regenerate TG-draft
       /feedback_tg <post_id> <text>    — free-form comment

This is the **separation of concerns** between WP and TG-channel flow:
WP side: rewriter → /approve → published (with image, full text, SEO).
TG side: after_wp_approve() → preview here → /approve_tg → @your_channel.

Public API:
  - after_wp_approve(post_id, *, note=None) -> dict | None
        Trigger entry point. Returns a dict {tg_draft_id, preview_sent,
        tg_chat_id, tg_thread_id, blocked} or None if TG-validation topic
        is not configured.

  - format_preview_message(post_id, tg_draft, *, wp_url=None) -> str
        Renders the preview body with the standard command list.

  - TGTriggerError raised when the trigger cannot complete (LLM error,
    TG send error, missing config).

Failure semantics:
  - TG trigger is BEST-EFFORT relative to the WP /approve. If it fails
    (no TG config, LLM error, send error), the WP /approve still succeeds
    and we log + move on. DD can always run /edit_tg <post_id> manually
    to re-trigger.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

import tg_bridge
from config import PIPE, TG

# Visible domain label rendered in TG-channel posts (footer link). Falls
# back to a generic placeholder when the env var is not set. Operators
# override per-deployment via MEDIA_DISPLAY_DOMAIN (mirrors the same
# constant in tg_channel_publisher.py — kept duplicated here rather
# than re-imported to avoid module-import order surprises in tests).
import os as _os_env
_DISPLAY_DOMAIN = _os_env.environ.get("MEDIA_DISPLAY_DOMAIN", "your-domain.example.com")


# --- Exceptions --------------------------------------------------------------
class TGTriggerError(Exception):
    """Trigger could not complete. /approve caller should log and move on."""


# --- DB helpers --------------------------------------------------------------
def _load_post_row(db_path: Path, post_id: int) -> Optional[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """SELECT id, title, slug, wp_post_url, wp_post_id, status
                 FROM draft_posts WHERE id = ?""",
            (post_id,),
        ).fetchone()
    finally:
        conn.close()


def _load_post_with_source(db_path: Path, post_id: int) -> Optional[sqlite3.Row]:
    """Sprint 6d.5: load draft_posts row JOINed with candidates + sources
    so the preview message can show the original article URL (источник)
    inline next to the WP canonical link."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """SELECT dp.id, dp.wp_post_url,
                      c.url AS item_url, c.title AS item_title,
                      s.name AS source_name, s.homepage_url AS source_homepage
                 FROM draft_posts dp
            LEFT JOIN candidates c ON c.id = dp.candidate_id
            LEFT JOIN sources    s ON s.id = c.source_id
                WHERE dp.id = ?""",
            (post_id,),
        ).fetchone()
    finally:
        conn.close()


# --- Formatting --------------------------------------------------------------
def format_preview_message(
    post_id: int,
    tg_draft: Dict[str, Any],
    *,
    wp_url: Optional[str] = None,
    source_url: Optional[str] = None,
    source_label: Optional[str] = None,
    telegra_url: Optional[str] = None,
) -> str:
    """Render the TG-channel preview message for the validation topic.

    Layout:
        📡 TG-preview для поста #<post_id> (WP ✅ опубликован)
        <empty line>
        🔴 <b>{tg_title}</b>
        <empty line>
        {tg_teaser}
        <empty line>
        #tag1 #tag2 #tag3
        <empty line>
        🔗 <a href="{source_url}">Источник</a> · <a href="{wp_url}">Читать полностью</a>
        ⚡ <a href="{telegra_url}">Instant View</a>  (only if Telegraph OK)
        <empty line>
        ─────
        Команды:
          /preview_tg  <post_id>           — 👁 посмотреть полный TG-preview текст
          /approve_tg  <post_id>           — 📡 опубликовать в @your_channel
          /reject_tg   <post_id>           — 🚫 отклонить TG-публикацию
          /edit_tg     <post_id> <правка>  — ♻️ перегенерировать
          /feedback_tg <post_id> <текст>   — 💬 комментарий

    Sprint 6d.5: source + wp + telegra URLs are clickable inline (matches
    what the final @your_channel post will look like). Backward compat:
    if source_url/wp_url are None, falls back to a literal "🔗 WP: <code>"
    mono line for the old preview style.
    """
    title = (tg_draft.get("tg_title") or "").strip()
    teaser = (tg_draft.get("tg_teaser") or "").strip()
    hashtags = tg_draft.get("tg_hashtags") or []
    blocked = tg_draft.get("blocked", False)

    parts: list[str] = []
    parts.append(
        f"📡 <b>TG-preview для поста #{post_id}</b> (WP ✅ опубликован)"
    )

    if blocked:
        reason = tg_draft.get("reason", "?")
        parts.append(
            f"\n🚫 <b>Стоп-тема</b> (<code>{reason}</code>) — "
            f"TG-preview не сгенерирован. Решай через /edit_tg с другой тональностью, "
            f"или /reject_tg если тема не подходит."
        )
    else:
        parts.append("")  # blank line
        if title:
            parts.append(f"🔴 <b>{_html_escape(title)}</b>")
        parts.append("")
        parts.append(_html_escape(teaser))
        if hashtags:
            rendered_tags = " ".join(
                f"#{_html_escape(str(t))}" for t in hashtags if t
            )
            if rendered_tags:
                parts.append("")
                parts.append(rendered_tags)

    # Sprint 6d.7 + 6d.8: footer split into separate paragraphs with
    # explicit prefix labels (same format as
    # tg_channel_publisher.format_tg_post). 6d.8 renames:
    #   - 'Источник' → 'Первоисточник'
    #   - 'Наше медиа-платформа' → 'Наша медиаплатформа'
    if source_url:
        label = _html_escape(source_label or source_url)
        parts.append("")
        parts.append(
            f"🔗 Первоисточник: <a href=\"{_html_escape(source_url)}\">{label}</a>"
        )
    if wp_url:
        parts.append("")
        parts.append(
            f"Наша медиаплатформа: <a href=\"{_html_escape(wp_url)}\">{_html_escape(_DISPLAY_DOMAIN)}</a>"
        )
    if not source_url and wp_url:
        # Legacy single-URL fallback: when only wp_url is known (old
        # callers haven't been updated), show a non-clickable mono line.
        truncated = wp_url if len(wp_url) <= 60 else wp_url[:57] + "..."
        parts.append("")
        parts.append(f"🔗 WP: <code>{_html_escape(truncated)}</code>")

    # NOTE (6d.9): explicit 'Instant View' link removed from preview
    # too — same reason as tg_channel_publisher.format_tg_post. TG
    # renders the IV button inline via link_preview_options; we don't
    # need to type the URL out in the message body. DD 2026-07-19 21:41.
    # (The block is intentionally empty — leaving the `if telegra_url:`
    # placeholder commented out so a future contributor can re-add the
    # explicit link WITHOUT needing to re-thread the telegra_url logic.)

    parts.append("")
    parts.append("─────────────")
    parts.append("<b>Команды:</b>")
    if not blocked:
        parts.append(
            f"  /preview_tg {post_id}            — 👁 посмотреть полный TG-preview текст"
        )
        parts.append(
            f"  /approve_tg {post_id}            — 📡 опубликовать в "
            f"@your_channel"
        )
    parts.append(f"  /reject_tg {post_id}            — 🚫 отклонить")
    parts.append(
        f"  /edit_tg {post_id} &lt;правка&gt;   — ♻️ перегенерировать TG-preview"
    )
    parts.append(
        f"  /feedback_tg {post_id} &lt;текст&gt; — 💬 комментарий"
    )

    return "\n".join(parts)


def _html_escape(s: str) -> str:
    """Same minimal escape as tg_channel_publisher — TG parse_mode=HTML
    only requires escaping <, >, &."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --- Main entry point --------------------------------------------------------
def after_wp_approve(
    post_id: int,
    *,
    note: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Generate a TG-channel preview and push it to the validation topic.

    Called from _do_approve (WP-flow) right after a successful WP
    publication. Best-effort: failures are logged and re-raised as
    TGTriggerError so the caller can decide whether to surface them.

    Returns:
        dict with keys: post_id, tg_draft_id, blocked, preview_sent,
        tg_chat_id, tg_thread_id, message_id. None if TG thread_tg_validation
        is not configured (treated as a no-op, not an error — dev machines
        and CI don't need this configured).

    Raises:
        TGTriggerError on actual failure (LLM error, send error).
    """
    path = Path(db_path) if db_path is not None else PIPE.db_path

    # No-config short-circuit: this is normal on dev / CI, not an error.
    if not TG.thread_tg_validation or not TG.chat_id or not TG.bot_token:
        logger.info(
            "tg_channel_trigger: not configured (TG.thread_tg_validation={!r}, "
            "TG.chat_id set={}, TG.bot_token set={}); skipping",
            TG.thread_tg_validation, bool(TG.chat_id), bool(TG.bot_token),
        )
        return None

    # Load post (verify it exists; defensive).
    post = _load_post_row(path, post_id)
    if post is None:
        raise TGTriggerError(f"draft_posts id={post_id} not found")

    # Sprint 6d.5: load source/wp URLs for clickable footer in preview.
    post_with_source = _load_post_with_source(path, post_id)
    wp_url = (post["wp_post_url"] or "").strip()
    source_url = ""
    source_label = ""
    if post_with_source is not None:
        source_url = (
            (post_with_source["item_url"] or "").strip()
            or (post_with_source["source_homepage"] or "").strip()
        )
        source_label = (
            (post_with_source["item_title"] or "").strip()
            or (post_with_source["source_name"] or "").strip()
        )

    # Generate TG draft via the same path /edit_tg uses.
    try:
        from tg_regenerate import tg_regenerate
    except ImportError as e:
        raise TGTriggerError(f"tg_regenerate import failed: {e}") from e

    try:
        result = tg_regenerate(post_id, note=note, db_path=path)
    except Exception as e:
        # Bubble up as TGTriggerError so caller can decide. /approve caller
        # will catch and log, NOT fail the WP /approve.
        raise TGTriggerError(
            f"tg_regenerate failed for post_id={post_id}: {type(e).__name__}: {e}"
        ) from e

    blocked = bool(result.get("blocked"))

    # Compose preview message.
    # telegra_url is None here — Telegraph page is built lazily in
    # /approve_tg so we don't double the API cost (preview + actual publish).
    text = format_preview_message(
        post_id,
        result,
        wp_url=wp_url or None,
        source_url=source_url or None,
        source_label=source_label or None,
        telegra_url=None,
    )

    # Send via tg_bridge._send so retries + curl/urllib fallback apply.
    # We bypass _send's "topic" arg here because we want full control
    # over the payload (no disable_web_page_preview — let the URL be
    # visible in DD's preview too).
    thread_id_int = int(TG.thread_tg_validation)
    payload = {
        "chat_id": TG.chat_id,
        "message_thread_id": thread_id_int,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = tg_bridge._call("sendMessage", payload)
    except Exception as e:
        raise TGTriggerError(
            f"TG send failed for post_id={post_id}: {type(e).__name__}: {e}"
        ) from e

    if resp is None:
        # _call returns None on dry-mode (bot_token empty in tests).
        # We've already checked TG.bot_token above, so this should be
        # unreachable in production. Treat as failure defensively.
        raise TGTriggerError(
            f"TG send returned None for post_id={post_id}; "
            f"check TG.bot_token / .env"
        )

    message_id = int((resp.get("result") or {}).get("message_id") or 0)

    logger.info(
        "tg_channel_trigger: post_id={} draft_id={} blocked={} "
        "preview_sent={} thread={} message_id={}",
        post_id, result.get("tg_draft_id"), blocked,
        True, thread_id_int, message_id,
    )

    return {
        "post_id": post_id,
        "tg_draft_id": result.get("tg_draft_id"),
        "blocked": blocked,
        "preview_sent": True,
        "tg_chat_id": TG.chat_id,
        "tg_thread_id": thread_id_int,
        "message_id": message_id,
    }