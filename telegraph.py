"""Sprint 6d.5 — Telegra.ph API wrapper for Instant View integration.

Telegra.ph (https://telegra.ph/) is a publishing platform owned by Telegram.
Every page hosted there gets Instant View support automatically inside the
Telegram messenger — including inside @your_channel posts. This module is
the thin wrapper that creates a "mirror" page for our post and returns its
telegra.ph URL, which we then plug into link_preview_options so TG renders
the IV overlay in the channel post.

Why a mirror (not a redirect):
  - WP URL stays canonical. telegraph URL is a *copy* of the post body so IV
    can render inside TG without leaving the messenger.
  - The bottom of every telegraph page links back to the original WP URL
    (so readers can find the canonical source).

Public API:
  - create_telegraph_page(title, body_html, *, source_url=None,
                           source_label=None, wp_url=None, tag_list=None)
        Returns the telegra.ph URL of the new page (e.g.
        https://telegra.ph/Valve-Steam-Machine-07-19-2).
        Raises TelegraphError on API failure.

  - TelegraphError: raised on API error or invalid configuration.

Configuration:
  - TELEGRA_PH_ACCESS_TOKEN  (str): access_token from createAccount()
        (env var). Operator got one on 2026-07-19 (short_name=YourBrand
        placeholder). Must be set on production (VPS-B). Smoke tests
        skip if unset.
  - TELEGRA_PH_AUTHOR_URL    (str): optional override of author profile URL.
        Defaults to your-domain homepage. In production set this to your
        canonical site URL (the telegra.ph author_url metadata is rendered
        on every published page).

API reference: https://telegra.ph/api
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess  # noqa: F401  kept around in case future smoke helpers need it
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from loguru import logger


# --- Endpoints --------------------------------------------------------------
TELEGRA_PH_API = "https://api.telegra.ph"


# --- Exceptions --------------------------------------------------------------
class TelegraphError(RuntimeError):
    """Telegra.ph API call failed (any reason). Caller (publish) handles gracefully."""


class TelegraphUnavailable(TelegraphError):
    """Telegra.ph failed after exhausting retries on transient network errors.

    Subclass of TelegraphError so existing except TelegraphError blocks
    still match, but callers that want to skip publishing (vs fallback to
    WP URL) can specifically catch this. Raised by _http_post_with_retry()
    when N attempts all timed out / URLError'd; indicates Telegra.ph is
    likely down or the route from VPS-B is wedged.
    """


# --- Helpers -----------------------------------------------------------------
def _esc(s: str) -> str:
    """Minimal text escape for Node children. Telegraph accepts plain text
    in children arrays; only HTML-special chars in <a href> need URL encoding."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _normalize_nodes(content: Any) -> List[Dict[str, Any]]:
    """Validate content is a list of Node dicts. Telegraph API rejects
    anything else (strings-as-content, missing tag, etc.) with a 400."""
    if not isinstance(content, list):
        raise TelegraphError(
            f"telegraph content must be a list of Node dicts, got {type(content).__name__}"
        )
    for i, node in enumerate(content):
        if not isinstance(node, dict) or "tag" not in node:
            raise TelegraphError(
                f"telegraph node #{i} must be dict with 'tag' key, got {node!r}"
            )
    return content


def _http_post(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """POST to api.telegra.ph/{method}. Returns parsed JSON.

    Telegraph API uses form-encoded POST params (not JSON body) — same as
    api.telegram.org. Returns {ok: True, result: ...} or {ok: False, error: ...}.

    Implementation history:
      - Sprint 6d.7 (DD 2026-07-19 21:24): switched to curl --ipv4 because
        urllib was hanging on IPv6 with no IPv4 fallback.
      - Sprint X hotfix (DD 2026-07-20 07:37): **switched back to urllib**.
        Empirically, as of 2026-07-20 ~04:17 MSK, `curl --ipv4` to
        api.telegra.ph from VPS-B now times out (rc=28) while urllib
        connects successfully to the same IP (149.154.164.13). The exact
        root cause is unclear (likely a Cloudflare/TG-side edge issue
        that affects one TLS stack but not the other), but the symptom
        is reproducible and urllib is the working path TODAY. We keep
        the SNI hostname intact (urllib uses `Host:` header from URL, no
        IP-literal trick that would break cert SAN verification).

        If curl --ipv4 ever works again, we may want to switch back — but
        right now the choice is 'urllib works, curl doesn't' so urllib
        wins.

    No retry — callers that want retry should use _http_post_with_retry().
    """
    body = urllib.parse.urlencode(params, doseq=True)
    data_bytes = body.encode("utf-8")
    req = urllib.request.Request(
        f"{TELEGRA_PH_API}/{method}",
        data=data_bytes,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        # URLError is the base for HTTPError, timeout, connection refused,
        # DNS failure. We wrap as TelegraphError so the retry layer can
        # distinguish network from API errors.
        raise TelegraphError(
            f"telegraph {method} urllib URLError: {e}"
        ) from e
    except TimeoutError as e:
        raise TelegraphError(
            f"telegraph {method} timeout"
        ) from e
    except OSError as e:
        raise TelegraphError(
            f"telegraph {method} OSError: {e}"
        ) from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TelegraphError(
            f"telegraph {method} returned non-JSON: {raw[:200]!r}"
        ) from e

    if not data.get("ok"):
        raise TelegraphError(
            f"telegraph {method} returned error: {data.get('error', '?')!r}"
        )
    return data["result"]


# Retry policy for transient failures (DD 2026-07-19 21:19, refined 2026-07-20).
# Only network-level errors (URLError: timeout, connection refused, DNS) are
# retried — API errors like ACCESS_TOKEN_INVALID are permanent and bubble up
# immediately. Backoff doubles each attempt: 1s, 2s, 4s for max_attempts=3.
# Note: after the Sprint X urllib switch, _http_post no longer raises
# subprocess.TimeoutExpired, so that type is gone from the tuple.
_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
_RETRY_NETWORK_EXC = (
    OSError,                     # urllib URLError + socket errors + DNS
    TimeoutError,                # urlopen timeout
)


def _http_post_with_retry(method: str, params: Dict[str, Any], *, max_attempts: int = 3) -> Dict[str, Any]:
    """POST to api.telegra.ph/{method} with retry + exponential backoff.

    On URLError/TimeoutError/ConnectionError, retry up to max_attempts
    times with delays from _RETRY_BACKOFF_SECONDS (1s, 2s, 4s for default
    3 attempts). The first attempt has no delay (fail fast). If all
    attempts fail with a network error, raises TelegraphUnavailable
    (subclass of TelegraphError) — callers that want to skip publishing
    rather than fallback should catch this specifically.

    On TelegraphError from a non-network cause (API returned error,
    non-JSON response, unexpected exception) — re-raise immediately, no
    retry. These indicate misconfiguration, not transient network issues.
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _http_post(method, params)
        except _RETRY_NETWORK_EXC as e:
            last_err = e
            if attempt < max_attempts:
                delay = _RETRY_BACKOFF_SECONDS[min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)]
                logger.warning(
                    "telegraph {} network error attempt={}/{}: {} — retrying in {:.1f}s",
                    method, attempt, max_attempts, e, delay,
                )
                import time as _time
                _time.sleep(delay)
            else:
                logger.warning(
                    "telegraph {} network error attempt={}/{}: {} — GIVING UP",
                    method, attempt, max_attempts, e,
                )
        except TelegraphError as e:
            # API-level error (not network). Don't retry — permanent.
            raise

    # All attempts exhausted with network errors.
    raise TelegraphUnavailable(
        f"telegraph {method} unavailable after {max_attempts} attempts: "
        f"{last_err!r}"
    ) from last_err


# --- Public API --------------------------------------------------------------
def create_telegraph_page(
    title: str,
    body_text: str,
    *,
    source_url: Optional[str] = None,
    source_label: Optional[str] = None,
    wp_url: Optional[str] = None,
    tag_list: Optional[List[str]] = None,
    image_src: Optional[str] = None,
    access_token: Optional[str] = None,
    author_url: Optional[str] = None,
) -> Optional[str]:
    """Create a Telegra.ph page containing the post body and source links.

    Returns:
        The telegra.ph URL (e.g. https://telegra.ph/Valve-...-07-19-2) on
        success, or None when access_token is unset (callers should treat
        as "IV disabled" rather than hard-fail).

    Args:
        title:            post title (becomes <h3> on telegra.ph).
        body_text:        post body paragraph (becomes <p> on telegra.ph).
        source_url:       original article URL (item.url from candidates).
                          If given, footer has "Источник: <a href=source_url>...".
        source_label:     link label for source_url (defaults to source_url).
        wp_url:           our WP canonical URL (with utm optional).
                          If given, footer has "Читать полностью: <a href=wp_url>...".
        tag_list:         list of hashtag strings (without '#'). Telegraph
                          accepts an optional `tag_list` field but it's
                          surfaced only in the admin/author view — not
                          rendered to readers. We still send it for SEO.
        access_token:     override the env var (tests use this).
        author_url:       override the env var (default
                          `TELEGRA_PH_AUTHOR_URL` env var, fallthrough
                          `https://your-domain.example.com`).

    Layout (Telegra.ph Node array):
        [h3: title, p: body, p: hashtags, hr, blockquote: source, p: read-more]
    """
    token = (
        access_token
        or os.environ.get("TELEGRA_PH_ACCESS_TOKEN", "")
    ).strip()
    if not token:
        # Skip gracefully — Telegraph is optional. Caller falls back to
        # WP URL in link_preview_options (TG will show a regular preview).
        logger.info(
            "telegraph: TELEGRA_PH_ACCESS_TOKEN is empty; "
            "skipping page creation (no IV)"
        )
        return None

    author_url = (
        author_url
        or os.environ.get("TELEGRA_PH_AUTHOR_URL", "https://your-domain.example.com")
    ).strip()

    # Visible domain label for the "Еще больше..." footer link. Falls back to
    # the author_url hostname so we don't hardcode any operator-specific
    # string into the rendered HTML.
    from urllib.parse import urlparse as _urlparse
    _author_host = _urlparse(author_url).hostname or "your-domain.example.com"
    display_domain = os.environ.get("MEDIA_DISPLAY_DOMAIN", _author_host)

    # Build Telegraph content nodes. The list is JSON-encoded by urlencoded.
    content: List[Dict[str, Any]] = []

    # Featured media BEFORE body (Telegra.ph convention). <figure><img>
    # wraps the image so the platform treats it as a real image block.
    # DD 2026-07-19 20:56: Telegram should see the post's actual image,
    # not just text. We pull the public source_url from WP after upload.
    if image_src:
        content.append({
            "tag": "figure",
            "children": [{"tag": "img", "attrs": {"src": image_src}}],
        })

    # Body paragraphs. We split on '\n\n' so each WP paragraph becomes
    # its own <p> node — Telegraph renders them with proper spacing.
    # Telegraph rejects raw HTML in children; we send plain text only
    # (caller strips HTML to plain text before calling).
    if body_text:
        for para in [p.strip() for p in body_text.split("\n\n") if p.strip()]:
            content.append({"tag": "p", "children": [_esc(para)]})

    if source_url or wp_url:
        content.append({"tag": "hr"})

    # Sprint 6d.6 + 6d.8: Telegraph footer points readers back to OUR
    # site, not to the original source article (the source attribution
    # lives in the Telegram-channel footer, not on the mirror). 6d.8:
    # label 'Еще больше горячих новостей' → 'Еще больше актуальных
    # новостей' (DD nuance).
    if wp_url:
        content.append({
            "tag": "p",
            "children": [
                "Еще больше актуальных новостей: ",
                {"tag": "a", "attrs": {"href": wp_url}, "children": [display_domain]},
            ],
        })

    # NOTE (6d.10 / DD 2026-07-19 22:11): hashtags moved to the BOTTOM of
    # the Telegraph page, AFTER the 'Еще больше актуальных новостей'
    # footer link. Was sitting just after body, which broke visual flow
    # (footer link read as a continuation of tags). Now tags are the
    # very last element on the page so they read like a closing sigil.
    if tag_list:
        hashtag_line = " ".join(f"#{_esc(str(t))}" for t in tag_list if t)
        if hashtag_line:
            content.append({"tag": "p", "children": [hashtag_line]})

    # NOTE (6d.8): the wp_url block was removed — we've consolidated all
    # reader navigation into the single 'Еще больше актуальных новостей'
    # footer link above. Two footer links with overlapping destinations
    # cluttered the page.

    content = _normalize_nodes(content)

    params: Dict[str, Any] = {
        "access_token": token,
        "title": title or "(без заголовка)",
        "author_name": "Media <deploy-user>",
        "author_url": author_url,
        "content": json.dumps(content, ensure_ascii=False),
        "return_content": "false",
    }
    if tag_list:
        params["tag_list"] = json.dumps(list(tag_list), ensure_ascii=False)

    try:
        result = _http_post_with_retry("createPage", params)
    except TelegraphError as e:
        logger.error("telegraph: createPage failed: {}", e)
        raise  # let caller decide fallback

    url = (result or {}).get("url", "")
    if not url:
        raise TelegraphError(
            f"telegraph createPage returned no url; result={result!r}"
        )
    logger.info("telegraph: createPage OK → {}", url)
    return url


# --- CLI helper for one-off registration smoke (not used in prod) ----------
if __name__ == "__main__":
    # Smoke: create a tiny test page and print URL. Re-runs are idempotent —
    # they create a new page each time, but the title stays under our control.
    test_url = create_telegraph_page(
        title="Telegraph smoke test",
        body_text="This page was created by media-<deploy-user> smoke at " + __import__("datetime").datetime.utcnow().isoformat() + " UTC.",
        source_url="https://your-domain.example.com",
        wp_url="https://your-domain.example.com",
        tag_list=["your-domain", "smoke"],
    )
    print("OK" if test_url else "SKIPPED (no token)")
    if test_url:
        print(test_url)
