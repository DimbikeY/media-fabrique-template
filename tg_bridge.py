"""Telegram observability bridge (Sprint 6.5).

One bot, one private group, four topics (message threads). All pipeline
modules call into this — never touch the Telegram HTTP API directly.
The bridge:

* Uses the Bot HTTP API via stdlib (urllib) — no extra runtime deps.
* Retries transient errors with exponential backoff (mirrors LLM/WP).
* Aggregates `feedback` events inside a short window so a flood of
  skipped candidates does not spam the chat (30 candidates in 5 minutes = 1 push).
* Runs in dry-mode when TG.chat_id is empty (CI / local without bot).

Module-level functions are the public surface:

* push_published(item, post)              -> topic #published
* push_published_tg(item, post, *, blocked_reason=None) -> topic #published_tg
* push_feedback(item, reason, llm_quote)  -> topic #feedback (rate-limited)
* push_morning_report(text)               -> topic #morning-report (1/day)
  (push_ideas removed 2026-07-20 09:26 MSK — #ideas topic retired)

Each push is best-effort: if the bot is down we log + move on. We never
let observability break the main pipeline.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

from config import TG

# DD's Telegram username (without @). Used by feedback_receiver to
# filter incoming commands — anything not from this user is ignored.
DD_USERNAME = ""  # populated lazily from TG_DD_USERNAME env in _dd_username()


def _dd_username() -> str:
    global DD_USERNAME
    if not DD_USERNAME:
        DD_USERNAME = os.getenv("TG_DD_USERNAME", "").strip()
    return DD_USERNAME

log = logging.getLogger("tg_bridge")

# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


class TGRateLimitError(RuntimeError):
    """We got HTTP 429 and exhausted retries. The pipeline should NOT
    crash — observability is best-effort."""


class TGAuthError(RuntimeError):
    """Bad bot token / chat id. Something is misconfigured; surface this."""


def _post_via_curl(method: str, payload: dict) -> dict:
    """Send one Telegram Bot API call via `curl` subprocess.

    curl is more reliable than urllib under the <deploy-user>-user on VPS-B
    (see commits c075d7b for setMyCommands and 31e8415 for /edit ack —
    both had to switch to curl). Raises the same exception types as
    _post_via_urllib so _call()'s retry/backoff logic stays uniform.
    """
    if not TG.bot_token:
        raise TGAuthError("TG.bot_token is empty; set TELEGRAM_BOT_TOKEN in .env")
    url = f"https://api.telegram.org/bot{TG.bot_token}/{method}"
    body = json.dumps(payload, ensure_ascii=False)
    try:
        result = subprocess.run(
            [
                "curl", "-sS",
                "-X", "POST", url,
                "-H", "Content-Type: application/json",
                "-d", body,
                "--max-time", str(TG.http_timeout_seconds),
            ],
            capture_output=True, text=True, timeout=TG.http_timeout_seconds + 5,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise RuntimeError(f"TG curl launch failed: {e}") from e
    if result.returncode != 0:
        raise RuntimeError(
            f"TG curl rc={result.returncode}: {(result.stderr or '')[:200]!r}"
        )
    raw = result.stdout or ""
    if not raw:
        raise RuntimeError("TG curl returned empty body")
    try:
        resp = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"TG returned non-JSON: {raw[:200]!r}") from e
    # Telegram returns HTTP 200 even for "logical" errors (ok=False),
    # but real 4xx/5xx from the API endpoint comes back through curl's
    # exit code (curl would print non-200 status into stdout, but since
    # we don't pass -f, exit code stays 0 — so we rely on ok=False).
    # Special-case the rate-limit / auth codes that come in the body
    # by inspecting error_code for parity with _post_via_urllib.
    if not resp.get("ok"):
        err_code = (resp.get("error_code") or resp.get("parameters", {}).get("retry_after"))
        # 429 — Bot API puts this in error_code when rate-limited.
        if resp.get("error_code") == 429 or err_code == 429:
            raise TGRateLimitError(f"TG 429: {resp!r}")
        if resp.get("error_code") in (401, 403):
            raise TGAuthError(f"TG auth {resp['error_code']}: {resp!r}")
        raise RuntimeError(f"TG API error: {resp!r}")
    return resp


def _post_via_urllib(method: str, payload: dict) -> dict:
    """Original urllib-based send. Kept as a fallback for _post() so
    exotic environments without curl still work."""
    if not TG.bot_token:
        raise TGAuthError("TG.bot_token is empty; set TELEGRAM_BOT_TOKEN in .env")
    url = f"https://api.telegram.org/bot{TG.bot_token}/{method}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TG.http_timeout_seconds) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise TGRateLimitError(f"TG 429: {e.read()[:200]!r}") from e
        if e.code in (401, 403):
            raise TGAuthError(f"TG auth {e.code}: {e.read()[:200]!r}") from e
        raise RuntimeError(f"TG HTTP {e.code}: {e.read()[:200]!r}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"TG connection error: {e}") from e
    try:
        resp = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"TG returned non-JSON: {raw[:200]!r}") from e
    if not resp.get("ok"):
        raise RuntimeError(f"TG API error: {resp!r}")
    return resp


def _post(method: str, payload: dict) -> dict:
    """One HTTP call to api.telegram.org. No retries here — _call() wraps it.

    Primary path is `curl` (subprocess), because urllib is flaky under the
    <deploy-user>-user on VPS-B (c075d7b, 31e8415). On any curl-side failure we
    fall back to urllib so the pipeline still works in environments where
    curl is missing or restricted. The exceptions raised are identical
    between the two paths, so _call()'s retry/backoff logic is uniform.
    """
    try:
        return _post_via_curl(method, payload)
    except TGAuthError:
        # Config problem — don't bother with urllib, the result will
        # be identical.
        raise
    except Exception as curl_exc:
        log.warning(
            "[tg] curl path failed (%s), falling back to urllib",
            curl_exc,
        )
        return _post_via_urllib(method, payload)


def _call(method: str, payload: dict) -> Optional[dict]:
    """Same as _post but with retries on transient errors."""
    if not TG.bot_token or not TG.chat_id:
        # Dry-mode: print so the dev sees what *would* have been sent.
        log.info("[tg:dry] %s %s", method, json.dumps(payload, ensure_ascii=False)[:300])
        return None
    last_exc: Optional[Exception] = None
    for attempt in range(1, TG.http_retries + 1):
        try:
            return _post(method, payload)
        except TGRateLimitError:
            # 429: back off exponentially and let the next push try again.
            time.sleep(TG.http_retry_backoff_seconds * (2 ** (attempt - 1)))
            last_exc = None
        except TGAuthError as e:
            log.error("[tg] auth/config error: %s", e)
            return None
        except Exception as e:  # network / 5xx
            last_exc = e
            time.sleep(TG.http_retry_backoff_seconds * (2 ** (attempt - 1)))
    log.warning("[tg] giving up after %d attempts: %s", TG.http_retries, last_exc)
    return None


# --------------------------------------------------------------------------- #
# Topic helpers
# --------------------------------------------------------------------------- #


def _thread_for(topic: str) -> Optional[str]:
    """Return message_thread_id for a topic, or None to skip the push."""
    return {
        "published": TG.thread_published,
        "feedback": TG.thread_feedback,
        "morning_report": TG.thread_morning_report,
        # ("ideas": TG.thread_ideas removed 2026-07-20 09:26 MSK — #ideas topic deleted)
        "published_tg": TG.thread_published_tg,
        "drafts": TG.thread_drafts,
        # Sprint 6 (channel-prompt): TG-channel "<your_channel>"
        # validation topic. Empty -> after_wp_approve() is a no-op.
        "tg_validation": TG.thread_tg_validation,
    }.get(topic) or None


def _send(topic: str, text: str, *, parse_mode: str = "HTML",
          disable_preview: bool = True) -> Optional[dict]:
    """Low-level: send one message to a topic. Returns the API response or
    None if TG isn't configured / the call failed."""
    thread_id = _thread_for(topic)
    if thread_id is None:
        log.info("[tg] topic %s disabled (thread id empty); drop message", topic)
        return None
    payload = {
        "chat_id": TG.chat_id,
        "message_thread_id": int(thread_id),
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    return _call("sendMessage", payload)


def reply_to(chat_id: str, thread_id: Optional[int], text: str,
             *, reply_to_message_id: Optional[int] = None,
             parse_mode: str = "HTML") -> Optional[dict]:
    """Send a reply in the same topic. Used by feedback_receiver so DD
    gets the answer right under their command.

    Falls back to a fresh send if reply_to_message_id is missing.
    """
    if not TG.bot_token or not chat_id:
        log.info("[tg:dry] reply %s/%s %s", chat_id, thread_id, text[:120])
        return None
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    if reply_to_message_id:
        payload["reply_to_message_id"] = int(reply_to_message_id)
    return _call("sendMessage", payload)


def get_updates(*, offset: Optional[int] = None,
                timeout_s: int = 25,
                allowed_updates: Optional[list[str]] = None) -> list[dict]:
    """Long-poll for updates. Used by feedback_receiver once per minute.

    Returns the list of Update objects (may be empty). The caller MUST
    advance `offset` past the last update_id+1 on the next call so we
    don't re-process the same message.
    """
    if not TG.bot_token:
        return []
    params: dict = {"timeout": timeout_s}
    if offset is not None:
        params["offset"] = offset
    if allowed_updates:
        params["allowed_updates"] = json.dumps(allowed_updates)
    url = f"https://api.telegram.org/bot{TG.bot_token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s + 10) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        log.warning("[tg] getUpdates failed: %s", e)
        return []
    if not data.get("ok"):
        log.warning("[tg] getUpdates returned !ok: %s", data)
        return []
    return data.get("result", [])


# --------------------------------------------------------------------------- #
# Published topic — one message per successful WP publish
# --------------------------------------------------------------------------- #


def push_published(item_row: sqlite3.Row, post_row: sqlite3.Row) -> None:
    """Called from publisher.py right after _mark_published."""
    title = (post_row["title"] or "").strip() or "(без заголовка)"
    url = post_row["wp_post_url"] or ""
    category = item_row["category"] or "—"
    weight = item_row["weight"]
    weight_str = f"{weight:.1f}" if weight is not None else "—"
    # Sprint X hotfix (DD 2026-07-20 07:55 MSK): include the Telegraph IV
    # link so the admin sees the Instant View mirror in the #published
    # preview. The column is filled by publisher.py after the TG-channel
    # auto-publish hook succeeds (only then does the telegra URL exist).
    # Without this line the preview only showed the WP canonical URL and
    # DD had no way to spot-check the IV link from the admin chat.
    telegra_url = post_row["tg_channel_telegra_url"] if "tg_channel_telegra_url" in post_row.keys() else None
    telegra_line = ""
    if telegra_url:
        telegra_line = f"\n⚡ <a href=\"{_html_escape(telegra_url)}\">Instant View</a>"
    # 1024 chars is the Telegram message limit; we keep it well under.
    text = (
        f"✅ <b>{_html_escape(title)}</b>\n"
        f"<i>{_html_escape(category)} · weight {weight_str}</i>\n"
        f"{url}"
        f"{telegra_line}"
    )
    _send("published", text)


# Outcome emojis per row — single source of truth so per-row push and
# per-tick summary stay aligned (DD 2026-07-21 20:59 MSK, Sprint Z).
_TG_OUTCOME_EMOJI: Dict[str, str] = {
    "published":         "✅",
    "blocked_retry":     "🚫",
    "blocked_exhausted": "🚫",
    "expired_skipped":   "⏭",
    "config_error":      "⚙️",
}

# Human-readable label per outcome (RU). Mirrors the run() counts dict.
_TG_OUTCOME_LABEL: Dict[str, str] = {
    "published":         "Успех",
    "blocked_retry":     "Ошибка (retry)",
    "blocked_exhausted": "Ошибка (exhausted)",
    "expired_skipped":   "Expired",
    "config_error":      "Config error",
}


def push_published_tg(
    *,
    dispatch_id: int,
    post_id: int,
    title: str,
    category: str,
    weight: Optional[float],
    outcome: str,
    attempts: int = 0,
    max_attempts: int = 0,
    failed_reason: Optional[str] = None,
    tg_message_url: Optional[str] = None,
    telegra_url: Optional[str] = None,
    wp_url: Optional[str] = "",
    latency_seconds: Optional[int] = None,
) -> None:
    """Per-row push to #published_tg topic (TG_THREAD_PUBLISHED_TG=977).

    Sprint Z (DD 2026-07-21 20:59 MSK): called from publish_tg.py:process_one
    after EACH tg_dispatch row reaches a terminal-ish outcome
    (published / blocked_retry / blocked_exhausted / expired_skipped /
    config_error). Replaces the per-tick-only summary that was sent to the
    WRONG topic (`morning_report`) in Sprint Y — DD caught the bug and asked
    for richer per-row detail on top of the tick rollup.

    Args:
        dispatch_id: tg_dispatch.id — для /reject|edit|preview_tg ссылок.
        post_id: draft_posts.id — для ссылки на WP.
        title: уже отформатированный заголовок (tg_title).
        category: candidates.category (closed-set, 12 значений).
        weight: candidates.weight (score); None если ещё не оценили.
        outcome: одно из ключей _TG_OUTCOME_EMOJI/_TG_OUTCOME_LABEL.
        attempts / max_attempts: для blocked_retry/exhausted показываем
            «(N/M)» — текущая попытка из лимита. Sprint Z: важно для DD,
            чтобы отличать «впервые фейл» от «уже 5 попыток всё мертво».
        failed_reason: tg_dispatch.failed_reason (для error rows). Truncated
            до 200 символов в сообщении (Telegram лимит 4096, но summary
            per-row должен быть компактным).
        tg_message_url: для published — ссылка на сообщение в @your_channel.
        telegra_url: для published — Instant View mirror.
        wp_url: для контекста, fallback если нет tg_message_url.
        latency_seconds: секунды от approved_at до attempted_at. None если
            не вычислимо (например, expired_skipped). Для blocked_retry
            это время ДО провала — полезно для отладки «почему так долго?».

    Best-effort: failure to push to #published_tg never blocks the pipeline
    (same contract as push_published).
    """
    emoji = _TG_OUTCOME_EMOJI.get(outcome, "❓")
    label = _TG_OUTCOME_LABEL.get(outcome, outcome)
    weight_str = f"{weight:.1f}" if weight is not None else "—"

    parts: list[str] = []
    # Header line: emoji + outcome label + attempts counter (if applicable)
    if outcome in ("blocked_retry", "blocked_exhausted") and max_attempts:
        header = f"{emoji} <b>{_html_escape(label)}</b> · <b>{_html_escape(title)}</b> ({attempts}/{max_attempts})"
    else:
        header = f"{emoji} <b>{_html_escape(label)}</b> · <b>{_html_escape(title)}</b>"
    parts.append(header)
    # Sub-header: category + weight + dispatch_id + latency
    meta_bits: list[str] = [
        f"cat={_html_escape(category or '—')}",
        f"weight {weight_str}",
        f"#{dispatch_id}",
    ]
    if latency_seconds is not None:
        meta_bits.append(f"latency {latency_seconds}s")
    parts.append(f"<i>{' · '.join(meta_bits)}</i>")
    # Body: links or reason
    if outcome == "published":
        if tg_message_url:
            parts.append(f"<a href=\"{_html_escape(tg_message_url)}\">→ @your_channel</a>")
        elif wp_url:
            parts.append(_html_escape(wp_url))
        if telegra_url:
            parts.append(f"⚡ <a href=\"{_html_escape(telegra_url)}\">Instant View</a>")
    elif outcome in ("blocked_retry", "blocked_exhausted", "config_error"):
        if failed_reason:
            # Truncate to 200 chars to keep summary readable; full reason
            # stays in tg_dispatch.failed_reason (400 char cap there too).
            reason = failed_reason if len(failed_reason) <= 200 else failed_reason[:197] + "..."
            parts.append(f"<b>Причина:</b> <code>{_html_escape(reason)}</code>")
        if wp_url:
            parts.append(_html_escape(wp_url))
        if outcome == "blocked_exhausted":
            parts.append("<i>Требует ручной проверки</i>")
        elif outcome == "config_error":
            parts.append("<i>Проверь .env</i>")
    elif outcome == "expired_skipped":
        if failed_reason:
            parts.append(f"<code>{_html_escape(failed_reason)}</code>")
        if wp_url:
            parts.append(_html_escape(wp_url))

    text = "\n".join(parts)
    _send("published_tg", text)


def push_published_tg_summary(
    *,
    counts: Dict[str, int],
    runtime_seconds: float,
    queue_remaining: int,
    unique_failed_reasons: Dict[str, int],
    iso_timestamp: str,
) -> None:
    """Per-tick rollup to #published_tg topic.

    Sprint Z (DD 2026-07-21 20:59 MSK): финальное сообщение tick=publish_tg
    в том же топике, что и per-row пуши. Содержит:
      - timestamp + runtime (сколько крутился tick)
      - queue_remaining: сколько ещё в tg_dispatch WHERE status='approved'
        AND attempts<MAX (полезно — если растёт, а published=0 → бьём тревогу)
      - counts per outcome (✅ Успех / 🚫 Ошибка / ⏭ Expired / ⚙️ Config)
      - агрегированные unique_failed_reasons (классифицированные по
        «префиксу до ': '» — например «publish_error», «publish_blocked»,
        «config_error»). Не выводим если dict пустой.

    Best-effort: failure to push never blocks the pipeline.
    """
    lines: list[str] = []
    lines.append(
        f"📊 <b>tick=publish_tg</b> · {_html_escape(iso_timestamp)}Z · "
        f"⏱ {runtime_seconds:.1f}s · queue={queue_remaining}"
    )
    # Order matches the run() counts dict declaration for visual consistency.
    for outcome in ("published", "blocked_retry", "blocked_exhausted",
                    "expired_skipped", "config_error"):
        n = counts.get(outcome, 0)
        if n == 0:
            continue
        emoji = _TG_OUTCOME_EMOJI.get(outcome, "·")
        label = _TG_OUTCOME_LABEL.get(outcome, outcome)
        lines.append(f"{emoji} {_html_escape(label)}: {n}")
        # Aggregated failed_reason breakdown (only for retry/exhausted)
        if outcome in ("blocked_retry", "blocked_exhausted", "config_error") and unique_failed_reasons:
            for reason, cnt in sorted(unique_failed_reasons.items(),
                                       key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"   · {_html_escape(reason)} (×{cnt})")
    text = "\n".join(lines)
    _send("published_tg", text)


# --------------------------------------------------------------------------- #
# Feedback topic — aggregated push with rate-limit
# --------------------------------------------------------------------------- #

# We debounce feedback pushes so 30 simultaneous skipped candidates do not
# produce 30 messages. The aggregator key is (safety_status) and we
# flush at most once per TG.feedback_aggregation_window_s.
@dataclass
class _PendingFeedback:
    reason: str
    samples: list[tuple[int, str]]   # (candidate_id, title)
    first_at: float


_feedback_lock = threading.Lock()
_pending_feedback: dict[str, _PendingFeedback] = {}


def push_feedback(candidate_id: int, title: str, reason: str,
                  llm_quote: Optional[str] = None) -> None:
    """Called from rewrite_and_score.py when an item is blocked/skipped.

    Aggregates into a single message per (reason, window). The flush is
    triggered by either (a) the next push for a different reason, or
    (b) an explicit flush_feedback_now() at the end of a tick."""
    now = time.time()
    bucket = _pending_feedback.setdefault(
        reason,
        _PendingFeedback(reason=reason, samples=[], first_at=now),
    )
    if len(bucket.samples) < 5:  # cap the sample list inside one push
        bucket.samples.append((candidate_id, title[:120]))
    # If we've crossed the window with this same reason, flush now.
    if now - bucket.first_at >= TG.feedback_aggregation_window_s:
        flush_feedback_now()


def flush_feedback_now() -> None:
    """Push all pending feedback candidates as a single message per reason,
    then clear the bucket. Safe to call multiple times."""
    with _feedback_lock:
        buckets = list(_pending_feedback.items())
        _pending_feedback.clear()
    if not buckets:
        return
    for reason, bucket in buckets:
        lines = [f"🚫 <b>{_html_escape(reason)}</b> · {len(bucket.samples)}+ item(s)"]
        for candidate_id, title in bucket.samples:
            lines.append(f"  · #{candidate_id} {_html_escape(title)}")
        _send("feedback", "\n".join(lines))


# --------------------------------------------------------------------------- #
# Morning report topic — called once per day by morning_report.py
# --------------------------------------------------------------------------- #


def push_morning_report(text: str) -> Optional[dict]:
    """Whole report as a single HTML message. Caller formats it."""
    return _send("morning_report", text)


# push_ideas() removed 2026-07-20 09:26 MSK — TG #ideas topic deleted.
# Anyone importing it will now get ImportError; that's intentional.

# --------------------------------------------------------------------------- #
# Feedback receiver — Telegram -> SQLite (DD's verdicts)
# --------------------------------------------------------------------------- #


def record_feedback(candidate_id: int, verdict: str, note: Optional[str] = None,
                    db_path: Optional[Path] = None) -> None:
    """DEPRECATED (Sprint 6.6). The candidates.feedback_* columns were
    dropped in migration 015; verdicts now live on draft_posts and
    free-form signals live in feedback_signals. Kept here only so that
    legacy test code that imports it doesn't break with an import error.
    Production code uses record_review() instead."""
    raise NotImplementedError(
        "record_feedback is deprecated as of Sprint 6.6. "
        "Use record_review() to approve/reject a draft_post."
    )


def record_review(draft_post_id: int, verdict: str, note: Optional[str] = None,
                  reviewer: Optional[str] = None,
                  db_path: Optional[Path] = None) -> str:
    """Atomically transition draft_posts.status: draft -> approved|rejected.

    Sprint 6.6.1 renamed the verb vocabulary from ``up``/``down`` to
    ``approve``/``reject`` to match the user-facing Telegram command
    names. The stored status values stay ``approved``/``rejected``
    because that's the canonical state name in the state machine.

    Returns the *previous* status as a string ('draft' if the transition
    happened, 'approved' / 'rejected' if it was already terminal — i.e.
    a no-op replay — or 'missing' if the id doesn't exist). The caller
    uses this to format a Telegram reply that tells DD what happened.

    Side effects: if note is non-empty, INSERT one row into
    feedback_signals (always, regardless of state transition outcome —
    so DD's notes are never lost even on a replay).
    """
    if verdict not in ("approve", "reject"):
        raise ValueError(f"verdict must be 'approve' or 'reject', got {verdict!r}")
    new_status = "approved" if verdict == "approve" else "rejected"
    path = db_path or Path(__file__).parent / "data" / "news_memory.db"
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        prev_row = conn.execute(
            "SELECT status FROM draft_posts WHERE id = ?", (draft_post_id,)
        ).fetchone()
        if prev_row is None:
            conn.commit()
            return "missing"
        prev_status = prev_row["status"]
        # Only transition from 'draft' — never overwrite a terminal state.
        # (Replays are silently accepted as no-ops so DD's commands are
        # idempotent, but we don't lose feedback_signals rows.)
        if prev_status == "draft":
            conn.execute(
                """UPDATE draft_posts
                      SET status      = ?,
                          reviewed_at = datetime('now'),
                          reviewed_by = COALESCE(?, reviewed_by),
                          review_note = COALESCE(?, review_note)
                    WHERE id = ? AND status = 'draft'""",
                (new_status, reviewer, note, draft_post_id),
            )
        conn.commit()
    return prev_status


def record_edit(draft_post_id: int, new_data: dict,
                feedback: str,
                reviewer: Optional[str] = None,
                db_path: Optional[Path] = None) -> str:
    """Sprint 6m.2: overwrite an existing draft_post with LLM-revised content.

    ``new_data`` is the JSON dict returned by ``LLMClient.edit_post()`` —
    same JSON contract as the rewriter output (title, content, excerpt,
    slug, meta_*, image_alt, image_prompt, categories, tags, telegram_teaser).
    Status stays ``draft``; only the text fields are updated. The previous
    version is **overwritten** (no revisions table — Sprint 6m.2 contract:
    "перезатираем, незачем хранить историю").

    Feedback is appended to ``feedback_signals`` with kind='edited' so the
    digest tick can later aggregate edit-patterns ("DD часто просит
    убрать перечисления", etc.).

    Returns one of:
      * ``'updated'``  — draft was overwritten; status remained 'draft'.
      * ``'missing'``  — no draft_post with that id.
      * ``'terminal'`` — draft_posts.status is not 'draft' (already
                        approved / publishing / published / rejected /
                        failed). We refuse to overwrite terminal state.
    """
    path = db_path or Path(__file__).parent / "data" / "news_memory.db"
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM draft_posts WHERE id = ?", (draft_post_id,)
        ).fetchone()
        if row is None:
            conn.commit()
            return "missing"
        prev_status = row["status"]
        if prev_status != "draft":
            conn.commit()
            return "terminal"
        # Overwrite content fields. Keep candidate_id, slug (slug is part of
        # the canonical WP URL — only rewrite_and_score sets it on first
        # creation; edits keep the same slug).
        try:
            conn.execute(
                """
                UPDATE draft_posts
                   SET title              = ?,
                       excerpt            = ?,
                       content_html       = ?,
                       meta_title         = ?,
                       meta_description   = ?,
                       image_alt          = ?,
                       image_prompt       = ?,
                       categories_json    = ?,
                       tags_json          = ?,
                       telegram_teaser    = ?,
                       updated_at         = datetime('now')
                 WHERE id = ? AND status = 'draft'
                """,
                (
                    (new_data.get("title") or "")[:500],
                    (new_data.get("excerpt") or "")[:1000],
                    new_data.get("content") or new_data.get("content_html") or "",
                    (new_data.get("meta_title") or "")[:500],
                    (new_data.get("meta_description") or "")[:1000],
                    (new_data.get("image_alt") or "")[:500],
                    (new_data.get("image_prompt") or "")[:1000],
                    json.dumps(new_data.get("categories") or [],
                               ensure_ascii=False),
                    json.dumps(new_data.get("tags") or [],
                               ensure_ascii=False),
                    (new_data.get("telegram_teaser") or "")[:1000],
                    draft_post_id,
                ),
            )
        except sqlite3.Error as e:
            conn.rollback()
            raise RuntimeError(f"record_edit UPDATE failed: {e}") from e
        # Persist the edit feedback into draft_posts.feedback_note for
        # future manual review (Sprint cleanup 2026-07-21: feedback_signals
        # table dropped; feedback_note is the single retention column).
        if feedback:
            try:
                conn.execute(
                    """UPDATE draft_posts
                          SET feedback_note = ?,
                              updated_at    = datetime('now')
                        WHERE id = ? AND status = 'draft'""",
                    (feedback[:2000], draft_post_id),
                )
            except sqlite3.Error:
                # Best-effort: if feedback_note write fails we still
                # keep the edit (don't roll back the overwriting UPDATE).
                pass
        conn.commit()
    return "updated"


def add_post_feedback(wp_post_id: int, note: Optional[str] = None,
                      reviewer: Optional[str] = None,
                      db_path: Optional[Path] = None) -> str:
    """Sprint cleanup 2026-07-21: write free-form note into
    ``draft_posts.feedback_note`` for WP-published posts.

    ``wp_post_id`` is the WordPress post id (the integer from ``?p=17``
    in the permalink). We look it up in ``draft_posts`` and overwrite
    feedback_note there. No automation, no digest — DD's note is just
    persisted for future manual review.

    Returns one of:
      * ``'attached'`` — wp_post_id found in draft_posts, note stored.
      * ``'missing'``  — wp_post_id not in draft_posts; nothing is
                         written. Caller's reply tells DD "post not found".
    """
    path = db_path or Path(__file__).parent / "data" / "news_memory.db"
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id FROM draft_posts WHERE wp_post_id = ?", (wp_post_id,)
        ).fetchone()
        if row is None:
            conn.commit()
            return "missing"
        draft_post_id = row["id"]
        if note:
            conn.execute(
                """UPDATE draft_posts
                      SET feedback_note = ?
                    WHERE id = ?""",
                (note[:2000], draft_post_id),
            )
        conn.commit()
    return "attached"


def record_tg_feedback_note(draft_post_id: int, note: Optional[str] = None,
                             db_path: Optional[Path] = None) -> str:
    """Sprint cleanup 2026-07-21: write free-form note into
    ``tg_dispatch.feedback_note`` for a TG-channel published post.

    ``draft_post_id`` is the local draft id (the row in draft_posts).
    We look up the latest tg_dispatch row for that post and overwrite
    feedback_note there. The dispatcher table is a 1-to-many history
    of generated TG texts; we always attach to the most recent one.

    Returns one of:
      * ``'attached'`` — tg_dispatch row found, note stored.
      * ``'missing'``  — no tg_dispatch row for this post yet (TG text
                         not generated). Nothing written.
    """
    if not note:
        return "attached"  # nothing to do, treat as no-op success
    path = db_path or Path(__file__).parent / "data" / "news_memory.db"
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT id FROM tg_dispatch
                WHERE post_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1""",
            (draft_post_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return "missing"
        conn.execute(
            """UPDATE tg_dispatch
                  SET feedback_note = ?,
                      updated_at    = datetime('now')
                WHERE id = ?""",
            (note[:2000], row["id"]),
        )
        conn.commit()
    return "attached"


def push_draft_preview(draft_post_id: int, title: str, wp_preview_url: str,
                       weight: Optional[float] = None,
                       category: Optional[str] = None) -> Optional[dict]:
    """Send a preview of a new draft_post into the #drafts topic so DD
    can review and /approve or /reject it. wp_preview_url is the admin
    edit URL on WordPress (we don't have a public preview token at this
    stage — DD's an admin, so the admin URL works).

    Format mirrors push_published() for symmetry.
    """
    title = (title or "").strip() or "(без заголовка)"
    weight_str = f"{weight:.1f}" if weight is not None else "—"
    cat_str = _html_escape(category or "—")
    text = (
        f"📝 <b>{_html_escape(title)}</b>\n"
        f"<i>{cat_str} · weight {weight_str}</i>\n"
        f"{wp_preview_url}\n"
        f"\n"
        f"<code>/preview {draft_post_id}</code> — 👁 посмотреть полный текст\n"
        f"<code>/approve {draft_post_id} {{note}}</code> — одобрить и опубликовать\n"
        f"<code>/reject {draft_post_id} {{note}}</code> — отклонить\n"
        f"<code>/feedback {{wp_post_id}} {{note}}</code> — обратная связь по опубликованному посту"
    )
    return _send("drafts", text)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _html_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )