"""Sprint 1 — RSS fetcher.

Reads enabled sources from `sources` table, fetches RSS, normalizes entries,
extracts the first whitelisted video embed (iframe preferred, anchor as
fallback), deduplicates by (source_id, guid), writes new candidates with
status='new' and safety_status='review'.

Topic-agnostic by design: we take whatever the feeds publish. Forbidden
topics (military operations, politics, sanctions, etc.) are rejected later
by the LLM in Sprint 2 via the `blocked` JSON flag.

Run:
    python rss_fetcher.py [--max N] [--source NAME]

--max:    max new candidates per run (overrides MAX_ITEMS_PER_RUN).
--source: only fetch from this source by name (case-insensitive).
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import PIPE, WP
from init_db import init_db
from utils import is_http_url, to_iso

log = logging.getLogger("rss")
logging.basicConfig(
    level=PIPE.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Hosts we trust for video embeds (read from config; see WP.allowed_embed_hosts).
VIDEO_HOSTS = list(WP.allowed_embed_hosts)

IFRAME_RE = re.compile(
    r"""<iframe[^>]+src=["'](?P<src>[^"']+)["']""",
    re.IGNORECASE,
)
IMG_RE = re.compile(
    r"""<img[^>]+src=["'](?P<src>[^"']+)["']""",
    re.IGNORECASE,
)


# --- default source seed -----------------------------------------------------
# Add a source by inserting a row; we ship a default starter set.
# `lang` here is a hint that goes into candidates.lang; the LLM later uses it to
# decide whether the post needs RU/EN translation in Sprint 2.
DEFAULT_SOURCES = [
    # Stopgame returns a SmartCaptcha HTML page instead of RSS, so it is
    # shipped disabled. Enable via SQL once you set up a proxy or Playwright.
    {"name": "Stopgame (RU)",          "feed_url": "https://www.stopgame.ru/rss",            "kind": "rss", "lang": "ru", "enabled": 0},
    {"name": "IGN (EN)",               "feed_url": "https://feeds.feedburner.com/ign/all",  "kind": "rss", "lang": "en", "enabled": 1},
    {"name": "Kotaku (EN)",            "feed_url": "https://kotaku.com/rss",                 "kind": "rss", "lang": "en", "enabled": 1},
    {"name": "TechRadar Gaming (EN)",  "feed_url": "https://www.techradar.com/feeds/tag/gaming", "kind": "rss", "lang": "en", "enabled": 1},
    {"name": "Dot Esports (EN)",       "feed_url": "https://dotesports.com/feed",           "kind": "rss", "lang": "en", "enabled": 1},
    {"name": "Eurogamer (EN)",         "feed_url": "https://www.eurogamer.net/?feed=rss",   "kind": "rss", "lang": "en", "enabled": 1},
    {"name": "GameSpot (EN)",          "feed_url": "https://www.gamespot.com/feeds/mashup/", "kind": "rss", "lang": "en", "enabled": 1},
]


@dataclass
class FeedItem:
    source_id: int
    guid: str
    url: str
    title: str
    summary: str
    body: str
    image_url: str
    video_embed_url: str
    published_at: Optional[str]
    lang: str


# --- helpers -----------------------------------------------------------------
def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _first_image(html_text: str) -> str:
    if not html_text:
        return ""
    m = IMG_RE.search(html_text)
    return m.group("src") if m else ""


def _first_video(html_text: str) -> str:
    """Return the first video URL we trust: <iframe src=...> first, then
    <a href="..."> to a whitelisted host. Empty string if none."""
    if not html_text:
        return ""

    # 1) iframe — preferred (already wrapped for oEmbed)
    for m in IFRAME_RE.finditer(html_text):
        src = m.group("src")
        host = (urlparse(src).hostname or "").lower()
        if any(h in host for h in VIDEO_HOSTS):
            return src

    # 2) anchor — fallback (raw YouTube/Vimeo link)
    for a in re.finditer(r"""<a[^>]+href=["'](?P<href>[^"']+)["']""", html_text, re.IGNORECASE):
        href = a.group("href")
        host = (urlparse(href).hostname or "").lower()
        if any(h in host for h in VIDEO_HOSTS):
            return href

    return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# `_safe_iso` and `_is_http_url` live in utils.py — re-exported under their
# historical names so this module's callers don't have to change.
_safe_iso = to_iso
_is_http_url = is_http_url


# --- DB ----------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    init_db(PIPE.db_path)
    conn = sqlite3.connect(PIPE.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def ensure_default_sources(conn: sqlite3.Connection) -> None:
    for s in DEFAULT_SOURCES:
        conn.execute(
            """INSERT OR IGNORE INTO sources (name, feed_url, kind, enabled)
               VALUES (?, ?, ?, ?)""",
            (s["name"], s["feed_url"], s["kind"], s.get("enabled", 1)),
        )
        # Backfill `lang` for already-inserted sources (column added in 002).
        conn.execute(
            "UPDATE sources SET lang = ? WHERE name = ? AND lang IS NULL",
            (s.get("lang"), s["name"]),
        )
    conn.commit()


def upsert_item(conn: sqlite3.Connection, item: FeedItem) -> Tuple[bool, str]:
    """Returns (inserted, status). Skips invalid candidates (no http(s) URL)."""
    if not _is_http_url(item.url):
        log.warning("skip: invalid url for guid=%s", item.guid)
        return False, "skipped"

    cur = conn.execute(
        "SELECT id, status FROM candidates WHERE source_id=? AND guid=?",
        (item.source_id, item.guid),
    )
    row = cur.fetchone()
    if row:
        return False, row["status"]

    conn.execute(
        """INSERT INTO candidates
           (source_id, guid, url, title, summary, body, image_url,
            video_embed_url, published_at, status, safety_status, lang)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', 'review', ?)""",
        (
            item.source_id, item.guid, item.url, item.title,
            item.summary[: PIPE.max_summary_chars],
            item.body[: PIPE.max_body_chars],
            item.image_url[:1024],
            item.video_embed_url[:1024],
            item.published_at,
            item.lang,
        ),
    )
    return True, "new"


@retry(
    reraise=True,
    stop=stop_after_attempt(PIPE.http_retries),
    wait=wait_exponential(multiplier=PIPE.http_retry_backoff_seconds, min=1, max=10),
    retry=retry_if_exception_type((requests.RequestException,)),
)
def fetch_feed(url: str) -> feedparser.FeedParserDict:
    log.info("GET %s", url)
    r = requests.get(
        url,
        timeout=PIPE.http_timeout_seconds,
        headers={"User-Agent": PIPE.user_agent, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
    )
    r.raise_for_status()
    return feedparser.parse(r.content)


def parse_entry(source_id: int, entry, lang: str) -> Optional[FeedItem]:
    guid = entry.get("id") or entry.get("link") or entry.get("title")
    if not guid:
        return None

    url = entry.get("link", "") or ""
    title = _strip_html(entry.get("title", ""))
    raw_html = entry.get("summary", "") or entry.get("content", [{}])[0].get("value", "")
    summary = _strip_html(entry.get("summary", ""))
    body = _strip_html(raw_html)

    # No topic filter — we ingest whatever the feed gave us. LLM rejects later.

    image_url = (
        _first_image(raw_html)
        or (entry.get("media_thumbnail", [{}])[0].get("url") if entry.get("media_thumbnail") else "")
        or (entry.get("media_content", [{}])[0].get("url") if entry.get("media_content") else "")
    )
    video_embed_url = _first_video(raw_html)

    # Refine lang: prefer the entry's hint if RSS provides one, else source hint.
    entry_lang = (entry.get("language") or entry.get("dc_language") or "").strip().lower()
    if entry_lang.startswith("ru"):
        lang = "ru"
    elif entry_lang.startswith("en"):
        lang = "en"

    return FeedItem(
        source_id=source_id,
        guid=guid[:512],
        url=url,
        title=title[:512],
        summary=summary,
        body=body,
        image_url=image_url[:1024],
        video_embed_url=video_embed_url[:1024],
        published_at=_safe_iso(entry.get("published") or entry.get("updated")),
        lang=lang,
    )


# --- main loop ---------------------------------------------------------------
def run(max_items: int, only_source: Optional[str] = None) -> dict:
    stats = {"fetched_feeds": 0, "matched": 0, "inserted": 0, "skipped_duplicate": 0}
    conn = connect()
    ensure_default_sources(conn)

    sources = conn.execute("SELECT * FROM sources WHERE enabled=1").fetchall()
    if only_source:
        needle = only_source.strip().lower()
        sources = [s for s in sources if (s["name"] or "").lower() == needle]

    inserted_total = 0
    for src in sources:
        if max_items and inserted_total >= max_items:
            break
        try:
            feed = fetch_feed(src["feed_url"])
        except Exception as e:
            log.error("feed failed: %s — %s", src["name"], e)
            continue

        stats["fetched_feeds"] += 1
        src_lang = (src["lang"] if "lang" in src.keys() else None) or "en"
        for entry in feed.entries:
            item = parse_entry(src["id"], entry, src_lang)
            if not item:
                continue
            stats["matched"] += 1
            try:
                inserted, _status = upsert_item(conn, item)
                if inserted:
                    stats["inserted"] += 1
                    inserted_total += 1
                    log.info("+ new [%s] %s", src["name"], item.title[:80])
                else:
                    stats["skipped_duplicate"] += 1
            except Exception as e:
                log.error("db insert failed for %s: %s", item.url, e)
            if max_items and inserted_total >= max_items:
                break

    conn.commit()
    conn.close()
    log.info("done: %s", json.dumps(stats, ensure_ascii=False))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=PIPE.max_items_per_run,
                    help="max new candidates per run")
    ap.add_argument("--source", type=str, default=None,
                    help="only this source by name (case-insensitive)")
    args = ap.parse_args()
    run(args.max, args.source)
    return 0


if __name__ == "__main__":
    sys.exit(main())