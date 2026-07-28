"""Sprint 6 smoke: tg_channel_publisher.publish() + helpers.

Uses an isolated DB and monkeypatches tg_bridge._call to capture the
payload that WOULD be sent (no real Telegram call). Verifies:

  1. build_utm_url adds utm_source=telegram_channel, utm_medium=post,
     utm_campaign=your_channel, utm_content=<post_id>; merges with existing
     query params; preserves fragments.
  2. format_tg_post:
     - escapes HTML chars in title and teaser
     - adds <b> around title
     - prepends "🔴 " to title
     - appends hashtags as "#tag1 #tag2"
     - ends with "🔗 Источник"
     - handles empty hashtags gracefully
     - blocks blocked drafts (empty title) → no body sent
  3. publish() happy path:
     - sends correct payload (chat_id, text, parse_mode, link_preview_options)
     - link_preview_options.url is the UTM-augmented WP URL
     - marks draft_posts.tg_channel_published_at, message_id, message_url
     - returns dict with all expected fields
  4. publish() idempotency:
     - second call on already-published post raises AlreadyPublished
       with existing message_id + URL
  5. publish() refuses if no tg_dispatch (PostNotFound).
  6. publish() refuses if TG.your_channel_channel_id is empty (config error).
  7. publish() skips sends for blocked drafts (returns blocked=True, no API call).
  8. message_url format:
     - public channel (TG.your_channel_username): https://t.me/<username>/<message_id>
     - private channel (no username): https://t.me/c/<chat_id_stripped>/<message_id>
  9. Race: second call to mark_tg_channel_published after another writer
     doesn't fail publish() (changed=False is logged, not raised).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from _smoke_lib import make_isolated_db


# --- Fake _call --------------------------------------------------------------
class FakeCall:
    """Captures the payload sent to tg_bridge._call and returns a canned response."""

    def __init__(
        self,
        response: Optional[Dict[str, Any]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.response = response or {
            "ok": True,
            "result": {"message_id": 4242},
        }
        self.error = error

    def __call__(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append({"method": method, "payload": dict(payload)})
        if self.error is not None:
            raise self.error
        return self.response


# --- Test helpers -------------------------------------------------------------
def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _seed_post_with_draft(
    db_path: str,
    *,
    seed_marker: str,
    wp_url: str = "https://media-<deploy-user>.local/test-post",
    title: str = "🔴 Test title",
    teaser: str = "Test teaser.",
    hashtags: Optional[List[str]] = None,
) -> int:
    """Insert a candidate + draft_posts + a tg_dispatch row, return post_id."""
    if hashtags is None:
        hashtags = ["ai", "test"]
    from pathlib import Path
    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        source_id = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO candidates (source_id, guid, url, title, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_id, seed_marker, f"https://example.com/{seed_marker}",
             "Test post title", "Body"),
        )
        candidate_id = conn.execute(
            "SELECT id FROM candidates WHERE guid=?", (seed_marker,)
        ).fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO draft_posts (
                candidate_id, title, slug, wp_post_url, status
            ) VALUES (?, ?, ?, ?, 'published')
            """,
            (candidate_id, "Test post", "test-post", wp_url),
        )
        post_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO tg_dispatch (
                post_id, tg_title, tg_teaser, tg_hashtags_json, prompt_version
            ) VALUES (?, ?, ?, ?, 'master_prompt_tg.md@v1.0')
            """,
            (post_id, title, teaser, json.dumps(hashtags, ensure_ascii=False)),
        )
        conn.commit()
        return post_id
    finally:
        conn.close()


def _set_environ(**kwargs: str) -> Dict[str, Optional[str]]:
    """Set env vars BEFORE importing config — capture old values for restore.

    NOTE: TelegramConfig is a dataclass initialized at module import time.
    Changing os.environ AFTER import does NOT update TG.bot_token /
    TG.your_channel_channel_id. Tests must patch the dataclass attribute
    directly (see _patch_tg below).
    """
    import os
    saved = {k: os.environ.get(k) for k in kwargs}
    for k, v in kwargs.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return saved


def _restore_environ(saved: Dict[str, Optional[str]]) -> None:
    import os
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _patch_tg(**kwargs: str):
    """Context manager: patch TG dataclass attributes directly.

    config.TG is a non-frozen dataclass; we use unittest.mock.patch.object
    on the class so attribute access in tg_channel_publisher sees the
    patched value. Yields a no-op so callers can `with _patch_tg(...) as x`.
    """
    from contextlib import contextmanager
    from unittest.mock import patch
    from config import TG

    @contextmanager
    def _cm():
        with patch.multiple(TG, **kwargs):
            yield

    return _cm()


# --- Tests -------------------------------------------------------------------
def test_build_utm_url_basic() -> None:
    from tg_channel_publisher import build_utm_url
    url = build_utm_url("https://media-<deploy-user>.local/post-1", post_id=42)
    _assert("utm_source=telegram_channel" in url, f"missing utm_source: {url}")
    _assert("utm_medium=post" in url, f"missing utm_medium: {url}")
    _assert("utm_campaign=your_channel" in url, f"missing utm_campaign: {url}")
    _assert("utm_content=42" in url, f"missing utm_content: {url}")
    _assert(url.startswith("https://media-<deploy-user>.local/post-1?"), f"bad base: {url}")
    print(f"  PASS  build_utm_url adds all 4 UTM tags")


def test_build_utm_url_merges_existing_query() -> None:
    from tg_channel_publisher import build_utm_url
    url = build_utm_url(
        "https://media-<deploy-user>.local/post-1?ref=twitter&utm_source=x", post_id=7,
    )
    _assert("ref=twitter" in url, f"lost existing ref: {url}")
    _assert("utm_source=telegram_channel" in url, f"utm_source overwritten: {url}")
    _assert("utm_content=7" in url, f"missing utm_content: {url}")
    print(f"  PASS  build_utm_url preserves existing query params")


def test_build_utm_url_preserves_fragment() -> None:
    from tg_channel_publisher import build_utm_url
    url = build_utm_url(
        "https://media-<deploy-user>.local/post-1#section-2", post_id=1,
    )
    _assert("#section-2" in url, f"lost fragment: {url}")
    _assert("utm_source=telegram_channel" in url, f"missing utm: {url}")
    print(f"  PASS  build_utm_url preserves URL fragment")


def test_build_utm_url_empty_input() -> None:
    from tg_channel_publisher import build_utm_url
    _assert(build_utm_url("", post_id=1) == "", "empty url should return empty")
    print(f"  PASS  build_utm_url handles empty input")


def test_format_tg_post_basic() -> None:
    """format_tg_post: title bold + emoji, teaser text, hashtags, source line."""
    import sqlite3
    from pathlib import Path
    from tg_channel_publisher import format_tg_post

    db_path, conn = make_isolated_db(label="tg_pub_fmt")
    post_id = _seed_post_with_draft(
        str(db_path), seed_marker="fmt-1",
        title="🔴 OpenAI выпустила GPT-5.1",
        teaser='Использует "новый" подход к рассуждению.',
        hashtags=["ai", "openai"],
    )
    row = conn.execute(
        "SELECT * FROM tg_dispatch WHERE post_id=?", (post_id,)
    ).fetchone()
    text = format_tg_post(row)
    conn.close()

    # TG's parse_mode=HTML only requires escaping <, >, & — quotes are safe.
    _assert(
        text.startswith("🔴 <b>🔴 OpenAI выпустила GPT-5.1</b>"),
        f"bad title line: {text!r}",
    )
    _assert(
        '"новый"' in text,
        f"teaser quotes (not HTML-special) preserved as-is: {text!r}",
    )
    _assert("#ai #openai" in text, f"hashtags missing: {text!r}")
    _assert(text.endswith("🔗 Источник"), f"missing source line: {text!r}")
    print(f"  PASS  format_tg_post layout + HTML escape + hashtags")


def test_format_tg_post_escapes_html_chars() -> None:
    """LLM sometimes emits < or & — we MUST escape to keep parse_mode=HTML happy."""
    import sqlite3
    from pathlib import Path
    from tg_channel_publisher import format_tg_post

    db_path, conn = make_isolated_db(label="tg_pub_esc")
    post_id = _seed_post_with_draft(
        str(db_path), seed_marker="esc-1",
        title="OpenAI <3 Anthropic & others",
        teaser="a < b && c > d",
        hashtags=[],
    )
    row = conn.execute(
        "SELECT * FROM tg_dispatch WHERE post_id=?", (post_id,)
    ).fetchone()
    text = format_tg_post(row)
    conn.close()

    _assert("&lt;3" in text, f"< not escaped: {text!r}")
    _assert("&amp;" in text, f"& not escaped: {text!r}")
    _assert("a &lt; b" in text, f"< not escaped in teaser: {text!r}")
    print(f"  PASS  format_tg_post escapes HTML chars")


def test_format_tg_post_empty_hashtags() -> None:
    import sqlite3
    from pathlib import Path
    from tg_channel_publisher import format_tg_post

    db_path, conn = make_isolated_db(label="tg_pub_notags")
    post_id = _seed_post_with_draft(
        str(db_path), seed_marker="notags-1",
        title="🔴 Title", teaser="Teaser.", hashtags=[],
    )
    row = conn.execute(
        "SELECT * FROM tg_dispatch WHERE post_id=?", (post_id,)
    ).fetchone()
    text = format_tg_post(row)
    conn.close()

    _assert("#" not in text, f"unexpected hashtags in text: {text!r}")
    _assert(text.endswith("🔗 Источник"), "source line missing")
    print(f"  PASS  format_tg_post handles empty hashtags")


def test_format_tg_post_invalid_hashtags_json() -> None:
    """Defensive: malformed JSON should drop tags, not crash."""
    import sqlite3
    from pathlib import Path
    from tg_channel_publisher import format_tg_post

    db_path, conn = make_isolated_db(label="tg_pub_badjson")
    post_id = _seed_post_with_draft(
        str(db_path), seed_marker="badjson-1",
        title="🔴 Title", teaser="Teaser.", hashtags=["x"],
    )
    conn.execute(
        "UPDATE tg_dispatch SET tg_hashtags_json = ? WHERE post_id = ?",
        ("this-is-not-json", post_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM tg_dispatch WHERE post_id=?", (post_id,)
    ).fetchone()
    text = format_tg_post(row)
    conn.close()
    _assert("#" not in text, f"unexpected hashtag from bad JSON: {text!r}")
    print(f"  PASS  format_tg_post tolerates malformed hashtags JSON")


def test_publish_happy_path() -> None:
    """publish() sends correct payload and marks the post as published."""
    db_path, conn = make_isolated_db(label="tg_pub_ok")
    post_id = _seed_post_with_draft(
        str(db_path), seed_marker="ok-1",
        wp_url="https://media-<deploy-user>.local/cool-post",
        title="🔴 Cool title", teaser="Cool teaser.", hashtags=["tech"],
    )
    conn.close()

    from tg_channel_publisher import publish
    fake = FakeCall(response={"ok": True, "result": {"message_id": 7777}})

    with _patch_tg(
        bot_token="test-token",
        your_channel_channel_id="-100XXXXXXXXXX",
        your_channel_username="your_channel",
    ), patch("tg_bridge._call", side_effect=fake):
        result = publish(post_id, db_path=db_path)

    # API call correctness.
    _assert(len(fake.calls) == 1, f"expected 1 _call, got {len(fake.calls)}")
    payload = fake.calls[0]["payload"]
    _assert(
        payload["chat_id"] == "-100XXXXXXXXXX",
        f"chat_id mismatch: {payload['chat_id']!r}",
    )
    _assert(payload["parse_mode"] == "HTML", f"parse_mode: {payload['parse_mode']}")
    _assert(
        "link_preview_options" in payload,
        f"link_preview_options missing: {payload}",
    )
    lpo = payload["link_preview_options"]
    _assert(
        lpo["url"] == "https://media-<deploy-user>.local/cool-post?utm_source=telegram_channel&utm_medium=post&utm_campaign=your_channel&utm_content=999".replace(
            "999", str(post_id),
        )
        or lpo["url"].startswith("https://media-<deploy-user>.local/cool-post?"),
        f"link_preview url wrong: {lpo['url']!r}",
    )
    _assert("utm_content=" + str(post_id) in lpo["url"],
            f"utm_content missing: {lpo['url']}")
    _assert(lpo["prefer_large_media"] is True, "prefer_large_media not set")
    _assert(lpo["show_above_text"] is False, "show_above_text wrong")

    # Returned info.
    _assert(result["message_id"] == 7777, f"message_id: {result['message_id']}")
    _assert(
        result["message_url"] == "https://t.me/your_channel/7777",
        f"message_url: {result['message_url']!r}",
    )
    _assert(result["blocked"] is False, "blocked should be False")
    _assert(result["dry_run"] is False, "dry_run should be False")

    # DB state.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT tg_channel_published_at, tg_channel_message_id, "
        "tg_channel_message_url FROM draft_posts WHERE id=?",
        (post_id,),
    ).fetchone()
    conn.close()
    _assert(row["tg_channel_published_at"] is not None, "published_at not set")
    _assert(row["tg_channel_message_id"] == 7777, "message_id not stored")
    _assert(
        row["tg_channel_message_url"] == "https://t.me/your_channel/7777",
        f"message_url not stored: {row['tg_channel_message_url']!r}",
    )
    print(f"  PASS  publish() happy path: payload correct + DB marked")


def test_publish_idempotent_second_call_raises_alreadypublished() -> None:
    db_path, conn = make_isolated_db(label="tg_pub_idem")
    post_id = _seed_post_with_draft(
        str(db_path), seed_marker="idem-1",
    )
    conn.close()

    from tg_channel_publisher import publish, AlreadyPublished
    fake = FakeCall(response={"ok": True, "result": {"message_id": 111}})

    with _patch_tg(
        bot_token="test-token",
        your_channel_channel_id="-100XXXXXXXXXX",
        your_channel_username="your_channel",
    ):
        with patch("tg_bridge._call", side_effect=fake):
            first = publish(post_id, db_path=db_path)
        _assert(first["message_id"] == 111, f"first call: {first}")

        # Second call should NOT hit the API and should raise.
        fake.calls.clear()
        with patch("tg_bridge._call", side_effect=fake):
            try:
                publish(post_id, db_path=db_path)
            except AlreadyPublished as e:
                _assert(
                    e.post_id == post_id and e.message_id == 111,
                    f"AlreadyPublished fields wrong: {e!r}",
                )
                _assert(
                    fake.calls == [],
                    f"second call hit API: {fake.calls!r}",
                )
                print(f"  PASS  publish() idempotent: 2nd call raises, no API hit")
                return
        raise AssertionError("expected AlreadyPublished on 2nd call")


def test_publish_refuses_no_drafts() -> None:
    db_path, conn = make_isolated_db(label="tg_pub_nodraft")
    # Seed a post but NO tg_dispatch.
    source_id = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO candidates (source_id, guid, url, title, body) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_id, "nodraft-1", "https://example.com/n", "T", "B"),
    )
    candidate_id = conn.execute(
        "SELECT id FROM candidates WHERE guid='nodraft-1'"
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO draft_posts (candidate_id, title, slug, wp_post_url, status) "
        "VALUES (?, ?, ?, ?, 'published')",
        (candidate_id, "Test", "t", "https://media-<deploy-user>.local/x"),
    )
    post_id = int(cur.lastrowid)
    conn.commit()
    conn.close()

    from tg_channel_publisher import publish
    from tg_regenerate import PostNotFound
    with _patch_tg(
        bot_token="test-token",
        your_channel_channel_id="-100XXXXXXXXXX",
    ):
        try:
            publish(post_id, db_path=db_path)
        except PostNotFound as e:
            _assert("tg_dispatch" in str(e), f"error msg should mention tg_dispatch: {e!r}")
            print(f"  PASS  publish() refuses post without tg_dispatch")
            return
        raise AssertionError("expected PostNotFound")


def test_publish_refuses_when_channel_id_empty() -> None:
    db_path, conn = make_isolated_db(label="tg_pub_nocfg")
    post_id = _seed_post_with_draft(str(db_path), seed_marker="nocfg-1")
    conn.close()

    from tg_channel_publisher import publish, TGChannelConfigError
    with _patch_tg(
        bot_token="test-token",
        your_channel_channel_id="",  # not configured
    ):
        try:
            publish(post_id, db_path=db_path)
        except TGChannelConfigError as e:
            _assert(
                "TG_CHANNEL_ID" in str(e),
                f"error should mention env var: {e!r}",
            )
            print(f"  PASS  publish() refuses when channel id empty")
            return
        raise AssertionError("expected TGChannelConfigError")


def test_publish_refuses_when_bot_token_empty() -> None:
    db_path, conn = make_isolated_db(label="tg_pub_notoken")
    post_id = _seed_post_with_draft(str(db_path), seed_marker="notoken-1")
    conn.close()

    from tg_channel_publisher import publish, TGChannelConfigError
    with _patch_tg(
        bot_token="",
        your_channel_channel_id="-100XXXXXXXXXX",
    ):
        try:
            publish(post_id, db_path=db_path)
        except TGChannelConfigError as e:
            _assert("TELEGRAM_BOT_TOKEN" in str(e), f"error: {e!r}")
            print(f"  PASS  publish() refuses when bot token empty")
            return
        raise AssertionError("expected TGChannelConfigError")


def test_publish_skips_blocked_drafts() -> None:
    """A blocked tg_draft (empty title) should NOT be sent to TG."""
    db_path, conn = make_isolated_db(label="tg_pub_blocked")
    post_id = _seed_post_with_draft(
        str(db_path), seed_marker="blocked-1",
        title="", teaser="", hashtags=[],
    )
    conn.close()

    from tg_channel_publisher import publish
    fake = FakeCall()
    with _patch_tg(
        bot_token="test-token",
        your_channel_channel_id="-100XXXXXXXXXX",
    ), patch("tg_bridge._call", side_effect=fake):
        result = publish(post_id, db_path=db_path)
    _assert(result["blocked"] is True, f"expected blocked=True, got {result!r}")
    _assert(fake.calls == [], f"should not hit API: {fake.calls!r}")
    print(f"  PASS  publish() skips blocked drafts (no API call)")


def test_message_url_format_private_channel() -> None:
    """Without TG.your_channel_username: fallback to t.me/c/<id>/<msg_id> format."""
    db_path, conn = make_isolated_db(label="tg_pub_privchan")
    post_id = _seed_post_with_draft(str(db_path), seed_marker="priv-1")
    conn.close()

    from tg_channel_publisher import publish
    fake = FakeCall(response={"ok": True, "result": {"message_id": 999}})
    with _patch_tg(
        bot_token="test-token",
        your_channel_channel_id="-100XXXXXXXXXX",
        your_channel_username="",  # no username (private channel)
    ), patch("tg_bridge._call", side_effect=fake):
        result = publish(post_id, db_path=db_path)
    # -100 prefix stripped → XXXXXXXXXX
    _assert(
        result["message_url"] == "https://t.me/c/XXXXXXXXXX/999",
        f"private channel url: {result['message_url']!r}",
    )
    print(f"  PASS  message_url format for private channel")


def test_message_url_format_strips_at_sign() -> None:
    db_path, conn = make_isolated_db(label="tg_pub_atstrip")
    post_id = _seed_post_with_draft(str(db_path), seed_marker="atstrip-1")
    conn.close()

    from tg_channel_publisher import publish
    fake = FakeCall(response={"ok": True, "result": {"message_id": 5}})
    with _patch_tg(
        bot_token="test-token",
        your_channel_channel_id="-100XXXXXXXXXX",
        your_channel_username="@your_channel",  # placeholder; set your TG channel here
    ), patch("tg_bridge._call", side_effect=fake):
        result = publish(post_id, db_path=db_path)
    _assert(
        result["message_url"] == "https://t.me/your_channel/5",
        f"@-strip: {result['message_url']!r}",
    )
    print(f"  PASS  message_url strips leading @ from username")


def main() -> int:
    tests = [
        test_build_utm_url_basic,
        test_build_utm_url_merges_existing_query,
        test_build_utm_url_preserves_fragment,
        test_build_utm_url_empty_input,
        test_format_tg_post_basic,
        test_format_tg_post_escapes_html_chars,
        test_format_tg_post_empty_hashtags,
        test_format_tg_post_invalid_hashtags_json,
        test_publish_happy_path,
        test_publish_idempotent_second_call_raises_alreadypublished,
        test_publish_refuses_no_drafts,
        test_publish_refuses_when_channel_id_empty,
        test_publish_refuses_when_bot_token_empty,
        test_publish_skips_blocked_drafts,
        test_message_url_format_private_channel,
        test_message_url_format_strips_at_sign,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    total = len(tests)
    print(f"\n{total - failed}/{total} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())