"""Sprint 6: publish curated TG drafts to @your_channel.
Sprint 6d.5 added: 2 clickable source links + Telegraph Instant View integration.

Public API:
  - publish(post_id) -> dict
        Take the latest tg_dispatch row for a post, format it, send via Bot API
        to TG.your_channel_channel_id, mark draft_posts.tg_channel_published_at.

  - format_tg_post(draft_row, *, max_title_len=80, max_teaser_len=500,
                   source_url=None, source_label=None,
                   wp_url=None, telegra_url=None) -> str
        Assemble the final Telegram post:
            🔴 <b>{tg_title}</b>

            {tg_teaser}

            #tag1 #tag2 #tag3

            🔗 <a href="{source_url}">Источник</a> · <a href="{wp_url}">Читать полностью</a>
            ⚡ <a href="{telegra_url}">Instant View</a>  (only if Telegraph API succeeded)

        Backward compat: if source_url/wp_url/telegra_url are None, falls
        back to a literal "🔗 Источник" marker (no link). Pre-Sprint-6d.5
        callers that don't pass these still get valid output.

  - build_utm_url(wp_url, post_id) -> str
        Append UTM tags so Я.Метрика attributes the visit.

  - AlreadyPublished exception for idempotent re-attempts.

Idempotency:
  - publish() refuses to send if draft_posts.tg_channel_published_at IS NOT NULL
    (returns the existing message_id/URL instead, raises AlreadyPublished so
    callers can decide whether to treat as success or as "skip").
  - mark_tg_channel_published() is also idempotent (UPDATE ... WHERE ... IS NULL).

Telegraph (Instant View) integration (Sprint 6d.5):
  - publish() creates a mirror page on telegra.ph via telegraph.create_telegraph_page().
  - Telegraph URL takes priority over WP URL in link_preview_options so TG renders
    Instant View. WP URL still appears in the visible footer for canonical attribution.
  - If TELEGRA_PH_ACCESS_TOKEN is unset, Telegraph is skipped gracefully and we
    fall back to WP URL in link_preview_options (no IV, but preview-card still works).

Why a separate module (not in tg_bridge.py):
  - tg_bridge is for *observability* (DD's private group). This is for
    *publication* (public channel). Different intent, different audience,
    different config keys. Keeping them separate avoids accidentally
    leaking a draft preview to the public channel.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from loguru import logger

import tg_bridge
import telegraph
from config import TG, PIPE, WP
from tg_regenerate import (
    PostNotFound,
    fetch_latest_tg_dispatched as fetch_latest_tg_draft,  # Sprint Y rename
    mark_tg_channel_published,
)


# --- Exceptions --------------------------------------------------------------
class AlreadyPublished(RuntimeError):
    """post_id was already published to @your_channel.

    Carries the existing message_id + URL so the caller can decide to
    silently succeed, surface a link, or treat as an error.
    """

    def __init__(self, post_id: int, message_id: int, message_url: str):
        super().__init__(
            f"post_id={post_id} already published to "
            f"{TG.your_channel_username}: message_id={message_id}"
        )
        self.post_id = post_id
        self.message_id = message_id
        self.message_url = message_url


class TGChannelConfigError(RuntimeError):
    """TG.your_channel_channel_id is not set or bot_token missing."""


# --- Sprint 6d.6 helpers -----------------------------------------------------
def _html_to_paragraphs(html_content: str) -> str:
    """Convert WordPress content_html to Telegraph-friendly plain text.

    Telegraph accepts Telegraph Nodes (its own subset of tags) in children
    arrays — NOT raw HTML. So we strip tags and normalize whitespace while
    preserving paragraph breaks. The resulting string is split on '\\n\\n'
    inside telegraph.create_telegraph_page into one <p> per paragraph.

    Strip rules:
      - <p>...</p>           → emits a paragraph break (\\n\\n)
      - <br>, <br/>, <br />  → emits a line break (\\n)
      - any other tag        → dropped (its text content kept)
      - &nbsp; / entities    → decoded to plain Unicode
      - trailing 'Источник: ...'  paragraph dropped (publisher.py legacy
        attribution; Telegraph footer replaces it)

    This is lossy by design (Telegra.ph doesn't render tables, lists, or
    inline styles), but for news posts it's adequate — text reads as a
    clean article.
    """
    if not html_content:
        return ""
    # Decode common entities (Telegraph expects raw Unicode in children).
    # Use stdlib html.unescape; no need for a full HTML parser here.
    import html as _html_mod
    text = _html_mod.unescape(html_content)

    # Paragraph breaks: each <p>...</p> becomes a literal \\n\\n block.
    # We use a simple regex; we don't need a parser since WP content is
    # well-formed (publisher.py produces it via str.format).
    import re as _re
    text = _re.sub(r"</p\s*>", "\n\n", text, flags=_re.IGNORECASE)
    text = _re.sub(r"<p[^>]*>", "", text, flags=_re.IGNORECASE)
    # <br> → single newline
    text = _re.sub(r"<br\s*/?>", "\n", text, flags=_re.IGNORECASE)
    # Strip remaining tags
    text = _re.sub(r"<[^>]+>", "", text)
    # Drop a trailing 'Источник: ...' paragraph (if present) — publisher.py
    # has been appending it inside content_html since early sprints; we
    # now handle source attribution via the Telegraph footer link instead.
    # content_html ends with multiple \n (paragraph break tail), which
    # creates an empty trailing element when split on \n\n — normalize
    # first, then drop blocks starting with 'Источник:' at the tail.
    text = text.rstrip()
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    # Strip trailing 'Источник:' attribution block(s).
    while paragraphs and paragraphs[-1].strip().startswith("Источник:"):
        paragraphs.pop()
    text = "\n\n".join(paragraphs)
    # Normalize consecutive spaces
    text = _re.sub(r"[ \t]+", " ", text)
    # Trim per-line
    lines = [line.strip() for line in text.split("\n")]
    # Collapse repeated blank lines
    out: list[str] = []
    prev_blank = False
    for line in lines:
        is_blank = not line
        if is_blank and prev_blank:
            continue
        out.append(line)
        prev_blank = is_blank
    return "\n".join(out).strip()


def _get_wp_featured_media_url(wp_post_id: int) -> Optional[str]:
    """Pull featured_media.source_url from WordPress REST API.

    Returns the public https URL of the post's featured image (uploaded to
    WP during publisher.py:publish). Returns None if the post has no
    featured_media or the fetch fails. We do not retry — Telegraph
    gracefully renders without an image.
    """
    import json as _json
    import urllib.request as _urlreq
    import urllib.error as _urlerr
    base_url = WP.base_url.rstrip("/")
    if not base_url:
        return None
    # First: get featured_media ID
    url_1 = f"{base_url}/wp-json/wp/v2/posts/{wp_post_id}?_fields=featured_media"
    req_1 = _urlreq.Request(
        url_1,
        headers={
            "Authorization": "Basic " + _b64(f"{WP.username}:{WP.app_password}"),
        },
    )
    try:
        with _urlreq.urlopen(req_1, timeout=10) as r:
            data_1 = _json.loads(r.read())
    except (_urlerr.URLError, _urlerr.HTTPError, TimeoutError) as e:
        logger.debug("wp featured_media id fetch failed: {}", e)
        return None
    fm_id = data_1.get("featured_media")
    if not fm_id:
        return None
    # Second: get the source_url
    url_2 = f"{base_url}/wp-json/wp/v2/media/{fm_id}?_fields=source_url"
    req_2 = _urlreq.Request(
        url_2,
        headers={
            "Authorization": "Basic " + _b64(f"{WP.username}:{WP.app_password}"),
        },
    )
    try:
        with _urlreq.urlopen(req_2, timeout=10) as r:
            data_2 = _json.loads(r.read())
    except (_urlerr.URLError, _urlerr.HTTPError, TimeoutError) as e:
        logger.debug("wp source_url fetch failed: {}", e)
        return None
    return data_2.get("source_url")


# Helper for _get_wp_featured_media_url — base64 of "user:pass" for Basic auth.
import base64 as _base64_mod
def _b64(s: str) -> str:
    return _base64_mod.b64encode(s.encode("utf-8")).decode("ascii")



def build_utm_url(wp_url: str, post_id: int) -> str:
    """Append UTM tags so Я.Метрика attributes the visit to TG-channel.

    Args:
        wp_url: the bare https://your-domain/<slug> URL from draft_posts.
        post_id: draft_posts.id — used as utm_content so each post gets
            its own line in Метрика reports.

    Returns:
        The same URL with utm_source=telegram_channel, utm_medium=post,
        utm_campaign=your_channel, utm_content=<post_id> appended (or merged
        if the URL already has a ?query string).
    """
    if not wp_url:
        return wp_url
    parts = urlsplit(wp_url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q["utm_source"] = "telegram_channel"
    q["utm_medium"] = "post"
    q["utm_campaign"] = "your_channel"
    q["utm_content"] = str(post_id)
    new_query = urlencode(q, doseq=True)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


# --- Formatting --------------------------------------------------------------
def format_tg_post(
    draft_row: sqlite3.Row,
    *,
    max_title_len: int = 80,
    max_teaser_len: int = 500,
    source_url: Optional[str] = None,
    source_label: Optional[str] = None,
    wp_url: Optional[str] = None,
    telegra_url: Optional[str] = None,
) -> str:
    """Assemble the final TG-channel post from a tg_dispatch row.

    Layout (5 paragraphs in the typical case, Sprint 6d.7):
        🔴 <b>{tg_title}</b>

        {tg_teaser}

        #tag1 #tag2 #tag3

        🔗 Источник: <a href="{source_url}">{label}</a>   (own paragraph)

        Наше медиа-платформа: <a href="{wp_url}">{_DISPLAY_DOMAIN}</a>   (own paragraph)
        ⚡ <a href="{telegra_url}">Instant View</a>   (own paragraph; optional, only when telegra_url)

    Sprint X hotfix (DD 2026-07-20 08:28 MSK) re-introduced the inline
    '⚡ Instant View' link after Sprint 6d.9 removed it — readers in
    web.telegram.org and third-party clients miss TG's auto-rendered IV
    overlay, so an explicit text link complements (does not replace) the
    client-side IV button.

    Sprint 6d.7 (DD 2026-07-19 21:21): the source and WP links are no
    longer glued with ' · ' on one line — each gets its own paragraph
    with an explicit prefix label. "Наше медиа-платформа" replaces
    "Читать полностью" so readers see a brand-level anchor before
    clicking, not a generic CTA.

    All three footer URLs are optional. If source_url+wp_url are both None,
    we fall back to the pre-6d.5 literal "🔗 Источник" marker so callers
    that don't track source/wp URLs (tests, drafts without candidate_id)
    still get a valid post.

    parse_mode is HTML (Telegram Bot API). We escape <, >, & in the LLM
    output defensively — the model can emit HTML-like characters in titles
    ("OpenAI <3 Anthropic") and TG would try to parse them as tags.
    """
    title = (draft_row["tg_title"] or "").strip()
    teaser = (draft_row["tg_teaser"] or "").strip()
    hashtags_raw = draft_row["tg_hashtags_json"] or "[]"

    try:
        hashtags = json.loads(hashtags_raw)
        if not isinstance(hashtags, list):
            hashtags = []
    except (TypeError, ValueError):
        logger.warning(
            "tg_dispatch id={} has invalid hashtags_json, dropping tags",
            draft_row["id"],
        )
        hashtags = []

    # Defensive: TG has a 4096-char limit per message. We won't normally
    # come close (master_prompt_tg.md caps title at 80, teaser at 500),
    # but cap defensively so a runaway LLM output can't 400 the API.
    title = title[:max_title_len]
    teaser = teaser[:max_teaser_len]

    parts: list[str] = []
    if title:
        parts.append(f"🔴 <b>{_html_escape(title)}</b>")
    parts.append(_html_escape(teaser))

    if hashtags:
        # Tag wrapping: #tag1 — but escape in case a tag contains HTML-ish chars.
        rendered_tags = " ".join(f"#{_html_escape(str(t))}" for t in hashtags if t)
        if rendered_tags:
            parts.append(rendered_tags)

    # Sprint 6d.7 + 6d.8: footer split into separate paragraphs with
    # explicit prefix labels (was Sprint 6d.5 inline-with-dot, but DD
    # found it hard to scan visually — two links on the same line
    # blurred together). 6d.8 (DD 2026-07-19 21:36) renames:
    #   - 'Источник' → 'Первоисточник' (more precise)
    #   - 'Наше медиа-платформа' → 'Наша медиаплатформа' (drop hyphen)
    # Telegraph mirror footer is a separate concern (see below).
    if source_url:
        label = _html_escape(source_label or source_url)
        parts.append(
            f"🔗 Первоисточник: <a href=\"{_html_escape(source_url)}\">{label}</a>"
        )
    if wp_url:
        # 'Наша медиаплатформа' as the clickable prefix label — anchors
        # the link in the channel's identity rather than a generic CTA.
        # URL itself stays the destination.
        parts.append(
            f"Наша медиаплатформа: <a href=\"{_html_escape(wp_url)}\">{_DISPLAY_DOMAIN}</a>"
        )

    if not source_url and not wp_url:
        # Legacy fallback: literal marker (no link).
        parts.append("🔗 Источник")
    # NOTE (6d.9): the explicit '⚡ Instant View' link is NOT included in
    # the message body. Telegram renders the Instant View overlay
    # automatically via link_preview_options (set in publish() below)
    # inside every modern TG client (Android, iOS, web.telegram.org,
    # desktop). A textual link duplicates that visual cue and adds
    # noise to every post. Sprint X commit 5371739 briefly re-added it
    # during the #published preview work, but DD 2026-07-20 08:32 MSK
    # caught the duplication and asked to remove it again — the
    # overlay-only behaviour from commit 87b9f8f (Sprint 6d.9) is the
    # final shape. The ⚡ Instant View line DOES still appear in the
    # #published admin preview (push_published in tg_bridge.py) where
    # it helps DD spot-check the IV link.



    # Join with blank lines (TG renders single \n as a space within a paragraph;
    # blank lines are how you actually break paragraphs).
    return "\n\n".join(parts)


def _html_escape(s: str) -> str:
    """Minimal HTML escaping for TG parse_mode=HTML.

    Telegram parses a small subset of HTML: <b>, <i>, <u>, <s>, <code>, <pre>,
    <a href="...">. Anything else (including stray '<' or '&' in the LLM
    output) gets the message rejected. We escape the three dangerous chars.
    """
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --- Main entry point --------------------------------------------------------
def publish(
    post_id: int,
    *,
    db_path: Optional[Path] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Publish the latest tg_draft for a post to @your_channel.

    Args:
        post_id: draft_posts.id.
        db_path: optional isolated DB path (for tests). Defaults to PIPE.db_path.
        force: if True, republish even if tg_channel_published_at is set.
            Use sparingly — there's no good reason for production callers
            to pass force=True. (Tests use it for the "already published
            returns existing" assertion by setting it back to False.)

    Returns:
        dict with keys: post_id, message_id, message_url, chat_id,
        tg_draft_id, blocked, dry_run.

    Raises:
        AlreadyPublished: when post was already published and force=False.
        PostNotFound: when no draft_posts row exists.
        TGChannelConfigError: when TG.your_channel_channel_id or TG.bot_token is empty.
        RuntimeError: on Bot API failure (propagated from tg_bridge._call).
    """
    if not TG.your_channel_channel_id:
        raise TGChannelConfigError(
            "TG.your_channel_channel_id is empty; set TG_CHANNEL_ID in .env"
        )
    if not TG.bot_token:
        raise TGChannelConfigError(
            "TG.bot_token is empty; set TELEGRAM_BOT_TOKEN in .env"
        )

    path = Path(db_path) if db_path is not None else PIPE.db_path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        # Load the post (raises PostNotFound if missing).
        # Sprint 6d.5: JOIN candidates + sources to get the original
        # article URL (источник) and source homepage fallback.
        # Sprint 6d.6: also pull full WP body (content_html) and the
        # local featured_image_path, so Telegraph mirror gets the WHOLE
        # post text (not just teaser) and the real featured image.
        post = conn.execute(
            """SELECT dp.id, dp.wp_post_url, dp.wp_post_id,
                      dp.content_html, dp.featured_image_path,
                      dp.tg_channel_published_at,
                      dp.tg_channel_message_id, dp.tg_channel_message_url,
                      c.url AS item_url, c.title AS item_title,
                      s.name AS source_name, s.homepage_url AS source_homepage
                 FROM draft_posts dp
            LEFT JOIN candidates c ON c.id = dp.candidate_id
            LEFT JOIN sources    s ON s.id = c.source_id
                WHERE dp.id = ?""",
            (post_id,),
        ).fetchone()
        if post is None:
            raise PostNotFound(f"draft_posts id={post_id} not found")

        # Idempotency gate.
        if not force and post["tg_channel_published_at"] is not None:
            raise AlreadyPublished(
                post_id=post_id,
                message_id=int(post["tg_channel_message_id"]),
                message_url=post["tg_channel_message_url"] or "",
            )

        # Latest draft.
        draft = fetch_latest_tg_draft(post_id, db_path=path)
        if draft is None:
            raise PostNotFound(
                f"no tg_dispatch for post_id={post_id}; "
                f"call tg_regenerate() first"
            )

        # Defensive: a blocked draft should not be published.
        if not (draft["tg_title"] or "").strip():
            logger.warning(
                "tg_publish: post_id={} has empty draft (blocked?); "
                "skipping send",
                post_id,
            )
            return {
                "post_id": post_id,
                "message_id": None,
                "message_url": None,
                "chat_id": TG.your_channel_channel_id,
                "tg_draft_id": draft["id"],
                "blocked": True,
                "dry_run": False,
                "source_url": None,
                "wp_url": None,
                "telegra_url": None,
            }

        utm_url = build_utm_url(post["wp_post_url"] or "", post_id)

        # Sprint 6d.5: 3-level source URL fallback.
        # 1. candidates.url (orig article) — preferred.
        # 2. sources.homepage_url (источник homepage) — если item_url отсутствует.
        # 3. utm_url (наш сайт) — если всё пусто.
        item_url = (post["item_url"] or "").strip()
        source_homepage = (post["source_homepage"] or "").strip()
        source_url = item_url or source_homepage
        source_label = (post["item_title"] or "").strip() or (post["source_name"] or "").strip() or None

        # Telegraph Instant View (Sprint 6d.5 + 6d.6 + 6d.7): create mirror page.
        # Sprint 6d.6 changes the body payload so the Telegraph page reads
        # like the canonical article, not a teaser:
        #   - body_text = full WP content (content_html stripped to plain)
        #     instead of the short tg_teaser (DD 2026-07-19 20:56 feedback)
        #   - image_src = featured media source_url from WP (so IV shows
        #     the post's image, not nothing)
        # Sprint 6d.7 (DD 2026-07-19 21:19): if Telegraph is unreachable
        # after retries, do NOT sendMessage to @your_channel — we don't
        # ship channel posts without Instant View. Returns a result with
        # blocked='telegraph_unavailable' instead.
        telegra_url: Optional[str] = None

        # 1. Resolve image_src: pull featured_media source_url from WP
        #    via REST API. Only attempt if post has wp_post_id (i.e. WP
        #    publication succeeded). Otherwise the image is local-only
        #    and unreachable for Telegraph.
        image_src: Optional[str] = None
        if post["wp_post_id"]:
            try:
                image_src = _get_wp_featured_media_url(int(post["wp_post_id"]))
            except Exception as e:
                # Don't fail publish if image fetch fails — Telegraph
                # without image is better than no publish.
                logger.warning(
                    "tg_publish: featured_media fetch failed for wp_post_id={}: {}",
                    post["wp_post_id"], e,
                )

        # 2. Resolve body_text: prefer full WP content_html; fall back to
        #    tg_teaser if content_html is empty/missing. _html_to_paragraphs
        #    strips HTML tags and keeps paragraph breaks via \n\n.
        body_text = _html_to_paragraphs(post["content_html"] or "")
        if not body_text.strip():
            body_text = (draft["tg_teaser"] or "").strip()

        # 3. Try Telegraph createPage with retry + exponential backoff.
        #    create_telegraph_page internally uses _http_post_with_retry.
        #    If Telegra.ph is unreachable (TelegraphUnavailable), we
        #    abort publication rather than ship a WP-only link.
        #    NOTE (6d.8): we no longer pass source_url to Telegraph —
        #    the Telegraph mirror footer points to the operator's site
        #    (configurable via MEDIA_DISPLAY_DOMAIN env var), not the
        #    original source article. The source link in the TG-channel
        #    post is the only place readers see the original article —
        #    Telegraph is for canonical reading.
        try:
            hashtags_for_iv = json.loads(draft["tg_hashtags_json"] or "[]")
            if not isinstance(hashtags_for_iv, list):
                hashtags_for_iv = []
            telegra_url = telegraph.create_telegraph_page(
                title=(draft["tg_title"] or "").strip() or "(без заголовка)",
                body_text=body_text,
                image_src=image_src,
                wp_url=utm_url or None,
                tag_list=hashtags_for_iv or None,
            )
        except telegraph.TelegraphUnavailable as e:
            # Telegra.ph exhausted retries — DO NOT sendMessage to channel.
            # DD rule (2026-07-19 21:19): no IV → no post. Return early
            # with a clear blocked marker so /approve_tg caller can retry.
            logger.error(
                "tg_publish: Telegraph unavailable after retries for post_id={} "
                "— aborting publication (no IV → no post): {}",
                post_id, e,
            )
            return {
                "post_id": post_id,
                "message_id": None,
                "message_url": None,
                "chat_id": TG.your_channel_channel_id,
                "tg_draft_id": draft["id"],
                "blocked": True,
                "blocked_reason": "telegraph_unavailable",
                "dry_run": False,
                "source_url": source_url or None,
                "wp_url": utm_url or None,
                "telegra_url": None,
            }
        except telegraph.TelegraphError as e:
            # API-level Telegraph error (e.g. ACCESS_TOKEN_INVALID) — same
            # policy: don't ship a post without IV. The author of the token
            # needs to fix the config; this isn't a transient issue.
            logger.error(
                "tg_publish: Telegraph API error for post_id={} — aborting: {}",
                post_id, e,
            )
            return {
                "post_id": post_id,
                "message_id": None,
                "message_url": None,
                "chat_id": TG.your_channel_channel_id,
                "tg_draft_id": draft["id"],
                "blocked": True,
                "blocked_reason": f"telegraph_error: {e}",
                "dry_run": False,
                "source_url": source_url or None,
                "wp_url": utm_url or None,
                "telegra_url": None,
            }

        # Render footer with all 3 URLs (only source/wp are required,
        # telegra is optional).
        text = format_tg_post(
            draft,
            source_url=source_url or None,
            source_label=source_label,
            wp_url=utm_url or None,
            telegra_url=telegra_url,
        )

        # link_preview_options: prefer telegra_url for Instant View,
        # fall back to wp_url if Telegraph wasn't used (token missing).
        preview_url = telegra_url or utm_url
        payload = {
            "chat_id": TG.your_channel_channel_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {
                "url": preview_url,
                "prefer_large_media": True,
                "show_above_text": False,
            },
        }

        logger.info(
            "tg_publish: post_id={} chat_id={} text_len={} preview_url={} "
            "telegra_url={} source_url={}",
            post_id, TG.your_channel_channel_id, len(text), preview_url,
            telegra_url or "(none)", source_url or "(none)",
        )
        resp = tg_bridge._call("sendMessage", payload)
        if resp is None:
            # _call returns None on dry-mode (TG.bot_token empty in tests).
            # We already checked above, so this branch should be unreachable
            # in production — but handle defensively.
            raise RuntimeError(
                "tg_bridge._call returned None; check TG.bot_token / .env"
            )

        # Parse the response. Bot API returns {"ok": true, "result": {"message_id": ...}}.
        result = resp.get("result") or {}
        message_id = int(result.get("message_id") or 0)
        if not message_id:
            raise RuntimeError(
                f"sendMessage response missing message_id: {resp!r}"
            )

        # Build the public link. Supergroup/channel format:
        # https://t.me/<username>/<message_id> for public channels.
        username = (TG.your_channel_username or "").lstrip("@")
        if username:
            message_url = f"https://t.me/{username}/{message_id}"
        else:
            # Private channel fallback: t.me/c/<chat_id_no_-100>/<message_id>
            cid = TG.your_channel_channel_id
            if cid.startswith("-100"):
                cid = cid[4:]
            message_url = f"https://t.me/c/{cid}/{message_id}"

        # Persist idempotently.
        changed = mark_tg_channel_published(
            conn, post_id, message_id=message_id, message_url=message_url,
        )
        conn.commit()
        if not changed:
            # Race: someone else (or a retry) already wrote the same row.
            # The send succeeded though, so we don't fail the caller.
            logger.info(
                "tg_publish: post_id={} message_id={} already marked "
                "(race); returning existing",
                post_id, message_id,
            )

        logger.info(
            "tg_publish OK: post_id={} message_id={} url={} telegra_url={}",
            post_id, message_id, message_url, telegra_url or "(none)",
        )
        return {
            "post_id": post_id,
            "message_id": message_id,
            "message_url": message_url,
            "chat_id": TG.your_channel_channel_id,
            "tg_draft_id": draft["id"],
            "blocked": False,
            "dry_run": False,
            # Sprint 6d.5: extra fields for debugging / future analytics
            # (e.g. log IV usage rate per run).
            "source_url": source_url or None,
            "wp_url": utm_url or None,
            "telegra_url": telegra_url,
        }
    finally:
        conn.close()