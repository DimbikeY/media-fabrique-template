"""Telegram feedback receiver (Sprint cleanup 2026-07-21).

This module is the dispatcher for DD's commands in the Telegram group:

* ``/approve <draft_post_id>`` — approve a draft for publication.
                              Only meaningful when ``WP_PUBLISH_AUTO_APPROVE=0`` (manual review).
                              (Canonical form 1/0 per DD 2026-07-20 11:46 MSK;
                              "true"/"yes" accepted via _env_bool().)
                              When review is disabled the command is a no-op
                              and tells DD to use ``/feedback`` instead.
* ``/feedback <wp_post_id> <note>`` — write a free-form note against an
                              already-published WordPress post. ``wp_post_id``
                              is the WordPress post id (the ``?p=17`` part
                              of the permalink). Persisted to
                              ``draft_posts.feedback_note`` (migration 021).
* ``/feedback_tg <draft_post_id> <note>`` — same for a TG-channel
                              published post. Persisted to
                              ``tg_dispatch.feedback_note`` (migration 021).
* ``/edit  <draft_post_id> <правка>`` — rewrite a draft via LLM.
* ``/edit_tg <draft_post_id> <правка>`` — regenerate the TG-channel text.
* ``/preview  <draft_post_id>`` — show the WP draft text inline.
* ``/preview_tg <draft_post_id>`` — show the TG preview text inline.
* ``/help`` — list commands.

Each ``/approve`` targets a ``draft_post_id`` (the row in
``draft_posts``). The state machine is::

    draft → approved → publishing → published | failed

When ``WP_PUBLISH_AUTO_APPROVE=1`` (auto-publish mode, canonical form
per DD 2026-07-20 11:46 MSK), the
``draft → publishing`` transition happens immediately on the rewriter tick.
In that mode ``/approve`` is still accepted (status is already past draft,
so the transition is a no-op) — the canonical manual action becomes
``/feedback`` against the published post.

Sprint cleanup 2026-07-21:
* ``/reject`` and ``/reject_tg`` are removed (DD wanted "если не понимаю — не используем").
* The feedback_signals table is dropped by migration 021; feedback notes
  now live in ``draft_posts.feedback_note`` / ``tg_dispatch.feedback_note``.
* There is no automated analysis or aggregation; ``/feedback`` and
  ``/feedback_tg`` are write-only notes for future manual review.

State persistence
-----------------
The receiver keeps a tiny offset file (``data/.tg_last_update_id``)
so we don't re-process the same update_id across restarts. The file is
trivially corruptable so we use ``int()`` and fall back to 0 on parse
errors.

Run:
    python feedback_receiver.py            # one polling cycle, then exit
    python feedback_receiver.py --loop     # poll forever (NOT used; cron drives)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import tg_bridge
from config import PIPE, PIPE_TICKS

log = logging.getLogger("feedback_receiver")

OFFSET_FILE = PIPE.db_path.parent / ".tg_last_update_id"


# --------------------------------------------------------------------------- #
# Offset persistence
# --------------------------------------------------------------------------- #


def _load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip() or "0")
    except (OSError, ValueError):
        return 0


def _save_offset(offset: int) -> None:
    try:
        OFFSET_FILE.write_text(str(offset))
    except OSError as e:
        log.warning("offset write failed: %s", e)


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


def _is_dd(update: dict) -> bool:
    """True iff this update was sent by DD (matched by username)."""
    expected = tg_bridge._dd_username().lower()
    if not expected:
        # If TG_DD_USERNAME isn't configured we accept no one — strict
        # default so we don't accidentally act on strangers' messages.
        return False
    msg = update.get("message") or update.get("edited_message") or {}
    frm = msg.get("from") or {}
    uname = (frm.get("username") or "").lower()
    return uname == expected


# --------------------------------------------------------------------------- #
# Command parsers
# --------------------------------------------------------------------------- #


_CMD_APPROVE  = re.compile(r"^/approve\s+(\d+)(?:\s+(.+))?$", re.DOTALL)
# /reject and /reject_tg restored 2026-07-21 (DD 10:48 MSK): needed for
# state-machine moderation even with WP_PUBLISH_AUTO_APPROVE=1. WP-side
# rejection marks draft_posts.status='rejected' (no WP rollback — the post
# stays live in WP, we just flag it in our DB for future analytics).
# TG-side rejection marks tg_dispatch.status='rejected_tg' which janitor
# sweeps on the next hourly tick.
_CMD_REJECT = re.compile(r"^/reject\s+(\d+)(?:\s+(.+))?$", re.DOTALL)
_CMD_REJECT_TG = re.compile(r"^/reject_tg\s+(\d+)(?:\s+(.+))?$", re.DOTALL)
_CMD_FEEDBACK = re.compile(r"^/feedback\s+(\d+)(?:\s+(.+))?$", re.DOTALL)
# Sprint 6m.2: /edit <draft_post_id> <feedback> — feedback is REQUIRED
# (regex requires at least one whitespace char after the id).
_CMD_EDIT     = re.compile(r"^/edit\s+(\d+)\s+(.+)$", re.DOTALL)
_CMD_EDIT_NOFB= re.compile(r"^/edit\s+(\d+)\s*$", re.DOTALL)

# --- Sprint 6 (channel-prompt): TG-channel "<your_channel>" commands ---
# Mirror the WP-flow commands but target the tg_dispatch table. Note order
# matters in parse_command() — _tg variants are tried AFTER their WP twins
# so /approve_tg never accidentally matches the /approve regex.
_CMD_APPROVE_TG  = re.compile(r"^/approve_tg\s+(\d+)\s*$", re.DOTALL)
_CMD_FEEDBACK_TG = re.compile(r"^/feedback_tg\s+(\d+)(?:\s+(.+))?$", re.DOTALL)
_CMD_EDIT_TG     = re.compile(r"^/edit_tg\s+(\d+)\s+(.+)$", re.DOTALL)
_CMD_EDIT_TG_NOFB= re.compile(r"^/edit_tg\s+(\d+)\s*$", re.DOTALL)
# Sprint 6d.5: /preview_tg <post_id> — show the TG-channel preview text
# inline (what /approve_tg would send to @your_channel). Read-only,
# no side-effects. Symmetric to /preview which shows the WP draft.
_CMD_PREVIEW_TG = re.compile(r"^/preview_tg\s+(\d+)\s*$", re.DOTALL)
# Sprint 5.5 mini: /preview <draft_post_id> — show full draft text inline.
# Lets DD read the post without opening WP, then /approve or /reject
# from the same message. Read-only, no side-effects.
_CMD_PREVIEW = re.compile(r"^/preview\s+(\d+)\s*$", re.DOTALL)


# Telegram bot username (without @). Used to strip the @botname suffix
# that Telegram Bot API auto-appends to slash commands in group chats.
# Read from env if present (TELEGRAM_BOT_USERNAME), else fallback.
_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "your_bot")


def _normalize_tg_command(text: str) -> str:
    """Strip @botname suffix from slash commands in group contexts.

    Telegram Bot API syntax:
      - In a private chat (DM):     '/preview 174'
      - In a group chat (no @):      '/preview 174' (with bot username
                                       appended automatically by TG client
                                       UI when picking from the menu)
      - In a group chat (typed):     '/preview@your_bot 174'
                                     or '/preview 174 @your_bot'

    We normalize both group forms to '/preview 174' so parse_command()
    can match them with the existing regexes (no need to duplicate the
    @botname pattern in every regex).

    SAFETY: only touches text that starts with '/'. Anything else
    (e.g. "бла-бла /preview 175" in the middle of normal conversation)
    is left untouched — we never want to accidentally execute a slash
    command that DD wrote as part of regular speech. The downstream
    regexes use re.match() (anchored at start), so even after
    normalization a command in the middle of text would still be ignored.

    Idempotent: '/preview 174' is returned unchanged (no double-strip).
    """
    if not text or not text.startswith("/"):
        return text
    bot = re.escape(_BOT_USERNAME)
    # Remove @botname immediately after /cmd: '/preview@bot 174' → '/preview 174'
    text = re.sub(rf"^/(\w+)@{bot}\b", r"/\1", text, count=1, flags=re.IGNORECASE)
    # Remove @botname at the end (with whitespace before):
    # '/preview 174 @bot' → '/preview 174'
    text = re.sub(rf"\s+@{bot}\s*$", "", text, flags=re.IGNORECASE)
    return text


def parse_command(text: str) -> tuple[str, dict]:
    """Return (cmd_name, kwargs) or ('noop', {}) if not a command.

    Legacy commands ``/up`` and ``/down`` (Sprint 6.6) are accepted as a
    deprecation shim for one cycle: they parse and route to the same
    handlers as ``/approve`` and ``/reject`` respectively, but the reply
    text tells DD to switch. ``/ideas`` and ``/status`` were removed
    without a shim — DD explicitly said drop them.

    Text is first normalized via _normalize_tg_command() so group-context
    forms like '/approve@your_bot 169' parse the
    same as DM '/approve 169'. Normalization only touches text starting
    with '/', so commands mid-sentence are still ignored.
    """
    text = _normalize_tg_command((text or "").strip())
    if not text.startswith("/"):
        return "noop", {}

    # Legacy shim (Sprint 6.6): /up → /approve (kept one cycle for muscle memory).
    # /down is removed alongside /reject — Sprint cleanup 2026-07-21.
    if re.match(r"^/up\s+\d+", text):
        m = _CMD_APPROVE.match("/approve" + text[3:])
        if m:
            return "approve_legacy", {"draft_post_id": int(m.group(1))}

    m = _CMD_APPROVE.match(text)
    if m:
        return "approve", {"draft_post_id": int(m.group(1))}
    # /reject <id> [note] — restored 2026-07-21 per DD 10:48 MSK
    m = _CMD_REJECT.match(text)
    if m:
        return "reject", {"draft_post_id": int(m.group(1)),
                          "note": (m.group(2) or "").strip() or None}
    m = _CMD_FEEDBACK.match(text)
    if m:
        return "feedback", {"wp_post_id": int(m.group(1)),
                            "note": (m.group(2) or "").strip() or None}
    m = _CMD_EDIT.match(text)
    if m:
        return "edit", {"draft_post_id": int(m.group(1)),
                        "feedback": (m.group(2) or "").strip()}
    # /edit without feedback text is not a separate command — _do_edit()
    # returns a re-prompt hint instead.
    m = _CMD_PREVIEW.match(text)
    if m:
        return "preview", {"draft_post_id": int(m.group(1))}
    # --- TG-channel commands (must come AFTER WP twins so /approve_tg
    #     doesn't accidentally hit _CMD_APPROVE which lacks the _tg suffix). ---
    m = _CMD_APPROVE_TG.match(text)
    if m:
        return "approve_tg", {"draft_post_id": int(m.group(1))}
    # /reject_tg <id> [note] — restored 2026-07-21 per DD 10:48 MSK
    m = _CMD_REJECT_TG.match(text)
    if m:
        return "reject_tg", {"draft_post_id": int(m.group(1)),
                             "note": (m.group(2) or "").strip() or None}
    # Sprint 6d.5: /preview_tg <post_id> — try before approve_tg would
    # accidentally match. Order matters: must come AFTER _CMD_PREVIEW
    # (which only matches literal "/preview N"), but BEFORE approve_tg
    # is a non-issue here because approve_tg requires literal "/approve_tg".
    # The narrow regex /preview_tg won't match /preview due to "_tg" suffix.
    m = _CMD_PREVIEW_TG.match(text)
    if m:
        return "preview_tg", {"draft_post_id": int(m.group(1))}
    m = _CMD_FEEDBACK_TG.match(text)
    if m:
        return "feedback_tg", {"draft_post_id": int(m.group(1)),
                               "note": (m.group(2) or "").strip() or None}
    m = _CMD_EDIT_TG.match(text)
    if m:
        return "edit_tg", {"draft_post_id": int(m.group(1)),
                           "feedback": (m.group(2) or "").strip()}
    if text.strip() == "/help":
        return "help", {}
    return "noop", {"raw": text[:200]}


# --------------------------------------------------------------------------- #
# Side effects
# --------------------------------------------------------------------------- #


def _do_approve(draft_post_id: int) -> str:
    # Sprint X (DD 2026-07-19 22:03): rename `publish_requires_review` →
    # `wp_publish_auto_approve`. Semantics flipped: True now means
    # auto-publish (not 'review required'). When auto is enabled,
    # /approve is a no-op redirect — pipeline auto-approves on next tick.
    if PIPE_TICKS.wp_publish_auto_approve:
        # Auto-publish mode — the post is either already published or about
        # to be. /approve is meaningless here; redirect DD to /feedback.
        return (
            f"ℹ️ /approve {draft_post_id}: ревью выключено (WP_PUBLISH_AUTO_APPROVE=1), "
            f"посты публикуются автоматически. Используй /feedback &lt;wp_post_id&gt; для обратной связи."
        )
    try:
        prev = tg_bridge.record_review(
            draft_post_id, "approve", note=None,
            reviewer=tg_bridge._dd_username() or None,
            db_path=PIPE.db_path,
        )
    except Exception as e:
        return f"❌ /approve {draft_post_id}: {e}"
    if prev == "missing":
        return f"❌ /approve {draft_post_id}: черновик не найден"
    if prev in ("approved", "rejected"):
        return (
            f"👍 /approve {draft_post_id} уже {('одобрен' if prev == 'approved' else 'отклонён')}"
        )
    # Sprint 6: after a successful WP /approve (status just moved from
    # 'draft' to 'approved'), push the TG-channel preview into the
    # validation topic. Best-effort: if the trigger fails the WP /approve
    # still succeeded and DD can re-trigger manually via /edit_tg.
    try:
        from tg_channel_trigger import after_wp_approve, TGTriggerError
        result = after_wp_approve(draft_post_id, note=note)
        if result is None:
            tg_line = ""
        elif result.get("blocked"):
            tg_line = "\n📡 TG-preview: 🚫 стоп-тема, валидируй в TG-топике"
        else:
            tg_line = (
                f"\n📡 TG-preview отправлен в топик "
                f"<code>{result['tg_thread_id']}</code> — "
                f"валидируй и жми /approve_tg {draft_post_id}"
            )
    except Exception as e:
        log.warning(
            "tg_channel_trigger after_wp_approve failed (non-fatal) "
            "for post_id=%s: %s",
            draft_post_id, e,
        )
        tg_line = "\n📡 TG-preview: ⚠️ не отправлен (см. лог)"

    return (
        f"👍 /approve {draft_post_id} одобрен → встанет в очередь публикации"
        + tg_line
    )


def _do_reject(draft_post_id: int, note: Optional[str] = None) -> str:
    """Mark a draft_post as rejected (WP-side moderation).

    Direct UPDATE on draft_posts.status — we bypass ``tg_bridge.record_review``
    because that helper only transitions from 'draft'. With
    ``WP_PUBLISH_AUTO_APPROVE=1`` the post is already 'published' in our DB
    the moment publisher.process_one() finishes; DD still needs a way to
    flag the post as rejected without an explicit WP rollback (the WP URL
    stays live; we just record DD's verdict for analytics + future sprints
    that may want to re-publish under a different angle).

    Side effects:
      * draft_posts.status ← 'rejected' (was draft|published|approved)
      * draft_posts.reviewed_at ← NOW
      * draft_posts.reviewed_by ← TG_DD_USERNAME (or DD username)
      * draft_posts.review_note ← <note> if provided
      * tg_dispatch (last row) status ← 'rejected_tg' if it currently has
        awaiting_approval / approved — cascade the WP-rejection into the
        TG-channel side too, otherwise publish_tg would still try to ship.

    Returns the previous status as a string ('draft'/'published' if a
    transition happened; 'rejected' on a replay no-op; 'missing' if the
    id doesn't exist).
    """
    import sqlite3 as _sql
    try:
        with _sql.connect(PIPE.db_path) as conn:
            conn.row_factory = _sql.Row
            row = conn.execute(
                "SELECT status FROM draft_posts WHERE id = ?",
                (draft_post_id,),
            ).fetchone()
            if row is None:
                conn.commit()
                return _fmt_reject_reply("missing", draft_post_id, note)
            prev_status = row["status"]
            if prev_status == "rejected":
                conn.commit()
                return _fmt_reject_reply("rejected", draft_post_id, note)
            # Direct UPDATE — accepts any non-terminal status (draft,
            # published, approved, failed).
            conn.execute(
                """UPDATE draft_posts
                      SET status      = 'rejected',
                          reviewed_at = datetime('now'),
                          reviewed_by = ?,
                          review_note = COALESCE(?, review_note)
                    WHERE id = ? AND status != 'rejected'""",
                (tg_bridge._dd_username() or None, note, draft_post_id),
            )
            # Cascade: if there's a live TG-channel row awaiting publish,
            # reject it too — otherwise publish_tg would still ship it.
            conn.execute(
                """UPDATE tg_dispatch
                      SET status       = 'rejected_tg',
                          updated_at   = datetime('now'),
                          failed_reason = COALESCE(failed_reason, '') ||
                                          CASE WHEN ?1 IS NULL OR ?1 = ''
                                               THEN ''
                                               ELSE 'rejected: ' || ?1
                                          END
                    WHERE id = (
                          SELECT id FROM tg_dispatch
                           WHERE post_id = ?
                             AND status IN ('awaiting_approval', 'approved')
                           ORDER BY created_at DESC, id DESC
                           LIMIT 1
                    )""",
                (note, draft_post_id),
            )
            conn.commit()
    except _sql.Error as e:
        return _fmt_reject_reply("db_error", draft_post_id, note, e)
    except Exception as e:
        log.exception("reject unexpected error for post_id=%s", draft_post_id)
        return _fmt_reject_reply("error", draft_post_id, note, e)
    return _fmt_reject_reply(prev_status, draft_post_id, note)


def _fmt_reject_reply(prev: str, post_id: int, note: Optional[str],
                     exc: Optional[Exception] = None) -> str:
    """Format a reply for /reject based on the previous status."""
    note_suffix = f" — «{_truncate(note)}»" if note else ""
    if prev == "missing":
        return f"❌ /reject {post_id}: черновик не найден"
    if prev == "rejected":
        return f"👎 /reject {post_id} уже отклонён{note_suffix}"
    if prev == "draft":
        return f"👎 /reject {post_id} отклонён (draft → rejected){note_suffix}"
    if prev in ("published", "approved"):
        return (
            f"👎 /reject {post_id} помечен rejected ({prev} → rejected)"
            f"{note_suffix}\n"
            f"  WP-URL остаётся live (нет rollback). "
            f"Для будущих sprint'ов: использовать как сигнал."
        )
    if prev == "db_error":
        return f"❌ /reject {post_id}: БД-ошибка ({exc})"
    if prev == "error":
        return f"❌ /reject {post_id}: {type(exc).__name__ if exc else '?'}: {exc}"
    return f"👎 /reject {post_id} → rejected{note_suffix}"


def _do_edit(draft_post_id: int, feedback: Optional[str]) -> str:
    """Sprint 6m.2: rewrite a draft with DD's feedback applied.

    Flow:
      1. Load current draft_posts row.
      2. If missing / terminal → tell DD.
      3. Build an LLMClient. Send current_post + feedback → new_data.
      4. tg_bridge.record_edit overwrites title/excerpt/content/...
         (status stays 'draft'; previous version is gone).
      5. Return short preview to TG.

    Latency: one LLM round-trip (~10–60s with MiniMax-M3 CoT). Feedback
    is REQUIRED; if absent we return the re-prompt hint instead of calling
    LLM. Failure modes (LLM error, parse error) surface as ❌ with the
    underlying message so DD can decide whether to retry.
    """
    if not feedback:
        return _bad_command_hint("/edit", "нужен текст правки")
    # 1. Load current draft row.
    try:
        with sqlite3.connect(PIPE.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT id, title, excerpt, content_html, slug,
                          meta_title, meta_description, image_alt,
                          image_prompt, categories_json, tags_json,
                          telegram_teaser, status
                     FROM draft_posts WHERE id = ?""",
                (draft_post_id,),
            ).fetchone()
    except sqlite3.Error as e:
        return f"❌ /edit {draft_post_id}: БД-ошибка: {e}"
    if row is None:
        return f"❌ /edit {draft_post_id}: черновик не найден"
    if row["status"] != "draft":
        return (
            f"❌ /edit {draft_post_id}: статус «{row['status']}», "
            f"редактирование возможно только для draft. "
            f"Используй /feedback &lt;wp_post_id&gt; для опубликованного."
        )

    # 2. Call LLM to regenerate.
    current = {
        "title": row["title"],
        "excerpt": row["excerpt"],
        "content_html": row["content_html"],
        "slug": row["slug"],
        "meta_title": row["meta_title"],
        "meta_description": row["meta_description"],
        "image_alt": row["image_alt"],
        "image_prompt": row["image_prompt"],
        "categories_json": row["categories_json"],
        "tags_json": row["tags_json"],
        "telegram_teaser": row["telegram_teaser"],
    }
    try:
        from llm_client import LLMClient  # local import — keep startup fast
        client = LLMClient()
        result = client.edit_post(current, feedback)
        new_data = result.data
    except Exception as e:
        log.exception("edit_post failed for draft %s", draft_post_id)
        return f"❌ /edit {draft_post_id}: LLM-ошибка: {e}"

    # 3. Overwrite in DB.
    try:
        outcome = tg_bridge.record_edit(
            draft_post_id, new_data, feedback,
            reviewer=tg_bridge._dd_username() or None,
            db_path=PIPE.db_path,
        )
    except Exception as e:
        return f"❌ /edit {draft_post_id}: запись в БД: {e}"
    if outcome == "missing":
        return f"❌ /edit {draft_post_id}: черновик исчез во время правки"
    if outcome == "terminal":
        return (
            f"❌ /edit {draft_post_id}: статус сменился во время правки, "
            f"ничего не перезаписано"
        )

    # 4. Build preview reply.
    # Strip HTML before sending to TG: parse_mode=HTML, so any raw
    # <p>/<a>/etc in content_html breaks TG parser with HTTP 400
    # "Unsupported start tag". Reuse the same approach _do_preview uses.
    new_title = new_data.get("title") or current["title"]
    new_content = (
        new_data.get("content") or new_data.get("content_html") or current["content_html"]
    )
    import re as _re_strip  # noqa: F401 - kept for legacy callers
    # Delegate the success-path reply to _do_preview so DD sees the same
    # format as #drafts previews: full title, excerpt, content (with HTML
    # strip + 3500-char TG limit), weight/age/half-life/bucket header, and
    # WP admin + public links. A single leading done line marks it
    # as the post-edit snapshot so DD knows the LLM rewrite just completed.
    # fr._do_preview reads the freshly-updated row (record_edit above
    # already committed the new title/content_html/excerpt/etc.).
    full_preview = _do_preview(draft_post_id)
    return "<b>/edit " + str(draft_post_id) + " готово</b>\\n\\n" + full_preview





# --------------------------------------------------------------------------- #
def _truncate(s: str, n: int = 80) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _bad_command_hint(cmd: str, why: str) -> str:
    """Re-prompt hint: echo the original text + list available commands.

    Per DD contract (Sprint 6m.2): bad commands get a re-prompt with the
    available commands listed. No note is recorded anywhere — bad input
    shouldn't pollute the manual-review columns.
    """
    return (
        f"❌ {cmd}: {why}.\n\n"
        "Доступно:\n"
        "  /approve &lt;draft_post_id&gt; — одобрить черновик\n"
        "  /edit &lt;draft_post_id&gt; <u>правка</u>   — перегенерировать с учётом правки\n"
        "  /feedback &lt;wp_post_id&gt; <u>коммент</u>   — заметка по опубликованному WP-посту\n"
        "  /feedback_tg &lt;draft_post_id&gt; <u>коммент</u> — заметка по опубликованному TG-посту\n"
        "Пример: /edit 195 сделай введение короче"
    )


def _do_preview(draft_post_id: int) -> str:
    """Sprint 5.5 mini: /preview <draft_post_id> — send the full draft text
    back to the caller so DD can read the post inline (without opening WP)
    and then /approve or /reject from the same message.

    Returns TG-formatted HTML text (less-than 3500 chars body; longer
    content gets truncated with a "see WP" pointer). No DB writes,
    no side-effects.
    """
    import sqlite3 as _sq
    import re as _re2
    from html import escape as _html_escape
    import config as _cfg
    from urllib.parse import quote as _quote
    try:
        con = _sq.connect(PIPE.db_path)
        con.row_factory = _sq.Row
        row = con.execute(
            """
            SELECT d.id, d.title, d.slug, d.excerpt, d.content_html,
                   d.status, d.wp_post_id, d.wp_post_url,
                   c.weight, c.base_score, c.half_life_h,
                   c.scored_at,
                   CAST((julianday('now') - julianday(c.scored_at)) * 24
                        AS REAL) AS age_hours,
                   c.category
              FROM draft_posts d JOIN candidates c ON c.id = d.candidate_id
             WHERE d.id = ?
            """,
            (draft_post_id,),
        ).fetchone()
    except Exception as e:
        return f"❌ /preview {draft_post_id}: {e}"
    finally:
        try:
            con.close()
        except Exception:
            pass

    if row is None:
        return f"❌ /preview {draft_post_id}: черновик не найден"

    def _strip_html(s):
        if not s:
            return ""
        # Drop script/style content entirely (rare but cheap).
        s = _re2.sub(r"<script\b.*?</script>", " ", s, flags=_re2.DOTALL | _re2.IGNORECASE)
        s = _re2.sub(r"<style\b.*?</style>",   " ", s, flags=_re2.DOTALL | _re2.IGNORECASE)
        # Block-level tags become paragraph breaks.
        s = _re2.sub(r"</\s*(p|div|li|h[1-6])\s*>", "\n\n", s, flags=_re2.IGNORECASE)
        s = _re2.sub(r"<\s*br\s*/?>", "\n", s, flags=_re2.IGNORECASE)
        # Drop remaining tags.
        s = _re2.sub(r"<[^>]+>", "", s)
        # Decode the entities we actually emit.
        s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"")
              .replace("&#39;", "\'"))
        s = _re2.sub(r"[ \t]+", " ", s)
        s = _re2.sub(r"\n\s*\n\s*\n+", "\n\n", s)
        return s.strip()

    title = (row["title"] or "").strip() or "(без заголовка)"
    excerpt = _strip_html(row["excerpt"] or "")
    content = _strip_html(row["content_html"] or "")
    status = row["status"] or "?"
    weight = float(row["weight"] or 0)
    category = row["category"] or "—"

    # Compact weight explanation (Sprint 5.5 explain):
    # `weight` is the decayed score; `base_score` is the LLM-emitted
    # priority from rewrite_and_score. Decay rate depends on category
    # via CATEGORY_HALF_LIFE_H (scoring.py).
    if weight >= 7.0:
        bucket = 'top'
    elif weight >= 5.0:
        bucket = 'mid'
    else:
        bucket = 'low'
    base_disp = f"{row['base_score']:.1f}" if row['base_score'] is not None else '—'
    age_hours = row['age_hours']
    age_str = (
        f"{age_hours:.0f}h" if age_hours is not None and age_hours < 240
        else ("—" if age_hours is None else f"{age_hours / 24:.1f}d")
    )
    half_life_str = (
        f"{row['half_life_h']:.0f}h" if row['half_life_h'] is not None else '—'
    )
    header = (
        f"📄 <b>Draft #{draft_post_id}</b> · <i>{_html_escape(status)}</i>\n"
        f"📂 {_html_escape(category)} · <i>half-life {half_life_str}</i>\n"
        f"📊 weight <b>{weight:.1f}</b>/10 · base ≈ {base_disp} · "
        f"{age_str} ago · <i>({bucket})</i>\n"
        f"\n<b>{_html_escape(title)}</b>"
    )

    body_parts = []
    if excerpt:
        body_parts.append(f"<i>{_html_escape(excerpt)}</i>")
    if content:
        body_parts.append(content)

    body = "\n\n".join(body_parts) if body_parts else ""
    # WordPress links: admin URL (always), public URL (only if published).
    slug = (row["slug"] or "").strip()
    wp_post_id = row["wp_post_id"]
    wp_post_url = (row["wp_post_url"] or "").strip()
    if wp_post_id:
        admin_url = f"{_cfg.WP.base_url}/wp-admin/post.php?post={wp_post_id}&action=edit"
        # public_url is only meaningful for PUBLISHED posts. For drafts, an
        # anonymous GET /?p=ID returns 404 (WP doesn't expose drafts to
        # non-authenticated visitors), so showing the link is misleading.
        if status == "published":
            public_url = wp_post_url or f"{_cfg.WP.base_url}/?p={wp_post_id}"
        else:
            public_url = None
    else:
        # For drafts (no wp_post_id yet), WP admin edit.php?s=SLUG does NOT work
        # because edit.php searches post_title, not post_name. We feed it a
        # 40-char title prefix instead — that's reliably unique per draft in
        # practice (otherwise WP shows a list of candidates DD can pick from).
        title_prefix = (row['title'] or '').strip()[:40]
        admin_url = (
            f"{_cfg.WP.base_url}/wp-admin/edit.php"
            f"?post_type=post&post_status=all&s={_quote(title_prefix)}"
        )
        public_url = None

    link_lines = [f'🔗 <a href="{_html_escape(admin_url)}">открыть в WP-админке</a>']
    if public_url:
        link_lines.append(f'👁 <a href="{_html_escape(public_url)}">публичная страница</a>')
    link_block = chr(10).join(link_lines)

    footer = (
        "─────\n"
        f"<code>/approve {draft_post_id}</code> — 👍 одобрить и опубликовать\n"
        f"<code>/edit {draft_post_id}</code> правка — ✍️ перегенерировать\n"
        "\n"
        f"{link_block}"
    )

    full = f"{header}\n\n{body}\n\n{footer}" if body else f"{header}\n\n{footer}"

    # TG hard cap is 4096 chars per message. We budget 3500 for the full
    # text and ~300 for header+footer; if still over, truncate body and
    # append a "see WP" pointer. Footer always lands intact.
    MAX = 3500
    if len(full) > MAX:
        keep_chars = MAX - len(header) - len(footer) - 80
        keep_chars = max(keep_chars, 200)
        body_truncated = body[:keep_chars]
        if " " in body_truncated[-50:]:
            body_truncated = body_truncated.rsplit(" ", 1)[0]
        body = body_truncated + "\n\n…(truncated; открой в WP для полного текста)"
        full = f"{header}\n\n{body}\n\n{footer}"

    return full


def _do_feedback(wp_post_id: int, note: Optional[str]) -> str:
    """Persist a free-form feedback note against an already-published post.

    ``wp_post_id`` is the WordPress post id (the integer after ``?p=`` in
    the permalink). We look it up in ``draft_posts`` and write the note
    into ``draft_posts.feedback_note`` (Sprint cleanup 2026-07-21:
    feedback_signals is gone). No automation, no analysis — purely for
    DD's future manual review.

    Note is REQUIRED — without it we ask DD what they meant via the
    re-prompt hint. Sprint cleanup 2026-07-21 removed the legacy "save
    the orphan even if wp_post_id is unknown" branch because there's no
    signal table to write to anymore.
    """
    if not note:
        return _bad_command_hint("/feedback", "нужен текст комментария")
    try:
        result = tg_bridge.add_post_feedback(
            wp_post_id, note=note,
            reviewer=tg_bridge._dd_username() or None,
            db_path=PIPE.db_path,
        )
    except Exception as e:
        return f"❌ /feedback {wp_post_id}: {e}"
    if result == "missing":
        return (
            f"❌ /feedback {wp_post_id}: пост не найден в нашей БД "
            f"(проверь что wp_post_id корректный)"
        )
    suffix = f" — «{_truncate(note)}»" if note else ""
    return f"💬 /feedback {wp_post_id} записан в draft_posts.feedback_note{suffix}"


def _do_help() -> str:
    # Sprint X rename + flip: True means auto-publish is ON (not 'review enabled').
    review_state = "выключено (auto-publish)" if PIPE_TICKS.wp_publish_auto_approve else "включено"
    return (
        "📖 <b>Команды</b>\n"
        f"  /approve &lt;draft_post_id&gt;         — 👍 одобрить черновик → публикация (ревью: {review_state})\n"
        "  /reject &lt;draft_post_id&gt; <u>причина</u> — 👎 пометить rejected (draft/published/approved → rejected)\n"
        "  /edit &lt;draft_post_id&gt; <u>правка</u>   — ✍️ перегенерировать с учётом правки (только draft)\n"
        "  /feedback &lt;wp_post_id&gt; <u>коммент</u> — 💬 записать заметку по опубликованному WP-посту\n"
        "  /preview &lt;draft_post_id&gt;         — 👁 показать полный текст черновика в чате\n"
        "  ─── TG-канал «<your_channel>» ───\n"
        "  /approve_tg &lt;draft_post_id&gt;        — 📡 одобрить TG-preview → cron publish_tg опубликует\n"
        "  /reject_tg &lt;draft_post_id&gt; <u>причина</u> — 🚫 пометить TG-preview rejected_tg (janitor удалит)\n"
        "  /edit_tg &lt;draft_post_id&gt; <u>правка</u>  — ♻️ перегенерировать TG-preview через master_prompt_tg.md\n"
        "  /feedback_tg &lt;draft_post_id&gt; <u>коммент</u> — 💬 записать заметку по опубликованному TG-посту\n"
        "  /help                                — эта справка\n"
        "\n"
        "<i>Sprint cleanup 2026-07-21: /feedback только в feedback_note колонку. "
        "/reject* восстановлены DD 10:48 MSK для state-machine moderation.</i>"
    )


# --- Sprint Y (DD 2026-07-20 22:33 MSK): /approve_tg no longer publishes
# directly. It moves the latest tg_dispatch row to status='approved'
# (and sets approved_at), and tick=publish_tg will pick it up on the
# next cron run to create the Telegraph mirror + send the TG channel
# message. This breaks the Sprint 6 monolithic publish flow and lets
# Telegraph failures queue for retry without blocking the WP post.
def _do_approve_tg(draft_post_id: int) -> str:
    """Move tg_dispatch row to approved; tick=publish_tg picks up next."""
    try:
        from tg_regenerate import mark_tg_dispatch_approved, PostNotFound
    except ImportError as e:
        return f"❌ /approve_tg {draft_post_id}: модуль tg_regenerate недоступен ({e})"
    try:
        with sqlite3.connect(PIPE.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT 1 FROM draft_posts WHERE id = ?", (draft_post_id,)
            ).fetchone()
            if row is None:
                return f"❌ /approve_tg {draft_post_id}: пост не найден"
            updated = mark_tg_dispatch_approved(conn, draft_post_id)
            conn.commit()
    except sqlite3.Error as e:
        return f"❌ /approve_tg {draft_post_id}: БД-ошибка ({e})"
    except Exception as e:
        log.exception("approve_tg unexpected error")
        return f"❌ /approve_tg {draft_post_id}: {type(e).__name__}: {e}"

    if updated == 0:
        # No awaiting_approval / text_generated / approved row exists —
        # either the LLM regen hasn't run yet, or the row is already
        # published_tg / rejected_tg.
        latest = conn.execute(
            """SELECT status FROM tg_dispatch
                WHERE post_id=?
                ORDER BY created_at DESC, id DESC LIMIT 1""",
            (draft_post_id,),
        ).fetchone()
        if latest is None:
            return f"❌ /approve_tg {draft_post_id}: нет TG-preview (сначала жди tick=generate_for_tg)"
        if latest["status"] == "published_tg":
            return f"ℹ️ /approve_tg {draft_post_id}: уже опубликовано (status=published_tg)"
        if latest["status"] == "rejected_tg":
            return f"🚫 /approve_tg {draft_post_id}: TG-preview отклонён ранее (/edit_tg для перегенерации)"
        return f"❌ /approve_tg {draft_post_id}: state={latest['status']} — действие невозможно"

    return (
        f"📡 /approve_tg {draft_post_id} одобрено\n"
        f"  Tick=publish_tg опубликует в @your_channel в течение 10 минут.\n"
        f"  Telegraph IV создаётся в этом же тике.\n"
        f"  Уведомление #published_tg придёт после sendMessage."
    )


def _do_reject_tg(draft_post_id: int, note: Optional[str] = None) -> str:
    """Mark the latest tg_dispatch row for this post as rejected_tg.

    WP-side stays untouched (post may be published in WP; we only flag
    the TG-channel draft as not-shippable). Janitor sweeps the
    rejected_tg rows on its next hourly tick (DD 2026-07-20 22:27 rule).
    """
    try:
        from tg_regenerate import mark_tg_dispatch_rejected, PostNotFound
    except ImportError as e:
        return f"❌ /reject_tg {draft_post_id}: модуль tg_regenerate недоступен ({e})"
    try:
        with sqlite3.connect(PIPE.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT 1 FROM draft_posts WHERE id = ?", (draft_post_id,)
            ).fetchone()
            if row is None:
                return f"❌ /reject_tg {draft_post_id}: пост не найден"
            updated = mark_tg_dispatch_rejected(conn, draft_post_id,
                                                reason=note or "")
            # Also persist the free-form note into feedback_note column
            # so it's discoverable from the admin side (Sprint cleanup
            # 2026-07-21: feedback_note is the single retention column).
            if updated and note:
                conn.execute(
                    """UPDATE tg_dispatch
                          SET feedback_note = ?,
                              updated_at   = datetime('now')
                        WHERE id = (
                              SELECT id FROM tg_dispatch
                               WHERE post_id = ?
                               ORDER BY created_at DESC, id DESC
                               LIMIT 1
                        )""",
                    (note[:2000], draft_post_id),
                )
            conn.commit()
    except sqlite3.Error as e:
        return f"❌ /reject_tg {draft_post_id}: БД-ошибка ({e})"
    except Exception as e:
        log.exception("reject_tg unexpected error for post_id=%s", draft_post_id)
        return f"❌ /reject_tg {draft_post_id}: {type(e).__name__}: {e}"

    if updated == 0:
        # No live TG-preview in awaiting_approval / approved / text_generated.
        latest = conn.execute(
            """SELECT status FROM tg_dispatch
                WHERE post_id=?
                ORDER BY created_at DESC, id DESC LIMIT 1""",
            (draft_post_id,),
        ).fetchone()
        if latest is None:
            return f"❌ /reject_tg {draft_post_id}: нет TG-preview (сначала жди tick=generate_for_tg)"
        if latest["status"] == "published_tg":
            return (
                f"ℹ️ /reject_tg {draft_post_id}: уже опубликовано в TG "
                f"(status=published_tg). /reject_tg действует только "
                f"на pending TG-preview, не на опубликованные."
            )
        if latest["status"] == "rejected_tg":
            return f"🚫 /reject_tg {draft_post_id}: TG-preview уже отклонён ранее"
        return (
            f"❌ /reject_tg {draft_post_id}: state={latest['status']} — "
            f"нельзя отклонить (только awaiting_approval/approved/text_generated)"
        )

    note_suffix = f" — «{_truncate(note)}»" if note else ""
    return (
        f"🚫 /reject_tg {draft_post_id} TG-публикация отклонена{note_suffix}\n"
        f"  Janitor удалит строку на ближайшем hourly sweep.\n"
        f"  /edit_tg для перегенерации, /approve_tg для отмены (если ещё не удалено)."
    )


def _do_edit_tg(draft_post_id: int, feedback: Optional[str]) -> str:
    """Regenerate the TG-channel draft with DD's note applied.

    Mirrors _do_edit() but routes through master_prompt_tg.md (NOT
    master_prompt.md). One LLM call (~10–60s with MiniMax-M3 CoT);
    the caller (feedback_webhook) is responsible for the immediate
    "working on it" ack.
    """
    if not feedback:
        return _bad_command_hint("/edit_tg", "нужен текст правки")
    try:
        from tg_regenerate import tg_regenerate, PostNotFound
    except ImportError as e:
        return f"❌ /edit_tg {draft_post_id}: модуль tg_regenerate недоступен ({e})"
    try:
        result = tg_regenerate(draft_post_id, note=feedback)
    except PostNotFound as e:
        return f"❌ /edit_tg {draft_post_id}: {e}"
    except RuntimeError as e:
        return f"❌ /edit_tg {draft_post_id}: ошибка LLM ({e})"
    except Exception as e:
        log.exception("edit_tg unexpected error")
        return f"❌ /edit_tg {draft_post_id}: {type(e).__name__}: {e}"

    if result.get("blocked"):
        return (
            f"🚫 /edit_tg {draft_post_id}: стоп-тема (<code>{result.get('reason')}</code>), "
            f"новый TG-preview не сгенерирован."
        )
    return (
        f"♻️ /edit_tg {draft_post_id} TG-preview обновлён (tg_dispatch_id={result['tg_draft_id']})\n\n"
        f"<b>Заголовок:</b> {_truncate(result['tg_title'], 80)}\n"
        f"<b>Тизер:</b> {_truncate(result['tg_teaser'], 200)}\n"
        f"<b>Хэштеги:</b> {' '.join('#' + h for h in result['tg_hashtags'])}\n\n"
        f"Посмотри preview и пришли /approve_tg {draft_post_id} если ок.\n"
        f"Sprint Y (DD 2026-07-20): строка выше теперь создаёт ЗАПИСЬ "
        f"в tg_dispatch со status='text_generated'. Tick=generate_for_tg "
        f"опционально регенерирует заново (auto-approve если включено)."
    )


def _do_feedback_tg(draft_post_id: int, note: Optional[str]) -> str:
    """Store a free-form comment against the latest tg_dispatch row.

    Sprint cleanup 2026-07-21: writes to ``tg_dispatch.feedback_note``
    (new column from migration 021) instead of the legacy
    ``draft_posts.error_reason`` tag. No LLM, no analysis — purely for
    DD's future manual review, same semantics as the WP-flow /feedback.
    """
    if not note:
        return _bad_command_hint("/feedback_tg", "нужен текст комментария")
    try:
        result = tg_bridge.record_tg_feedback_note(
            draft_post_id, note=note,
            db_path=PIPE.db_path,
        )
    except Exception as e:
        return f"❌ /feedback_tg {draft_post_id}: {e}"
    if result == "missing":
        return (
            f"❌ /feedback_tg {draft_post_id}: нет tg_dispatch (TG-preview ещё не сгенерирован). "
            f"Сначала /edit_tg {draft_post_id} правка."
        )
    return f"💬 /feedback_tg {draft_post_id} записан в tg_dispatch.feedback_note: «{_truncate(note)}»"


def _do_preview_tg(draft_post_id: int) -> str:
    """Sprint 6d.5: /preview_tg <post_id> — show the TG-channel preview
    text (what /approve_tg would send to @your_channel) inline.

    Read-only: no DB writes, no TG send. Symmetric to /preview which
    shows the WP draft body. DD uses this to verify the TG version
    before publishing — especially the clickable источник/читать
    полностью/Instant View links added in Sprint 6d.5.
    """
    import sqlite3 as _sq
    try:
        from tg_channel_trigger import format_preview_message
        from tg_regenerate import fetch_latest_tg_dispatched as fetch_latest_tg_draft  # Sprint Y rename
    except ImportError as e:
        return f"❌ /preview_tg {draft_post_id}: модуль TG-preview недоступен ({e})"

    path = PIPE.db_path
    try:
        conn = _sq.connect(path)
        conn.row_factory = _sq.Row
        try:
            post = conn.execute(
                """SELECT dp.id, dp.wp_post_url,
                          c.url AS item_url, c.title AS item_title,
                          s.name AS source_name, s.homepage_url AS source_homepage
                     FROM draft_posts dp
                LEFT JOIN candidates c ON c.id = dp.candidate_id
                LEFT JOIN sources    s ON s.id = c.source_id
                    WHERE dp.id = ?""",
                (draft_post_id,),
            ).fetchone()
            if post is None:
                return f"❌ /preview_tg {draft_post_id}: пост не найден"
        finally:
            conn.close()
    except Exception as e:
        return f"❌ /preview_tg {draft_post_id}: БД-ошибка ({e})"

    # Load latest tg_draft for this post (mirrors the format we send).
    try:
        draft = fetch_latest_tg_draft(draft_post_id, db_path=path)
    except Exception as e:
        return f"❌ /preview_tg {draft_post_id}: tg_draft load failed ({e})"

    if draft is None:
        return (
            f"❌ /preview_tg {draft_post_id}: нет tg_dispatch. "
            f"Запусти /edit_tg {draft_post_id} правка, чтобы сгенерировать."
        )

    # Build the same preview text we send to the #tg-validation topic.
    tg_draft_dict = {
        "tg_title": draft["tg_title"],
        "tg_teaser": draft["tg_teaser"],
        "tg_hashtags": json.loads(draft["tg_hashtags_json"] or "[]")
            if draft["tg_hashtags_json"] else [],
        "blocked": not (draft["tg_title"] or "").strip(),
    }
    source_url = (post["item_url"] or "").strip() or (post["source_homepage"] or "").strip()
    source_label = (post["item_title"] or "").strip() or (post["source_name"] or "").strip() or None

    text = format_preview_message(
        draft_post_id,
        tg_draft_dict,
        wp_url=(post["wp_post_url"] or "").strip() or None,
        source_url=source_url or None,
        source_label=source_label,
        # Telegraph URL is created lazily in /approve_tg \u2014 preview skips
        # it to avoid double API cost (preview + actual publish).
        telegra_url=None,
    )
    # Trim "───────────── Команды:" footer for the inline reply \u2014 it's noise
    # when DD is just checking content in DM. They already know the commands.
    cut = text.rfind("─────────────")
    if cut > 0:
        text = text[:cut].rstrip()
    return text


# Update loop
# --------------------------------------------------------------------------- #


def _handle(update: dict) -> Optional[str]:
    """Process one update. Returns the reply text, or None if nothing to say."""
    msg = update.get("message") or update.get("edited_message") or {}
    if not msg:
        return None
    text = msg.get("text") or ""
    cmd, kwargs = parse_command(text)
    if cmd == "noop":
        raw = kwargs.get("raw") or text.strip()
        # Per DD contract: if text starts with '/' but didn't parse as a
        # known command, treat it as a bad command — re-prompt with the
        # command list. We DO echo the original text so DD sees what we
        # received (important when typing on mobile).
        if raw and raw.startswith("/"):
            return (
                f"❓ не понял команду: <i>{_truncate(raw, 200)}</i>\n\n"
                "Доступно:\n"
                "  /approve &lt;draft_post_id&gt; — одобрить черновик\n"
                "  /edit &lt;draft_post_id&gt; <u>правка</u>   — перегенерировать с учётом правки\n"
                "  /preview &lt;draft_post_id&gt;         — 👁 показать полный текст черновика в чате\n"
                "  /feedback &lt;wp_post_id&gt; <u>коммент</u>   — заметка по опубликованному WP-посту\n"
                "  /feedback_tg &lt;draft_post_id&gt; <u>коммент</u> — заметка по опубликованному TG-посту\n"
                "  /help                                — эта справка"
            )
        # Plain chat text (not a command) — don't pretend to understand it.
        # In a real prod setup OpenClaw would forward this to the LLM
        # session and reply. feedback_receiver.py only handles commands.
        return None
    if cmd in ("approve", "approve_legacy"):
        reply = _do_approve(**kwargs)
        if cmd == "approve_legacy":
            reply += "\n<i>(команда /up устарела, используй /approve)</i>"
        return reply
    if cmd == "reject":
        return _do_reject(**kwargs)
    if cmd == "feedback":
        return _do_feedback(**kwargs)
    if cmd == "edit":
        return _do_edit(**kwargs)
    if cmd == "preview":
        return _do_preview(**kwargs)
    if cmd == "help":
        return _do_help()
    # --- Sprint 6: TG-channel commands ---
    if cmd == "preview_tg":
        return _do_preview_tg(**kwargs)
    if cmd == "approve_tg":
        return _do_approve_tg(**kwargs)
    if cmd == "reject_tg":
        return _do_reject_tg(**kwargs)
    if cmd == "edit_tg":
        return _do_edit_tg(**kwargs)
    if cmd == "feedback_tg":
        return _do_feedback_tg(**kwargs)
    return None


def run_once(*, timeout_s: int = 25) -> dict:
    """One poll cycle. Returns counts (received, replied, skipped, max_update_id)."""
    offset = _load_offset()
    updates = tg_bridge.get_updates(
        offset=offset, timeout_s=timeout_s,
        allowed_updates=["message", "edited_message"],
    )
    received = len(updates)
    replied = 0
    skipped = 0
    max_update_id = offset
    for u in updates:
        uid = u.get("update_id", 0)
        max_update_id = max(max_update_id, uid)
        if not _is_dd(u):
            skipped += 1
            continue
        try:
            reply_text = _handle(u)
        except Exception as e:
            log.exception("handle failed: %s", e)
            reply_text = f"❌ internal error: {e}"
        if reply_text:
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat") or {}
            tg_bridge.reply_to(
                chat_id=str(chat.get("id") or ""),
                thread_id=msg.get("message_thread_id"),
                text=reply_text,
                reply_to_message_id=msg.get("message_id"),
            )
            replied += 1
    if max_update_id >= offset:
        _save_offset(max_update_id + 1)
    return {
        "received": received, "replied": replied,
        "skipped": skipped, "max_update_id": max_update_id,
    }


def _cli_handle(text: str) -> str:
    """CLI entry point for OpenClaw skill handler subprocess (Sprint 5.5c, DD 2026-07-19).

    Accepts a raw command text (e.g. "/approve 169 привет"), parses it via
    parse_command(), dispatches to the matching _do_*(), returns the reply text.
    The OpenClaw skill handler reads stdout and ships it back to TG as a
    message reply. On any unhandled input, returns the same help text that
    feedback_webhook.py would.

    Stdout is exclusively the reply text — no log noise, no JSON envelope,
    no trailing newline past the reply itself. Logs go to stderr (so
    subprocess capture_output=True captures only the reply on stdout).
    """
    cmd, kwargs = parse_command(text)
    if cmd == "noop":
        raw = kwargs.get("raw") or text.strip()
        if raw and raw.startswith("/"):
            return (
                f"❓ не понял команду: <i>{_truncate(raw, 200)}</i>\n\n"
                "Доступно:\n"
                "  /approve &lt;draft_post_id&gt; — одобрить черновик\n"
                "  /edit &lt;draft_post_id&gt; <u>правка</u>   — перегенерировать с учётом правки\n"
                "  /preview &lt;draft_post_id&gt;         — 👁 показать полный текст черновика в чате\n"
                "  /feedback &lt;wp_post_id&gt; <u>коммент</u>   — заметка по опубликованному WP-посту\n"
                "  /feedback_tg &lt;draft_post_id&gt; <u>коммент</u> — заметка по опубликованному TG-посту\n"
                "  /help                                — эта справка"
            )
        return _do_help()
    if cmd in ("approve", "approve_legacy"):
        reply = _do_approve(**kwargs)
        if cmd == "approve_legacy":
            reply += "\n<i>(команда /up устарела, используй /approve)</i>"
        return reply
    if cmd == "reject":
        return _do_reject(**kwargs)
    if cmd == "feedback":
        return _do_feedback(**kwargs)
    if cmd == "edit":
        return _do_edit(**kwargs)
    if cmd == "preview":
        return _do_preview(**kwargs)
    if cmd == "help":
        return _do_help()
    if cmd == "preview_tg":
        return _do_preview_tg(**kwargs)
    if cmd == "approve_tg":
        return _do_approve_tg(**kwargs)
    if cmd == "edit_tg":
        return _do_edit_tg(**kwargs)
    if cmd == "feedback_tg":
        return _do_feedback_tg(**kwargs)
    return _do_help()


# --------------------------------------------------------------------------- #
# HTTP webhook server (Sprint 5.5d, DD 2026-07-19)
# --------------------------------------------------------------------------- #
#
# Routing contract (the whole point of Sprint 5.5d):
#   TG update → POST /feedback-webhook → nginx :8788 (us)
#       ├─ if message starts with /approve|/reject|/edit|/feedback|/preview|/help
#       │   ├─ /edit* → ack 'thinking' via curl subprocess (LLM rewrite is 60-120s)
#       │   └─ in-process _handle(update) → tg_bridge.reply_to()
#       └─ else (free-form text)
#           └─ urllib POST → 127.0.0.1:8787/telegram-webhook (OpenClaw plugin)
#               OpenClaw runs an LLM turn, replies via its own TG outbound.
#
# Why split slash vs free-form in PYTHON and not in OpenClaw:
#   Slash commands are deterministic — no LLM needed, 0 tokens, ~300ms.
#   Free-form is conversational — needs LLM, ~2-4s, ~700 tokens.
#   Keeping the split at the ingress means LLM availability is not on the
#   critical path for /approve /reject — DD can always moderate even if
#   M3 is down. (Was the case in Sprint 6.6 feedback loops already.)
#
# Why we don't use OpenClaw's native TG webhook handler for slash:
#   OpenClaw's handler is a long-poll channel that ALSO supports webhook
#   mode when setWebhook points at its ingress (:8787/telegram-webhook).
#   Routing slash through LLM costs ~700 tokens per /approve just to
#   select the right skill — wasted tokens for a known command. The
#   pre-LLM split here means slash never reaches the LLM at all.
#
# Why urllib (not curl subprocess, not httpx) for the free-form proxy:
#   - urllib is stdlib, no extra deps.
#   - We don't need retries or fancy timeouts — OpenClaw's ingress has
#     its own retry / dead-letter, and TG retry from the webhook is the
#     outer safety net. If urllib fails we just 500 back to TG and it
#     will redeliver. The free-form path is best-effort by design.
#   - The 401-on-:8787 bug from Sprint 5.5a (RC1) is gone: the original
#     failure was that feedback_webhook.py was sending the WRONG URL
#     (:8787/telegram-webhook) without the X-Telegram-Bot-Api-Secret-Token
#     header. Now we send the header explicitly.

import os
import json as _json_for_proxy
import urllib.request as _urlreq
import urllib.error as _urlerr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8788

# OpenClaw plugin's TG webhook ingress. It expects:
#   POST /telegram-webhook
#   Header X-Telegram-Bot-Api-Secret-Token: <TELEGRAM_WEBHOOK_SECRET>
#   Body: the original TG Update JSON (untouched).
# Source: openclaw.json channels.telegram.webhookHost/webhookPort/webhookPath
# + env TELEGRAM_WEBHOOK_SECRET.
OPENCLAW_TG_INGRESS = "http://127.0.0.1:8787/telegram-webhook"

# Bot menu entries (sent to TG via setMyCommands). Mirrors the slash command
# list so DD sees /preview /approve /reject /edit /feedback /help in the
# bot menu inside the TG app. Without this, the bot menu falls back to
# whatever scope OpenClaw registered (or empty), and DD has to type the
# slash manually. See feedback_webhook._register_tg_commands() history.
# Telegram supergroup chat_id (operator's private admin group).
# Used for the chat_administrators scope so the operator (admin) sees our
# slash commands in that specific group, regardless of what OpenClaw
# registered in all_group_chats / default. Format: -100 prefix for
# supergroups. Required env var; absent values fall back to 0 (sentinel)
# so admin-only features degrade silently rather than crashing startup.
_ADMIN_GROUP_CHAT_ID = int(os.environ.get("TELEGRAM_ADMIN_GROUP_CHAT_ID", "0") or "0")

_BOT_COMMANDS = [
    {"command": "preview",    "description": "Show full draft text (id after command)"},
    {"command": "preview_tg", "description": "Show TG-channel preview text (id after command) — Sprint 6d.5"},
    {"command": "approve",    "description": "Approve draft for publishing (id)"},
    {"command": "reject",     "description": "Mark draft as rejected (id) [optional note] — restored DD 2026-07-21 10:48"},
    {"command": "edit",       "description": "Regenerate draft with feedback (id + new text)"},
    {"command": "feedback",   "description": "Write a note to draft_posts.feedback_note for published WP post (wp_post_id + note)"},
    {"command": "approve_tg", "description": "Approve TG-channel draft; cron publish_tg ships it (id)"},
    {"command": "reject_tg",  "description": "Mark TG-channel draft as rejected_tg (id) [optional note] — restored DD 2026-07-21 10:48"},
    {"command": "edit_tg",    "description": "Regenerate TG-channel draft via master_prompt_tg.md (id + note)"},
    {"command": "feedback_tg","description": "Write a note to tg_dispatch.feedback_note (id + note)"},
    {"command": "help",       "description": "List feedback receiver commands"},
]

# Slash commands we own (everything else goes to OpenClaw LLM).
# Order matters here only for the match check — text.startswith() on each
# is enough, no overlap between these prefixes.
_FEEDBACK_COMMANDS_PREFIXES = (
    "/approve", "/approve_tg",
    "/reject",  "/reject_tg",
    "/edit",    "/edit_tg",
    "/feedback","/feedback_tg",
    "/preview", "/preview_tg",
    "/help",
)


def _is_feedback_command(text: str) -> bool:
    """Return True if text starts with one of our slash commands."""
    if not text:
        return False
    text = text.strip()
    return any(text.startswith(cmd) for cmd in _FEEDBACK_COMMANDS_PREFIXES)


def _register_bot_commands() -> None:
    """Sync our slash command list with Telegram's bot menu.

    Mirrors the legacy feedback_webhook._register_tg_commands() dance.
    Pushes to four scopes:
      - default (fallback)
      - all_private_chats (DD's ЛС)
      - all_group_chats (any group)
      - chat_administrators for DD's main <deploy-user> group (override:
        ensures our commands win over OpenClaw's in that specific group)

    The chat_administrators override exists because Telegram resolves
    BotCommandScope by specificity — chat_administrators beats all_group_chats
    beats default. DD reported his group was showing OpenClaw's default
    list; pushing our list at chat_administrators scope for that chat_id
    makes ours win regardless of what OpenClaw registered in wider scopes.
    Best-effort: any failure is logged and dropped.

    Per-scope retry (DD 2026-07-19 21:06): the chat_administrators scope
    timing out in production was masking the new /preview_tg entry from
    the <deploy-user> group menu (DD saw 10 commands instead of 11). We now retry
    each scope up to 2 times (3 attempts total) on curl-timeout before
    giving up — api.telegram.org is occasionally flaky from VPS-B.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        log.info("skip _register_bot_commands: TELEGRAM_BOT_TOKEN not set")
        return
    url = f"https://api.telegram.org/bot{bot_token}/setMyCommands"
    for scope in (
        {"type": "default"},
        {"type": "all_private_chats"},
        {"type": "all_group_chats"},
        # Override for admins of DD's main <deploy-user> group. Without this,
        # DD saw default commands in that group because OpenClaw had
        # registered its own list in all_group_chats scope.
        {"type": "chat_administrators", "chat_id": _ADMIN_GROUP_CHAT_ID},
    ):
        payload = _json_for_proxy.dumps({"commands": _BOT_COMMANDS, "scope": scope})
        # Per-scope retry: curl timeout is the most common failure from
        # VPS-B (see 17:52:15 in /var/log/<project>/telegram_receiver.log).
        # We try up to 3 times with 2s sleeps before giving up on this
        # scope — best-effort for the overall startup. The 4-scope loop
        # keeps going even if one scope permanently fails.
        import subprocess as _sp
        import time as _time
        success = False
        for attempt in range(1, 4):
            try:
                r = _sp.run(
                    ["curl", "-sS", "-X", "POST", url,
                     "-H", "Content-Type: application/json",
                     "-d", payload, "--max-time", "15"],
                    capture_output=True, text=True, timeout=20,
                )
                if r.returncode == 0 and r.stdout:
                    data = _json_for_proxy.loads(r.stdout) if r.stdout.strip() else {}
                    if data.get("ok"):
                        log.info(
                            "registered %d TG bot commands (scope=%s, attempt=%d)",
                            len(_BOT_COMMANDS), scope["type"], attempt,
                        )
                        success = True
                        break
                    else:
                        log.warning(
                            "setMyCommands failed (scope=%s, attempt=%d): %r",
                            scope["type"], attempt, data,
                        )
                else:
                    log.warning(
                        "setMyCommands curl failed (scope=%s, attempt=%d, rc=%s): %s",
                        scope["type"], attempt, r.returncode,
                        (r.stderr or "")[:200],
                    )
            except Exception as e:
                log.warning(
                    "setMyCommands exception (scope=%s, attempt=%d): %s",
                    scope["type"], attempt, e,
                )
            if attempt < 3:
                _time.sleep(2.0)
        if not success:
            log.warning(
                "setMyCommands GIVING UP (scope=%s, 3 attempts); DD may "
                "see stale commands in that scope until next restart",
                scope["type"],
            )


def _register_webhook() -> None:
    """Re-assert our /feedback-webhook URL on Telegram.

    Why: OpenClaw calls setWebhook on its own webhookUrl
    (/telegram-webhook after our fix) at every gateway restart. That
    would overwrite our webhook registration and TG would start
    delivering updates to OpenClaw's listener instead of ours.
    systemd After=openclaw-gateway.service ensures we start AFTER
    OpenClaw, so this call wins the race and TG updates flow to us.

    Best-effort: any failure is logged. systemd Restart=on-failure
    retries. If the race is lost, manual `systemctl restart
    mf-telegram-receiver` re-asserts.

    secret_token: Telegram expects the same secret_token we use on
    our HTTP listener (env TELEGRAM_WEBHOOK_SECRET). OpenClaw uses
    the same env var, so the secret matches.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not bot_token:
        log.info("skip _register_webhook: TELEGRAM_BOT_TOKEN not set")
        return
    url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    # Public-facing webhook base URL. Operator MUST set
    # TELEGRAM_WEBHOOK_BASE_URL env var (e.g. https://your-domain.tld)
    # so this matches the ingress proxy in front of telegram_receiver.
    # Default placeholder makes the registration fail loudly in prod
    # rather than silently pointing at the wrong domain.
    webhook_base = os.environ.get(
        "TELEGRAM_WEBHOOK_BASE_URL", "https://your-domain.example.com"
    ).rstrip("/")
    webhook_url = f"{webhook_base}/ingress-telegram-webhook"
    payload = _json_for_proxy.dumps({
        "url": webhook_url,
        "secret_token": secret,
        "allowed_updates": ["message", "edited_message"],
    })
    try:
        import subprocess as _sp
        r = _sp.run(
            ["curl", "-sS", "-X", "POST", url,
             "-H", "Content-Type: application/json",
             "-d", payload, "--max-time", "15"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0 and r.stdout:
            data = _json_for_proxy.loads(r.stdout) if r.stdout.strip() else {}
            if data.get("ok"):
                log.info(f"setWebhook OK: {webhook_url}")
            else:
                log.warning("setWebhook failed: %r", data)
        else:
            log.warning("setWebhook curl failed (rc=%s, stderr=%r)",
                        r.returncode, (r.stderr or "")[:200])
    except Exception as e:
        log.warning("setWebhook exception (best-effort): %s", e)


def _send_edit_ack(chat_id, thread_id, incoming_msg_id, text: str) -> None:
    """Send a quick 'LLM is thinking' ack for /edit and /edit_tg.

    Both commands trigger a 60-120s LLM rewrite via master_prompt*.md.
    Without an ack DD sees nothing for ~2 minutes. We use curl subprocess
    here too (urllib was flaky under <deploy-user>-user on VPS-B — see commit c075d7b).
    """
    import re as _re
    import subprocess as _sp
    m = _re.match(r"^/edit(?:_tg)?\s+(\d+)", text)
    if not m:
        return
    _draft_id = m.group(1)
    _is_tg = text.startswith("/edit_tg ")
    _label = "/edit_tg" if _is_tg else "/edit"
    _prompt = "master_prompt_tg.md" if _is_tg else "master_prompt.md"
    _ack = (
        f"<b>{_label} {_draft_id} принят</b>\n\n"
        f"LLM переписывает через {_prompt} (60-120 сек), жди…\n\n"
        f"Пришлю полный preview когда закончу."
    )
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return
    payload = _json_for_proxy.dumps({
        "chat_id": str(chat_id),
        "message_thread_id": thread_id,
        "text": _ack,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_to_message_id": incoming_msg_id,
    })
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        r = _sp.run(
            ["curl", "-sS", "-X", "POST", url,
             "-H", "Content-Type: application/json",
             "-d", payload, "--max-time", "10"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout:
            log.info("sent /edit ack via curl (rc=%s)", r.returncode)
        else:
            log.warning("ack curl failed (rc=%s): %s",
                        r.returncode, (r.stderr or "")[:200])
    except Exception as e:
        log.warning("ack curl exception (non-fatal): %s", e)


def _proxy_to_openclaw(raw_body: bytes, secret_token: str) -> int:
    """Forward the raw TG update JSON to OpenClaw's TG ingress.

    Returns the HTTP status code from OpenClaw (200 / 401 / 5xx). 0 on
    transport failure (urllib couldn't connect or timed out). The
    caller (HTTP handler) decides what to do with the status — we just
    relay it back to TG so TG sees a 200 only if OpenClaw accepted.
    """
    req = _urlreq.Request(
        OPENCLAW_TG_INGRESS,
        data=raw_body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": secret_token,
        },
    )
    try:
        with _urlreq.urlopen(req, timeout=30) as resp:
            return resp.status
    except _urlerr.HTTPError as e:
        log.warning("openclaw ingress HTTP %s for free-form proxy", e.code)
        return e.code
    except Exception as e:
        log.warning("openclaw ingress proxy failed: %s", e)
        return 0



def _dispatch_slash(update, chat_id, thread_id, incoming_msg_id) -> None:
    """Run _handle() and send reply to TG in a background thread.

    Mirrors the legacy feedback_webhook._handle_feedback() body but is
    invoked from a daemon thread so the HTTP handler can return 200 OK
    immediately. Any exception is logged and dropped — TG's redelivery
    covers the rare crash-and-retry case.
    """
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    try:
        reply_text = _handle(update)
        log.info("_handle returned: %r", (reply_text or "")[:120])
    except Exception as e:
        log.exception("_handle failed: %s", e)
        try:
            import tg_bridge
            tg_bridge.reply_to(
                chat_id, thread_id,
                f"internal error: {type(e).__name__}: {str(e)[:200]}",
                reply_to_message_id=incoming_msg_id,
            )
        except Exception as inner:
            log.exception("failed to send error reply to TG: %s", inner)
        return
    if not reply_text:
        log.info("handler returned None - no TG reply to send")
        return
    if not chat_id:
        log.warning("no chat_id in update - cannot reply: %r", update)
        return
    try:
        import tg_bridge
        tg_bridge.reply_to(
            chat_id, thread_id, reply_text,
            reply_to_message_id=incoming_msg_id,
        )
        log.info("sent TG reply (%d chars) to chat_id=%s",
                 len(reply_text), chat_id)
    except Exception as e:
        log.exception("tg_bridge.reply_to failed: %s", e)


def _dispatch_freeform(raw_body: bytes, secret_token: str, chat_id) -> None:
    """Proxy a free-form update to OpenClaw TG ingress in a background thread.

    Best-effort by design: failures are logged but never propagated. TG's
    webhook redelivery will retry if our 200 OK was wrong (it shouldn't
    be — we always 200 on the free-form path).
    """
    status = _proxy_to_openclaw(raw_body, secret_token)
    if status == 0:
        log.warning("free-form proxy to OpenClaw failed for chat_id=%s", chat_id)
    else:
        log.info("free-form proxy returned HTTP %s for chat_id=%s",
                 status, chat_id)


class TelegramWebhookHandler(BaseHTTPRequestHandler):
    """Single endpoint: POST /feedback-webhook (and a few aliases).

    Accepts:
        POST /            — convenience for local testing
        POST /feedback    — alias
        POST /feedback-webhook — production path (matches nginx location)
        POST /telegram-webhook — legacy alias (matches what setWebhook used
                                  to point at before Sprint 5.5d)
        GET  /            — health probe for systemd / monitoring

    Behaviour:
        - Secret header check (X-Telegram-Bot-Api-Secret-Token) using env
          TELEGRAM_WEBHOOK_SECRET. Missing or wrong → 401.
        - Slash command → in-process _handle(update) + tg_bridge.reply_to.
          We send 200 OK immediately after dispatching the handler because
          _handle returns the reply text but does NOT itself call TG; this
          layer's job is to wire handler reply → tg_bridge.
        - Free-form text → urllib POST to OpenClaw TG ingress (LLM turn).
          We return whatever status OpenClaw gave us.
    """

    def log_message(self, fmt, *args):
        # Route stdlib per-request logs through our logger so they end
        # up in /var/log/<project>/telegram_receiver.log too.
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_POST(self):  # noqa: N802 — http.server API
        if self.path not in ("/", "/ingress-telegram-webhook"):
            self.send_error(404, "unknown path; POST /ingress-telegram-webhook")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > 1_048_576:
            self.send_error(400, "bad Content-Length")
            return
        raw = self.rfile.read(length)
        try:
            update = _json_for_proxy.loads(raw)
        except Exception as e:
            log.warning("invalid JSON: %s", e)
            self.send_error(400, "invalid JSON")
            return

        expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        if expected:
            got = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if got != expected:
                log.warning("secret mismatch (got len=%d, expected len=%d)",
                           len(got), len(expected))
                self.send_error(401, "unauthorized")
                return

        msg = update.get("message") or update.get("edited_message") or {}
        text = (msg.get("text") or msg.get("caption") or "").strip()
        # Normalize group-context slash forms ('/cmd@bot ...' or
        # '/cmd ... @bot') so the rest of the pipeline sees a uniform
        # '/cmd args' shape. SAFETY: only modifies text starting with '/',
        # so '/preview' in the middle of normal speech is untouched.
        text = _normalize_tg_command(text)
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        thread_id = msg.get("message_thread_id")
        incoming_msg_id = msg.get("message_id")

        # Both branches (slash + free-form) run their outbound side effects
        # in background threads. Why: TG webhook expects 200 OK within a
        # few seconds, otherwise it retries the update. tg_bridge.reply_to
        # can take 15+ seconds when api.telegram.org is slow (curl timeout
        # is 15s + retry + urllib fallback), and the OpenClaw LLM turn is
        # 60-120s with MiniMax-M3 CoT. Doing either synchronously would
        # block TG's retry timer and cause duplicate processing on every
        # transient network blip. Fire-and-forget means the reply still
        # happens, just decoupled from the webhook ack — and TG's pending
        # queue naturally re-delivers any updates we crashed on.
        import threading as _threading

        if _is_feedback_command(text):
            log.info("FEEDBACK command: text=%r", text[:200])
            # /edit* triggers a 60-120s LLM rewrite — ack first so DD
            # doesn't stare at empty chat for two minutes. The ack is
            # also fire-and-forget because curl can hang.
            if text.startswith("/edit ") or text.startswith("/edit_tg "):
                _threading.Thread(
                    target=_send_edit_ack,
                    args=(chat_id, thread_id, incoming_msg_id, text),
                    daemon=True,
                    name="edit-ack",
                ).start()
            _threading.Thread(
                target=_dispatch_slash,
                args=(update, chat_id, thread_id, incoming_msg_id),
                daemon=True,
                name="slash-dispatch",
            ).start()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return

        # Unknown slash (text starts with `/` but not in our whitelist).
        # Don't proxy these to OpenClaw LLM — that would burn tokens on
        # confused answers like "what do you mean?". Instead route through
        # _handle() which calls parse_command() → "noop" → deterministic
        # help text ("не знаю команду, доступно: ..."). Same dispatcher
        # thread as known slash commands.
        if text.startswith("/"):
            log.info("UNKNOWN slash: text=%r", text[:200])
            _threading.Thread(
                target=_dispatch_slash,
                args=(update, chat_id, thread_id, incoming_msg_id),
                daemon=True,
                name="unknown-slash-dispatch",
            ).start()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return

        # Free-form text → OpenClaw LLM turn via the plugin's webhook ingress.
        # Proxy is also backgrounded: if OpenClaw ingress is down (port 8787
        # dead), the urllib call would otherwise tie up the handler thread.
        log.info("free-form text=%r — proxying to OpenClaw ingress", text[:200])
        _threading.Thread(
            target=_dispatch_freeform,
            args=(raw, expected, chat_id),
            daemon=True,
            name="freeform-dispatch",
        ).start()
        # Always 200 OK — OpenClaw ingress is best-effort, and TG will
        # redeliver if OpenClaw's internal queue drops the update.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"telegram_receiver OK\n")


def serve() -> None:
    # Configure logging once at server start. CLI mode (--text) stays quiet
    # on stdout/stderr because logs go to stderr only when basicConfig is
    # called, and we deliberately defer it to here so subprocess callers
    # get a clean stdout.
    import logging as _logging
    from pathlib import Path as _Path
    _log_path = _Path("/var/log/<project>/telegram_receiver.log")
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            _logging.FileHandler(_log_path),
            _logging.StreamHandler(sys.stderr),
        ],
    )
    log.info("starting telegram_receiver on %s:%d", LISTEN_HOST, LISTEN_PORT)
    # Run the TG API registration calls in a background daemon thread so
    # httpd.serve_forever() can start immediately. Without this, when
    # api.telegram.org is slow the startup blocks on curl (15s default
    # --max-time × N scopes = up to 60s), and nginx 502s every incoming
    # webhook during that window. Race-safe: registration is idempotent
    # (setWebhook + setMyCommands just overwrite), and systemd
    # Restart=on-failure will retry if anything goes wrong.
    import threading as _threading
    def _register_all():
        try:
            _register_bot_commands()
            _register_webhook()
        except Exception as e:
            log.warning("startup TG registration failed (non-fatal): %s", e)
    _threading.Thread(target=_register_all, daemon=True, name="tg-registration").start()
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), TelegramWebhookHandler)
    httpd.serve_forever()


# --------------------------------------------------------------------------- #
# CLI entry — argparse dispatch (Sprint 5.5d)
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Telegram receiver: HTTP webhook server (default) "
                    "or single-shot CLI command processor.",
    )
    ap.add_argument(
        "--serve", action="store_true", default=True,
        help="Run the HTTP webhook server on 127.0.0.1:8788 (default).",
    )
    ap.add_argument(
        "--no-serve", dest="serve", action="store_false",
        help="Disable the HTTP server mode (mostly useful for unit tests).",
    )
    ap.add_argument(
        "--text", type=str, default=None,
        help="Process a single command from TEXT and print the reply to stdout. "
             "Stdout is exclusively the reply text; logs go to stderr. "
             "Used by external tools and for debugging.",
    )
    args = ap.parse_args(argv)
    if args.text is not None:
        sys.stdout.write(_cli_handle(args.text))
        sys.stdout.flush()
        return 0
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())