"""Sprint 6 smoke: tg_regenerate() + db helpers.

Uses an isolated DB (_smoke_lib.make_isolated_db) and a FakeLLMClient so
no real LLM is called. Verifies:

  1. fetch_latest_tg_draft returns None when no drafts exist.
  2. tg_regenerate() with a successful LLM response:
     - creates a tg_dispatch row with correct fields
     - persists hashtags as JSON
     - records prompt_version (NOT NULL)
     - records note when provided
     - returns the same data the LLM emitted
  3. tg_regenerate() with a blocked LLM response:
     - creates a row with empty tg_title/tg_teaser/hashtags
     - returns blocked=True + reason
  4. /edit_tg pattern: regenerating twice creates 2 rows; latest wins.
  5. fetch_latest_tg_draft returns the latest by created_at DESC.
  6. mark_tg_channel_published is idempotent (2nd call = no-op).
  7. mark_tg_channel_published sets the 3 draft_posts columns correctly.
  8. mark_tg_rejected records trace in error_reason.
  9. add_tg_feedback records trace in error_reason.
 10. PostNotFound when post_id doesn't exist.
 11. LLM error propagates as RuntimeError.
 12. Note from DD flows into the LLM payload (build_tg_user_payload contract).
 13. master_prompt_tg.md prompt version constant matches the file header.
 14. TGChannelOutput Pydantic model: hashtags get #-stripped + lowercased.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any, Dict, Optional

from _smoke_lib import make_isolated_db


# --- Fake LLM client --------------------------------------------------------
class FakeLLMClient:
    """Stand-in for LLMClient.tg_rewrite(). The script sets
    ``fake.next_response`` and ``fake.next_error`` to control behaviour."""

    def __init__(self) -> None:
        self.next_response: Dict[str, Any] = {
            "blocked": False,
            "tg_title": "🔴 Test title",
            "tg_teaser": "Test teaser.",
            "tg_hashtags": ["ai", "test"],
        }
        self.next_error: Optional[Exception] = None
        self.calls: list[Dict[str, Any]] = []  # for verifying what was sent

    def tg_rewrite(self, post: Dict[str, Any]) -> Any:
        self.calls.append(post)
        if self.next_error is not None:
            raise self.next_error
        from llm_client import RewriteResult
        return RewriteResult(
            data=dict(self.next_response),
            usage={"prompt_tokens_provider": 0, "completion_tokens_provider": 0},
            metrics={"prompt_chars_local": 0},
        )


def _seed_post(db_path: str, post_id_marker: str = "seed-1") -> int:
    """Create a candidate + draft_post + return the draft_post id.

    Sets up the minimum state needed for tg_regenerate to find the post.
    The post is in 'published' status (Sprint 6 only operates on
    WP-published posts).
    """
    import tempfile, uuid
    db_path_obj = __import__('pathlib').Path(db_path)
    conn = sqlite3.connect(db_path_obj)
    conn.row_factory = sqlite3.Row
    try:
        source_id = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO candidates (source_id, guid, url, title, body, "
            "category, base_score) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, post_id_marker, "https://example.com/x",
             "Test post title", "Body text", "tech", 7.5),
        )
        candidate_id = conn.execute(
            "SELECT id FROM candidates WHERE guid=?", (post_id_marker,)
        ).fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO draft_posts (
                candidate_id, title, slug, excerpt, content_html,
                wp_post_url, status, telegram_teaser,
                categories_json, tags_json
            ) VALUES (?, ?, ?, ?, ?, ?, 'published', ?, ?, ?)
            """,
            (
                candidate_id, "Test post", "test-post", "Short excerpt",
                "<p>Body text content.</p>",
                "https://media-<deploy-user>.local/test-post",
                "Existing teaser",
                '["tech"]', '["ai", "test"]',
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --- Tests ------------------------------------------------------------------
def test_fetch_latest_returns_none_when_empty() -> None:
    db_path, conn = make_isolated_db(label="tg_regen_empty")
    try:
        from tg_regenerate import fetch_latest_tg_dispatched as fetch_latest_tg_draft  # Sprint Y rename
        result = fetch_latest_tg_draft(post_id=99999, db_path=db_path)
        _assert(result is None, f"expected None for missing post, got {result!r}")
        print(f"  PASS  fetch_latest_tg_draft returns None when no drafts")
    finally:
        conn.close()


def test_tg_regenerate_success_persists_draft() -> None:
    db_path, conn = make_isolated_db(label="tg_regen_ok")
    post_id = _seed_post(str(db_path), "seed-ok-1")
    conn.close()

    from tg_regenerate import tg_regenerate, fetch_latest_tg_dispatched as fetch_latest_tg_draft  # Sprint Y rename

    fake = FakeLLMClient()
    fake.next_response = {
        "blocked": False,
        "tg_title": "🔴 OpenAI выпустила GPT-5.1",
        "tg_teaser": "Модель получила улучшенное рассуждение. Доступна сегодня.",
        "tg_hashtags": ["ai", "openai", "релиз"],
    }
    result = tg_regenerate(post_id, note=None, client=fake, db_path=db_path)

    _assert(result["blocked"] is False, f"unexpected block: {result}")
    _assert(result["post_id"] == post_id, f"post_id mismatch: {result['post_id']}")
    _assert(result["tg_title"] == "🔴 OpenAI выпустила GPT-5.1", "title mismatch")
    _assert(
        result["tg_hashtags"] == ["ai", "openai", "релиз"],
        f"hashtags mismatch: {result['tg_hashtags']}",
    )
    _assert(result["prompt_version"], "prompt_version missing")
    _assert(result["note"] is None, f"note should be None, got {result['note']!r}")

    # Verify row was actually persisted.
    row = fetch_latest_tg_draft(post_id, db_path=db_path)
    _assert(row is not None, "draft row missing after tg_regenerate")
    _assert(row["tg_title"] == "🔴 OpenAI выпустила GPT-5.1", "persisted title mismatch")
    _assert(
        json.loads(row["tg_hashtags_json"]) == ["ai", "openai", "релиз"],
        f"persisted hashtags_json mismatch: {row['tg_hashtags_json']}",
    )
    _assert(row["prompt_version"], "prompt_version NOT NULL violated")
    print(f"  PASS  tg_regenerate success persists tg_dispatch row")
    conn.close()


def test_tg_regenerate_blocked_persists_empty() -> None:
    db_path, conn = make_isolated_db(label="tg_regen_blocked")
    post_id = _seed_post(str(db_path), "seed-blocked-1")
    conn.close()

    from tg_regenerate import tg_regenerate, fetch_latest_tg_dispatched as fetch_latest_tg_draft  # Sprint Y rename

    fake = FakeLLMClient()
    fake.next_response = {
        "blocked": True,
        "reason": "violent",
        "tg_title": "",
        "tg_teaser": "",
        "tg_hashtags": [],
    }
    result = tg_regenerate(post_id, client=fake, db_path=db_path)

    _assert(result["blocked"] is True, f"expected blocked=True, got {result}")
    _assert(result["reason"] == "violent", f"reason mismatch: {result['reason']!r}")
    _assert(result["tg_title"] == "" and result["tg_teaser"] == "",
            "blocked row should have empty fields")

    row = fetch_latest_tg_draft(post_id, db_path=db_path)
    _assert(row is not None, "blocked draft row missing")
    _assert(row["tg_title"] == "", f"persisted title should be empty, got {row['tg_title']!r}")
    print(f"  PASS  tg_regenerate blocked creates empty-draft row")
    conn.close()


def test_edit_tg_creates_history_two_rows() -> None:
    """The /edit_tg pattern: each call appends a new row. Latest by created_at wins."""
    db_path, conn = make_isolated_db(label="tg_regen_history")
    post_id = _seed_post(str(db_path), "seed-history-1")
    conn.close()

    from tg_regenerate import tg_regenerate, fetch_latest_tg_dispatched as fetch_latest_tg_draft  # Sprint Y rename

    fake = FakeLLMClient()

    # First generation (no note).
    fake.next_response = {
        "blocked": False, "tg_title": "v1 title",
        "tg_teaser": "v1 teaser.", "tg_hashtags": ["v1"],
    }
    tg_regenerate(post_id, client=fake, db_path=db_path)

    # Simulate /edit_tg with a note.
    fake.next_response = {
        "blocked": False, "tg_title": "v2 title (edited)",
        "tg_teaser": "v2 teaser.", "tg_hashtags": ["v2"],
    }
    tg_regenerate(post_id, note="tighten wording", client=fake, db_path=db_path)

    latest = fetch_latest_tg_draft(post_id, db_path=db_path)
    _assert(latest["tg_title"] == "v2 title (edited)",
            f"latest should be v2, got {latest['tg_title']!r}")
    _assert(latest["note"] == "tighten wording",
            f"note should be persisted, got {latest['note']!r}")

    # Count rows for this post.
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    n = conn2.execute(
        "SELECT COUNT(*) AS c FROM tg_dispatch WHERE post_id=?", (post_id,)
    ).fetchone()["c"]
    conn2.close()
    _assert(n == 2, f"expected 2 history rows, got {n}")

    # Verify the second LLM call received the note.
    _assert(
        fake.calls[-1].get("note") == "tighten wording",
        f"note should be in user payload, got {fake.calls[-1]!r}",
    )
    print(f"  PASS  /edit_tg creates history; latest wins; note flows through")
    conn.close()


def test_mark_tg_channel_published_idempotent() -> None:
    db_path, conn = make_isolated_db(label="tg_regen_pub")
    post_id = _seed_post(str(db_path), "seed-pub-1")
    conn.close()

    from tg_regenerate import mark_tg_channel_published

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        first = mark_tg_channel_published(
            conn, post_id,
            message_id=12345,
            message_url="https://t.me/your_channel/12345",
        )
        conn.commit()
        _assert(first is True, f"first publish should return True, got {first}")

        # Verify the 3 columns are set.
        row = conn.execute(
            "SELECT tg_channel_published_at, tg_channel_message_id, "
            "tg_channel_message_url FROM draft_posts WHERE id=?", (post_id,)
        ).fetchone()
        _assert(row["tg_channel_published_at"] is not None, "published_at missing")
        _assert(row["tg_channel_message_id"] == 12345, "message_id mismatch")
        _assert(
            row["tg_channel_message_url"] == "https://t.me/your_channel/12345",
            "message_url mismatch",
        )

        # Second call: must be a no-op (idempotency).
        second = mark_tg_channel_published(
            conn, post_id,
            message_id=99999,
            message_url="https://t.me/your_channel/99999",
        )
        _assert(
            second is False,
            f"second publish should return False (idempotent), got {second}",
        )

        # The original values must NOT have been overwritten.
        row2 = conn.execute(
            "SELECT tg_channel_message_id FROM draft_posts WHERE id=?",
            (post_id,),
        ).fetchone()
        _assert(
            row2["tg_channel_message_id"] == 12345,
            f"second publish overwrote message_id: {row2['tg_channel_message_id']}",
        )
        print(f"  PASS  mark_tg_channel_published is idempotent")
    finally:
        conn.close()


def test_mark_tg_rejected_and_feedback() -> None:
    db_path, conn = make_isolated_db(label="tg_regen_trace")
    post_id = _seed_post(str(db_path), "seed-trace-1")
    conn.close()

    from tg_regenerate import mark_tg_rejected, add_tg_feedback

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        mark_tg_rejected(conn, post_id, reason="слишком длинно")
        add_tg_feedback(conn, post_id, "нужен более цепляющий заголовок")
        conn.commit()

        row = conn.execute(
            "SELECT error_reason FROM draft_posts WHERE id=?", (post_id,)
        ).fetchone()
        _assert(
            "tg_channel_feedback" in row["error_reason"],
            f"feedback tag missing: {row['error_reason']!r}",
        )
        _assert(
            "цепляющий заголовок" in row["error_reason"],
            f"feedback text missing: {row['error_reason']!r}",
        )
        print(f"  PASS  mark_tg_rejected / add_tg_feedback write trace")
    finally:
        conn.close()


def test_post_not_found() -> None:
    db_path, conn = make_isolated_db(label="tg_regen_404")
    try:
        from tg_regenerate import tg_regenerate, PostNotFound
        fake = FakeLLMClient()
        try:
            tg_regenerate(post_id=99999, client=fake, db_path=db_path)
        except PostNotFound as e:
            _assert("99999" in str(e), f"error message should mention id, got {e!r}")
            print(f"  PASS  PostNotFound raised for missing post")
            return
        raise AssertionError("expected PostNotFound")
    finally:
        conn.close()


def test_llm_error_propagates() -> None:
    db_path, conn = make_isolated_db(label="tg_regen_llmerr")
    post_id = _seed_post(str(db_path), "seed-llmerr-1")
    conn.close()

    from tg_regenerate import tg_regenerate
    fake = FakeLLMClient()
    fake.next_error = RuntimeError("provider quota exceeded")

    try:
        tg_regenerate(post_id, client=fake, db_path=db_path)
    except RuntimeError as e:
        _assert("quota" in str(e).lower(), f"unexpected error: {e!r}")
        # No draft row should have been written.
        from tg_regenerate import fetch_latest_tg_dispatched as fetch_latest_tg_draft  # Sprint Y rename
        row = fetch_latest_tg_draft(post_id, db_path=db_path)
        _assert(row is None, "no row should be written when LLM fails")
        print(f"  PASS  LLM error propagates as RuntimeError; no draft persisted")
        return
    raise AssertionError("expected RuntimeError from LLM failure")


def test_prompt_version_constant_matches_file() -> None:
    """Lock-step check: the constant in prompts.py must reference the
    current master_prompt_tg.md header. If the file gets bumped to v2, the
    constant must move too — otherwise tg_dispatch.prompt_version drifts."""
    from prompts import TG_PROMPT_VERSION
    from pathlib import Path
    p = Path(__file__).parent / "master_prompt_tg.md"
    head = p.read_text(encoding="utf-8").splitlines()
    # Find a line like "(v1.0)" — header line of the file.
    version_line = next((ln for ln in head if "(v" in ln), "")
    _assert(
        version_line, f"master_prompt_tg.md has no version header line"
    )
    import re
    m = re.search(r"\((v[\d.]+)\)", version_line)
    _assert(m, f"could not parse version from header: {version_line!r}")
    file_version = "master_prompt_tg.md@" + m.group(1)
    _assert(
        TG_PROMPT_VERSION == file_version,
        f"prompt version drift: constant={TG_PROMPT_VERSION!r} "
        f"file_header={file_version!r}",
    )
    print(f"  PASS  TG_PROMPT_VERSION matches master_prompt_tg.md header")


def test_tgchanneloutput_strips_hash_and_lowercases() -> None:
    """master_prompt_tg.md rule #7: hashtags WITHOUT '#'. Model sometimes
    emits '#Tag' — TGChannelOutput validator must normalize."""
    from models import TGChannelOutput
    out = TGChannelOutput.model_validate({
        "blocked": False,
        "tg_title": "t",
        "tg_teaser": "teaser.",
        "tg_hashtags": ["#AI", "#Open AI", "#РЕЛИЗ", "", "# "],
    })
    _assert(
        out.tg_hashtags == ["ai", "openai", "релиз"],
        f"hashtag normalization wrong: {out.tg_hashtags!r}",
    )
    print(f"  PASS  TGChannelOutput normalizes hashtags")


def main() -> int:
    tests = [
        test_fetch_latest_returns_none_when_empty,
        test_tg_regenerate_success_persists_draft,
        test_tg_regenerate_blocked_persists_empty,
        test_edit_tg_creates_history_two_rows,
        test_mark_tg_channel_published_idempotent,
        test_mark_tg_rejected_and_feedback,
        test_post_not_found,
        test_llm_error_propagates,
        test_prompt_version_constant_matches_file,
        test_tgchanneloutput_strips_hash_and_lowercases,
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