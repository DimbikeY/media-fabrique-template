"""Sprint 4 — WordPress publisher.

Reads draft draft_posts from SQLite, publishes them to a WordPress site via the
REST API, and stores ``wp_post_id`` / ``wp_post_url`` back into ``draft_posts``.

The contract is idempotent on slug: if a post with the same slug already
exists on WP (e.g. we crashed after POST but before UPDATE), we PATCH the
existing post instead of creating a duplicate.

State machine for ``draft_posts.status`` (Sprint 6.6.1):
    draft ──► approved ──► publishing ──► published | failed
       │                       ▲
       └──► rejected (terminal, kept for analysis)

The ``draft → approved`` transition requires a human signal via
``/approve`` in the #drafts Telegram topic. Whether this gate is enforced
is controlled by ``PIPE_TICKS.wp_publish_auto_approve``:

  * ``True``  (default): publisher only picks ``status='approved'``.
    New drafts sit in the #drafts topic waiting for ``/approve``.
  * ``False``: publisher picks ``status='draft'`` directly (auto-publish).
    /approve becomes a no-op redirect; /feedback is the verdict command.

Atomic claim: ``UPDATE draft_posts SET status='publishing' WHERE id=? AND
status IN ('approved', 'draft')``. The IN clause mirrors the same WHERE
clause used by ``_fetch_candidates`` so a row claimed matches a row fetched.

Run:
    python publisher.py --limit 5
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import socket
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from loguru import logger
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    RetryCallState,
)

from config import PIPE, PIPE_TICKS, WP
from prompts import TG_PROMPT_VERSION

# --- Worker identity (parallel to rewrite_and_score.py) --------------------
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


# --- DB helpers --------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(PIPE.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _fetch_candidates(conn: sqlite3.Connection, limit: int) -> List[sqlite3.Row]:
    """Pick draft draft_posts whose underlying item is ready (Sprint 2 contract).

    Sprint 5.1 ordering: top-weight draft_posts come first. The whole point
    of scoring is "publish what matters most, fast" — if we publish
    low-weight draft_posts FIFO, the top candidates wait for filler to drain out,
    which kills the dwell-time window for the news that actually
    drives engagement.

    NULLS LAST emulation: scored candidates first, unscored last.
    """
    return list(conn.execute(
        """
        SELECT p.id AS post_id,
               p.candidate_id, p.title, p.slug, p.excerpt, p.content_html,
               p.meta_title, p.meta_description,
               p.featured_image_path, p.image_alt, p.image_prompt,
               p.categories_json, p.tags_json,
               i.title AS item_title, i.url AS item_url,
               s.name AS source_name, s.feed_url AS source_url,
               s.homepage_url AS source_homepage,
               i.video_embed_url,
               i.image_url AS source_image_url,
               i.weight, i.base_score, i.category
          FROM draft_posts p
          JOIN candidates i ON i.id = p.candidate_id
          JOIN sources s ON s.id = i.source_id
         WHERE p.status = CASE ?
                        WHEN 1 THEN 'approved'
                        ELSE 'draft'
                    END
           AND i.status = 'ready'
           AND p.slug IS NOT NULL AND p.slug != ''
         ORDER BY
           CASE WHEN i.weight IS NULL THEN 1 ELSE 0 END,
           i.weight DESC,
           p.id ASC
         LIMIT ?
        """,
        # Sprint X rename + flip: 0 = auto-publish (claim drafts),
        # 1 = manual review (claim approved).
        (0 if PIPE_TICKS.wp_publish_auto_approve else 1, limit),
    ))


def _claim(conn: sqlite3.Connection, post_id: int) -> bool:
    """Atomically transition approved|draft -> publishing. Returns True if we got it.

    Sprint 6.6.1: the claim clause mirrors the WHERE used in
    ``_fetch_candidates`` so a fetched row always passes the claim.
    Which clause is used depends on ``PIPE_TICKS.wp_publish_auto_approve``:

      * ``True``  → claim only ``status='approved'`` (human-gated)
      * ``False`` → claim any ``status='draft'`` (auto-publish mode)

    Same pattern as rewrite_and_score._claim: stash WORKER_ID in
    error_reason as a soft 'taken by' marker. It's overwritten on
    every state transition.
    """
    # Sprint X (DD 2026-07-19 22:03): rename `publish_requires_review` →
    # `wp_publish_auto_approve`. Semantics: True = auto-publish mode,
    # False = manual approve. After rename we FLIP the body of the if
    # because the old `publish_requires_review=True` meant 'claim only
    # approved' (manual mode), while the new `wp_publish_auto_approve=True`
    # means 'claim drafts' (auto mode).
    if PIPE_TICKS.wp_publish_auto_approve:
        claim_clause = "status = 'draft'"
    else:
        claim_clause = "status = 'approved'"
    cur = conn.execute(
        f"UPDATE draft_posts SET status='publishing', error_reason=?, "
        f"updated_at=datetime('now') "
        f"WHERE id = ? AND {claim_clause}",
        (f"worker:{WORKER_ID}", post_id),
    )
    return cur.rowcount == 1


def _mark_published(conn: sqlite3.Connection, post_id: int,
                    wp_post_id: int, wp_post_url: str,
                    sent_content_html: Optional[str] = None) -> None:
    # Persist the exact content we sent to WP so the DB row is the source
    # of truth (and a re-run / PATCH is bit-for-bit idempotent).
    if sent_content_html is None:
        conn.execute(
            "UPDATE draft_posts SET status='published', wp_post_id=?, wp_post_url=?, "
            "error_reason=NULL, updated_at=datetime('now') WHERE id=?",
            (wp_post_id, wp_post_url, post_id),
        )
    else:
        conn.execute(
            "UPDATE draft_posts SET status='published', wp_post_id=?, wp_post_url=?, "
            "content_html=?, error_reason=NULL, updated_at=datetime('now') WHERE id=?",
            (wp_post_id, wp_post_url, sent_content_html, post_id),
        )
    # Sprint Y (DD 2026-07-20 22:33 MSK): the admin-channel push is now
    # triggered by the process_one() call site AFTER the tg_dispatch row
    # has been inserted. We no longer fire tg_bridge.push_published()
    # from _mark_published() because the WP-only "published" state
    # happens here but the TG-channel leg hasn't run yet — the admin
    # would see a #published message for a post that hasn't appeared in
    # @your_channel. tick=publish_tg emits #published_tg separately when
    # it actually sends the TG message.


def _mark_failed(conn: sqlite3.Connection, post_id: int, reason: str) -> None:
    """Move back to failed (NOT draft) so it doesn't get re-claimed automatically.

    If the failure is transient (network, 5xx), the user / cron can move it
    back to 'draft' manually to retry. We deliberately do NOT auto-retry
    here — that would risk looping on a broken WP.
    """
    # Truncate the reason to keep the column readable.
    short = _summarize_error(reason)
    conn.execute(
        "UPDATE draft_posts SET status='failed', error_reason=?, updated_at=datetime('now') "
        "WHERE id=?",
        (short, post_id),
    )


def _summarize_error(msg: str) -> str:
    """Trim long tracebacks to a single readable line for draft_posts.error_reason.

    Same vibe as rewrite_and_score._summarize_error — we keep the column scannable.
    """
    s = (msg or "").strip().splitlines()[0] if msg else ""
    if not s:
        return "unknown"
    return s[:240]


# --- WP REST client ----------------------------------------------------------
class WPAuthError(Exception):
    """401/403 from WP — don't retry, surface immediately."""


class WPRequestError(Exception):
    """Network / 5xx — retryable."""


@dataclass
class WPPostResult:
    post_id: int
    url: str


class WPClient:
    """Thin wrapper over requests.Session with Basic Auth.

    All endpoints live under WP.base_url + /wp-json/wp/v2/.
    Application Passwords use Basic Auth with username:app_password as the
    credential pair. See https://make.wordpress.org/core/2020/11/05/application-passwords/.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        app_password: Optional[str] = None,
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
    ):
        self.base_url = (base_url or WP.base_url).rstrip("/")
        self.username = username or WP.username
        self.app_password = app_password or WP.app_password
        if not self.base_url or not self.username or not self.app_password:
            raise RuntimeError(
                "WP_BASE_URL / WP_USERNAME / WP_APP_PASSWORD must be set in .env"
            )
        self.timeout = timeout or PIPE.http_timeout_seconds
        self.retries = retries or PIPE.http_retries
        self.api_root = f"{self.base_url}/wp-json/wp/v2"
        self.session = self._make_session()

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        token = base64.b64encode(
            f"{self.username}:{self.app_password}".encode("utf-8")
        ).decode("ascii")
        s.headers.update({
            "Authorization": f"Basic {token}",
            "User-Agent": PIPE.user_agent,
            "Accept": "application/json",
        })
        return s

    # -- low-level HTTP ------------------------------------------------------
    @retry(
        reraise=True,
        stop=stop_after_attempt(PIPE.http_retries),
        wait=wait_exponential(min=PIPE.http_retry_backoff_seconds, max=10),
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, WPRequestError)),
        # Sprint X hotfix (DD 2026-07-20 07:05 MSK): tenactiy.before_sleep_log
        # uses loguru-style format-string `{exception}`, which collides with
        # loguru's own `format(*args, **kwargs)` when the exception message
        # contains `"{...}"` (e.g. WP returns `{"code":"..."}` errors).
        # Symptom: `KeyError: '"code"'` raised from loguru, masking the
        # original retryable exception. Inline a safe callback that uses
        # loguru's positional substitution instead of format-string parsing.
        before_sleep=lambda rs: logger.warning(
            "WP _request retry attempt={} outcome={} exc={}",
            rs.attempt_number,
            rs.outcome.exception() if rs.outcome and rs.outcome.failed else "ok",
            type(rs.outcome.exception()).__name__ if rs.outcome and rs.outcome.failed else None,
        ),
    )
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.api_root}{path}"
        try:
            r = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise WPRequestError(f"{method} {path}: {e}") from e
        if r.status_code in (401, 403):
            raise WPAuthError(f"{method} {path} → {r.status_code}: {r.text[:200]}")
        if r.status_code >= 500:
            raise WPRequestError(f"{method} {path} → {r.status_code}: {r.text[:200]}")
        return r

    # -- high-level operations ----------------------------------------------
    def find_post_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Return the existing WP post for this slug, or None.

        Two subtleties the WP REST API forces us to deal with:

        1. **Status filter.** WP REST API does NOT allow ``status=any``
           for users below Editor. Sprint 6m moved to Editor role to
           get ``edit_others_posts``, but even Editor can't pass
           ``status=any`` — that requires Administrator or a custom
           ``view_private_posts`` capability. We use ``status=publish``
           and accept that we'll create a fresh post if the previous
           run left a draft that wasn't published. The second publish
           will PATCH (idempotent on slug) only if the previous post
           made it to ``publish``. Drafts get a new sibling post.

        2. **Auto-suffixing on collision.** When WP creates a post whose
           slug is already taken, it appends ``-2``, ``-3`` etc. to the
           slug of the *new* post. The original keeps its slug. So a
           slug check alone is enough — we don't need to enumerate
           suffixes on the lookup side.
        """
        r = self._request("GET", "/posts", params={
            "slug": slug,
            "status": "publish",
            "per_page": 1,
        })
        if r.status_code != 200:
            raise WPRequestError(f"GET /posts?slug={slug} → {r.status_code}: {r.text[:200]}")
        arr = r.json()
        if not arr:
            return None
        return arr[0]

    def lookup_categories(self, slugs: List[str]) -> List[int]:
        """Resolve category slugs to WP term IDs. Falls back to ?search= if
        nothing matches by slug (the LLM sometimes returns display names
        rather than kebab slugs, e.g. Cyrillic titles)."""
        if not slugs:
            return []
        # 1) Try slug match first (cheap, exact).
        ids = self._lookup_terms_by_slug("/categories", slugs)
        if ids:
            return ids
        # 2) Fallback: search by name (one request per slug; cap total).
        ids = []
        for s in slugs[:10]:
            found = self._lookup_terms_by_search("/categories", s)
            ids.extend(found)
        return ids

    def lookup_tags(self, slugs: List[str]) -> List[int]:
        if not slugs:
            return []
        ids = self._lookup_terms_by_slug("/tags", slugs)
        if ids:
            return ids
        ids = []
        for s in slugs[:10]:
            found = self._lookup_terms_by_search("/tags", s)
            ids.extend(found)
        return ids

    def _lookup_terms_by_slug(self, endpoint: str, slugs: List[str]) -> List[int]:
        slugs_csv = ",".join(slugs)
        r = self._request("GET", endpoint, params={"slug": slugs_csv, "per_page": 100})
        if r.status_code != 200:
            raise WPRequestError(f"GET {endpoint} → {r.status_code}: {r.text[:200]}")
        return [t["id"] for t in r.json()]

    def _lookup_terms_by_search(self, endpoint: str, name: str) -> List[int]:
        r = self._request("GET", endpoint, params={"search": name, "per_page": 5})
        if r.status_code != 200:
            return []
        return [t["id"] for t in r.json()]

    def upload_media(self, file_path: Path, alt_text: str, caption: str = "") -> int:
        """Upload a local image to WP media library, return its media ID."""
        # WP media upload wants multipart/form-data. We read the file here
        # (it's small — ~1200×630 WebP after Sprint 5.2.1, ~50-130 KB) and
        # pass it inline. Mime is derived from the file extension so the
        # same uploader works for WebP today and any future format.
        mime, _ = mimetypes.guess_type(file_path.name)
        if mime is None:
            # Last-resort fallback. WP will reject unknown mime, but at
            # least our log will say what we tried.
            mime = "application/octet-stream"
        with open(file_path, "rb") as f:
            data = f.read()
        files = {"file": (file_path.name, data, mime)}
        payload: Dict[str, str] = {"alt_text": alt_text or ""}
        if caption:
            payload["caption"] = caption
        r = self._request("POST", "/media", files=files, data=payload)
        if r.status_code not in (200, 201):
            raise WPRequestError(
                f"POST /media → {r.status_code}: {r.text[:300]}"
            )
        media_id = r.json().get("id")
        if not isinstance(media_id, int):
            raise WPRequestError(f"POST /media returned no id: {r.text[:300]}")
        return media_id

    def create_post(self, payload: Dict[str, Any]) -> WPPostResult:
        r = self._request("POST", "/posts", json=payload)
        if r.status_code not in (200, 201):
            raise WPRequestError(f"POST /posts → {r.status_code}: {r.text[:400]}")
        body = r.json()
        return WPPostResult(post_id=body["id"], url=body.get("link", ""))

    def update_post(self, wp_post_id: int, payload: Dict[str, Any]) -> WPPostResult:
        r = self._request("POST", f"/posts/{wp_post_id}", json=payload)
        if r.status_code not in (200, 201):
            raise WPRequestError(
                f"POST /posts/{wp_post_id} → {r.status_code}: {r.text[:400]}"
            )
        body = r.json()
        return WPPostResult(post_id=body["id"], url=body.get("link", ""))

    def delete_post(self, wp_post_id: int) -> bool:
        """Trash a WP post. Used by the telegraph-required rollback path.

        Sprint X (DD 2026-07-20 07:09 MSK): when Telegraph IV is unavailable
        and ``TELEGRAPH_REQUIRED_FOR_PUBLISH=1`` (canonical form per
        DD 2026-07-20 11:46 MSK; ``true``/``yes`` accepted by
        ``_env_bool()``), publisher.py calls this to undo the WP publish
        so we don't leave an orphaned WP-only post.

        Note: WP REST API supports hard-delete (``?force=true``) only for
        users with the ``delete_others_posts`` cap (Editor+). Our
        ``media-<deploy-user>`` Author role does NOT have that, so we trash instead
        (default DELETE behaviour). The janitor sweep can later purge
        trashed posts if DD wants to free WP storage.
        """
        r = self._request("DELETE", f"/posts/{wp_post_id}")
        if r.status_code not in (200, 201, 204):
            raise WPRequestError(
                f"DELETE /posts/{wp_post_id} → {r.status_code}: {r.text[:400]}"
            )
        return True


# --- Content composition -----------------------------------------------------
import re

# Match a trailing <p>Источник ... </p> that the LLM (or our previous
# publish run) might have emitted. We strip ours before adding the canonical
# <a href> version so re-publishes don't accumulate duplicates.
# The body of the <p> can contain an <a> tag, so we use non-greedy .*? to
# match the closing </p> rather than a stricter character class.
_SOURCE_PARAGRAPH_RE = re.compile(
    r"<p>\s*Источник\s*:[^\n]*?</p>",
    re.IGNORECASE | re.DOTALL,
)


def _compose_content_html(row: sqlite3.Row) -> str:
    """Append a Source attribution paragraph and an optional Video paragraph.

    These are appended on every publish run — idempotent because we
    recompose from scratch each time (overwriting the previous WP body).

    The LLM sometimes emits its own «Источник:» paragraph as a bare URL
    (per master_prompt rule #9 — bare URLs are auto-wrapped in oEmbed iframes).
    We strip any trailing Источник paragraph(s) and replace with a clean
    <a href> link so we don't get duplicates.
    """
    body = row["content_html"] or ""
    # Strip trailing Источник paragraphs (and trailing whitespace).
    body = body.rstrip()
    body = _SOURCE_PARAGRAPH_RE.sub("", body).rstrip()

    pieces: List[str] = [body] if body else []

    # 3-level fallback for the «Источник:» link (Sprint 5.5 B3):
    #   1) item.url (URL of the actual article) — if present and not the feed
    #   2) source.homepage_url — landing page of the source (never the RSS XML)
    #   3) source.name as plain text — no link (don't dump user into XML)
    item_url = (row["item_url"] or "").strip()
    feed_url = (row["source_url"] or "").strip()
    homepage_url = (row["source_homepage"] or "").strip()
    source_name = (row["source_name"] or "источник").strip()

    chosen_url = ""
    if item_url and item_url != feed_url:
        chosen_url = item_url
    elif homepage_url:
        chosen_url = homepage_url

    if chosen_url:
        pieces.append(
            f'<p>Источник: <a href="{chosen_url}" rel="nofollow noopener" '
            f'target="_blank">{source_name}</a></p>'
        )
    else:
        pieces.append(f'<p>Источник: {source_name}</p>')

    video_url = row["video_embed_url"]
    if video_url:
        # WP oEmbed handles bare URLs in <p>; we keep the same convention as
        # the master prompt.
        pieces.append(f"<p>Видео: {video_url}</p>")

    return "\n\n".join(pieces)


def _decode_json_field(raw: Optional[str]) -> List[Any]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


# --- One-shot process --------------------------------------------------------
def _resolve_term_ids(client: WPClient, row: sqlite3.Row) -> Tuple[List[int], List[int]]:
    """Turn LLM-supplied category/tag slugs (plus WP_DEFAULT_CATEGORIES) into IDs."""
    cat_slugs = list(_decode_json_field(row["categories_json"]))
    tag_slugs = list(_decode_json_field(row["tags_json"]))
    # Add default categories from config (dedup, preserve order).
    seen = set(cat_slugs)
    for s in WP.default_categories:
        if s and s not in seen:
            cat_slugs.append(s)
            seen.add(s)
    cat_ids = client.lookup_categories(cat_slugs) if cat_slugs else []
    tag_ids = client.lookup_tags(tag_slugs) if tag_slugs else []
    return cat_ids, tag_ids


def _build_payload(
    row: sqlite3.Row,
    *,
    featured_media_id: int,
    category_ids: List[int],
    tag_ids: List[int],
) -> Dict[str, Any]:
    """Build the JSON payload for POST /wp/v2/posts."""
    title = row["title"] or row["item_title"] or ""
    excerpt = row["excerpt"] or ""
    content_html = _compose_content_html(row)
    payload: Dict[str, Any] = {
        "title": title,
        "slug": row["slug"],
        "content": content_html,
        "excerpt": excerpt,
        # Sprint 6.6.1: publisher ALWAYS emits status='publish'. The legacy
        # WP.default_status ('draft') was the actual bug behind broken
        # wp_post_url links: a draft post in WP returns ?p=<id> in body.link,
        # which 404s for anonymous visitors. We hard-code 'publish' here
        # because the publisher is, by definition, the publish step.
        "status": "publish",
        "featured_media": featured_media_id,
    }
    if category_ids:
        payload["categories"] = category_ids
    if tag_ids:
        payload["tags"] = tag_ids
    # Yoast / Rank Math / SEOPress all read 'meta' for these keys; not all
    # plugins honor them, but it's a no-op for those that don't.
    meta: Dict[str, str] = {}
    if row["meta_title"]:
        meta["_yoast_wpseo_title"] = row["meta_title"]
    if row["meta_description"]:
        meta["_yoast_wpseo_metadesc"] = row["meta_description"]
    if meta:
        payload["meta"] = meta
    return payload


def process_one(client: WPClient, conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Publish a single post. Returns one of: 'published' | 'failed'."""
    post_id = row["post_id"]
    started = time.monotonic()
    try:
        # 1. Slug-atom check: if WP already has this slug, reuse it.
        existing = client.find_post_by_slug(row["slug"])
        # 2. Resolve term IDs.
        category_ids, tag_ids = _resolve_term_ids(client, row)
        # 3. Upload featured image (inline Plan A→B→C if needed).
        # Sprint 5.2: image acquisition is no longer a separate cron-tick.
        # If draft_posts.featured_image_path is missing, run the ladder here so
        # every published post has either a real cover or an explicit
        # Plan D graceful skip. We never publish a post that has a path
        # pointing at a missing file — either we regenerate or skip.
        featured_media_id = 0
        img_path_str = row["featured_image_path"]
        img_path = Path(img_path_str) if img_path_str else None
        need_image = (img_path is None) or (not img_path.exists())
        if need_image:
            # Inline image generation. Lazy import: image_pipeline pulls
            # in PIL/OpenAI; we only pay that cost when we actually need
            # to fetch an image, not for every published post.
            from image_pipeline import ensure_image
            ensured = ensure_image(
                candidate_id=row["candidate_id"],
                source_image_url=row["source_image_url"] if "source_image_url" in row.keys() else None,
                post_image_prompt=row["image_prompt"],
                post_image_alt=row["image_alt"],
                item_title=row["item_title"],
                item_category=row["category"] if "category" in row.keys() else None,
            )
            if ensured.path is not None:
                # Persist the path so the same post doesn't regenerate
                # on a retry, and so a future smoke run can find it.
                conn.execute(
                    "UPDATE draft_posts SET featured_image_path=? WHERE id=?",
                    (str(ensured.path), post_id),
                )
                conn.commit()
                img_path = ensured.path
                logger.success(
                    "Inline Plan {} ok for post {}: {}", ensured.plan, post_id, ensured.path,
                )
            else:
                # Plan D — graceful skip, publish without featured_media.
                logger.warning(
                    "Inline Plan D for post {}: publishing without image ({})",
                    post_id, ensured.note,
                )

        if img_path is not None and img_path.exists():
            caption = row["source_name"] or ""
            featured_media_id = client.upload_media(
                img_path, alt_text=row["image_alt"] or "", caption=caption
            )
        # 4. Build payload + create or update.
        payload = _build_payload(
            row,
            featured_media_id=featured_media_id,
            category_ids=category_ids,
            tag_ids=tag_ids,
        )
        if existing:
            wp_id = existing["id"]
            result = client.update_post(wp_id, payload)
            action = "updated"
        else:
            result = client.create_post(payload)
            action = "created"
        # 5. Persist back to SQLite. Store the exact content we sent so
        # the DB row reflects what WP has (idempotent re-runs).
        _mark_published(conn, post_id, result.post_id, result.url,
                        sent_content_html=payload["content"])
        conn.commit()
        elapsed = (time.monotonic() - started) * 1000
        logger.success(
            "Post {} published ({}, wp_id={}, media={}, cats={}, tags={}, {:.0f}ms) → {}",
            post_id, action, result.post_id, featured_media_id,
            len(category_ids), len(tag_ids), elapsed, result.url,
        )

        # Sprint Y (DD 2026-07-20 22:33 MSK): the Telegraph/TG-channel
        # publish is no longer part of this tick. We register the post
        # in tg_dispatch with status='pending_tg_text' so that the new
        # tick=generate_for_tg can pick it up on its next run. Telegraph
        # failures in tick=publish_tg cannot roll back the WP publish
        # any more — they just bump the attempts counter and leave the
        # row in the queue. draft_posts.status stays 'published' for the
        # lifetime of the post so future integrations (e.g. Telegram
        # channel, mailing-list, future feed) can read the WP URL even
        # when the TG-channel leg is stalled.
        conn.execute(
            """
            INSERT INTO tg_dispatch (post_id, status, prompt_version, note)
            VALUES (?, 'pending_tg_text', ?, NULL)
            """,
            (post_id, TG_PROMPT_VERSION),
        )
        conn.commit()
        # Best-effort admin notification (replaces Sprint X
        # push_published_tg which no longer applies because the TG
        # channel side is now handled in tick=publish_tg).
        try:
            import tg_bridge
            tg_bridge.push_published(row, row)
        except Exception as bridge_e:
            logger.warning(
                "tg_bridge.push_published failed for post_id={} (non-fatal): {}",
                post_id, bridge_e,
            )

        return "published"
    except WPAuthError as e:
        logger.error("WP auth failure for post {}: {}", post_id, e)
        _mark_failed(conn, post_id, f"wp_auth: {e}")
        conn.commit()
        return "failed"
    except WPRequestError as e:
        logger.error("WP request failure for post {}: {}", post_id, e)
        _mark_failed(conn, post_id, f"wp_request: {e}")
        conn.commit()
        return "failed"
    except Exception as e:
        logger.error(
            "Unhandled error publishing post {}:\n{}",
            post_id, traceback.format_exc(),
        )
        _mark_failed(conn, post_id, f"wp_unhandled: {e}")
        conn.commit()
        return "failed"


# --- Orchestration -----------------------------------------------------------
def run(limit: Optional[int] = None) -> Dict[str, int]:
    """Publish up to ``limit`` draft draft_posts (default = PIPE.max_items_per_run)."""
    n = limit if limit is not None else PIPE.max_items_per_run
    counts = {"published": 0, "failed": 0, "skipped_already": 0}

    try:
        client = WPClient()
    except RuntimeError as e:
        logger.error("{}", e)
        return counts

    with _connect() as conn:
        rows = _fetch_candidates(conn, n)
        if not rows:
            logger.info("No draft draft_posts to publish.")
            return counts
        for row in rows:
            if not _claim(conn, row["post_id"]):
                counts["skipped_already"] += 1
                continue
            conn.commit()  # persist the claim
            status = process_one(client, conn, row)
            counts[status] = counts.get(status, 0) + 1

    logger.info("Run summary: {}", counts)
    return counts


def _parse_args(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="publisher",
        description="Publish draft draft_posts to WordPress via REST API.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max draft_posts to publish this run. Overrides MAX_ITEMS_PER_RUN.",
    )
    args = parser.parse_args(argv)
    return args.limit if args.limit is not None else -1


def main() -> int:
    try:
        limit_override = _parse_args()
        if limit_override is not None and limit_override >= 0:
            run(limit=limit_override)
        else:
            run()
    except SystemExit:
        raise
    except Exception:
        logger.error("Unhandled exception:\n{}", traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())