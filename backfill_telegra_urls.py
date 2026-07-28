#!/usr/bin/env python3
"""Sprint X backfill: generate Telegraph IV URLs for posts that were
published to @your_channel before migration 019_draft_posts_telegra_url,
and write them back into draft_posts.tg_channel_telegra_url.

Run as <deploy-user> on VPS-B after `git pull`:
    sudo -u <deploy-user> .venv/bin/python backfill_telegra_urls.py

Run with --dry-run first to preview.

DD 2026-07-20 08:28 MSK.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from html.parser import HTMLParser

from dotenv import load_dotenv

from config import PIPE
import telegraph


class _HTMLStrip(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


def _strip(html_str: str | None) -> str:
    if not html_str:
        return ""
    p = _HTMLStrip()
    p.feed(html_str)
    return p.text()


def _hashtags(cats_json: str | None, teaser: str | None) -> list[str]:
    import json
    if cats_json:
        try:
            parsed = json.loads(cats_json)
            if isinstance(parsed, list):
                return [c for c in parsed if isinstance(c, str)][:5]
        except Exception:
            pass
    # Fallback: try to use the first word of the teaser as a tag.
    if teaser:
        first = teaser.split()[0].strip("#").lower()[:20]
        if first:
            return [first]
    return ["news"]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(PIPE.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--only-ids",
        type=int,
        nargs="*",
        default=None,
        help="Process only these draft_posts IDs (default: all unprocessed)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be processed; do not write to DB",
    )
    ap.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Per-post retries on Telegraph timeout (default 4)",
    )
    args = ap.parse_args()

    load_dotenv("/opt/<deploy-user>/.env")

    db_path = PIPE.db_path
    print(f"DB: {db_path}")

    rows_query = """
        SELECT id, slug, title, content_html, wp_post_id, wp_post_url,
               tg_channel_message_id, tg_channel_message_url,
               telegram_teaser, categories_json
          FROM draft_posts
         WHERE tg_channel_published_at IS NOT NULL
           AND tg_channel_telegra_url IS NULL
           {extra}
    """
    if args.only_ids:
        placeholders = ",".join("?" * len(args.only_ids))
        rows_query = rows_query.format(extra=f"AND id IN ({placeholders})")
        params: tuple = tuple(args.only_ids)
    else:
        rows_query = rows_query.format(extra="")
        params = ()

    with _connect() as conn:
        c = conn.cursor()
        candidates = list(c.execute(rows_query, params))
        print(f"Candidates: {len(candidates)}")

        if args.dry_run:
            for row in candidates:
                print(
                    f"  post={row['id']} slug={(row['slug'] or '')[:50]} "
                    f"wp_id={row['wp_post_id']} tg_msg={row['tg_channel_message_id']}"
                )
            return 0

        ok = 0
        failed = 0
        for row in candidates:
            post_id = row["id"]
            title = row["title"] or "(без заголовка)"
            body_text = _strip(row["content_html"])[:5000]
            wp_url = row["wp_post_url"]
            tags = _hashtags(row["categories_json"], row["telegram_teaser"])

            url = None
            last_err: Exception | None = None
            for attempt in range(1, args.retries + 1):
                try:
                    url = telegraph.create_telegraph_page(
                        title=title,
                        body_text=body_text,
                        source_url=None,  # 6d.8: Telegraph footer points to WP
                        wp_url=wp_url,
                        tag_list=tags,
                        image_src=None,
                    )
                    if url:
                        break
                except Exception as e:
                    last_err = e
                    print(f"  post={post_id} attempt={attempt}/{args.retries}: {type(e).__name__}: {e}")
                    if attempt < args.retries:
                        # Telegraph API has shown transient timeouts after
                        # long idle. Backoff exponentially (5, 15, 30s) — bigger
                        # than the per-call retry inside _http_post_with_retry.
                        wait = 5 * (3 ** (attempt - 1))
                        print(f"    sleeping {wait}s before retry")
                        time.sleep(wait)

            if url:
                c.execute(
                    "UPDATE draft_posts SET tg_channel_telegra_url=? WHERE id=?",
                    (url, post_id),
                )
                conn.commit()
                print(f"✓ post={post_id} telegra={url}")
                ok += 1
            else:
                print(f"✗ post={post_id} FAILED: {last_err!r}")
                failed += 1

        print(f"\nDone. ok={ok} failed={failed}")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
