"""Sprint 6 smoke: tg_channel_trigger.after_wp_approve() + preview formatting.

Verifies:
  1. format_preview_message: layout + commands list + escape
  2. format_preview_message: blocked draft shows stop-topic hint, hides /approve_tg
  3. format_preview_message: wp_url truncated to <=60 chars
  4. after_wp_approve happy path:
     - calls tg_regenerate with the post_id (and note if provided)
     - sends to TG.chat_id + thread_tg_validation
     - returns dict with all expected fields
  5. after_wp_approve no-config short-circuits to None (no error).
  6. after_wp_approve: LLM error → TGTriggerError, no TG send.
  7. after_wp_approve: TG send error → TGTriggerError.
  8. _do_approve (WP-flow) calls after_wp_approve and includes TG line.
  9. _do_approve for already-approved post does NOT re-trigger.
 10. _do_approve handles TG trigger failure gracefully (WP still ✅).
"""
import telegram_receiver as feedback_receiver
from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any, Dict, Optional
from unittest.mock import patch

from _smoke_lib import make_isolated_db


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _seed_post(db_path, *, marker: str = "trig-1",
               wp_url: str = "https://media-<deploy-user>.local/post-x") -> int:
    from pathlib import Path
    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        source_id = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO candidates (source_id, guid, url, title, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_id, marker, f"https://example.com/{marker}", "T", "B"),
        )
        candidate_id = conn.execute(
            "SELECT id FROM candidates WHERE guid=?", (marker,)
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO draft_posts (candidate_id, title, slug, wp_post_url, status) "
            "VALUES (?, ?, ?, ?, 'draft')",
            (candidate_id, "T", "t", wp_url),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _patch_tg(**kwargs):
    """Patch TG dataclass attributes for tests."""
    from contextlib import contextmanager
    from unittest.mock import patch as _patch
    from config import TG
    @contextmanager
    def _cm():
        with _patch.multiple(TG, **kwargs):
            yield
    return _cm()


def _patch_pipe_db_path(db_path):
    from unittest.mock import patch as _patch
    from config import PIPE
    return _patch.object(PIPE, "db_path", db_path)


# --- format_preview_message tests -------------------------------------------
def test_format_preview_message_happy() -> None:
    from tg_channel_trigger import format_preview_message
    tg_draft = {
        "blocked": False,
        "tg_title": "🔴 OpenAI выпустила GPT-5.1",
        "tg_teaser": "Улучшенное рассуждение. Доступно в Plus.",
        "tg_hashtags": ["ai", "openai"],
    }
    text = format_preview_message(42, tg_draft)
    _assert("📡" in text, f"missing header emoji: {text!r}")
    _assert("#42" in text, f"missing post_id in header: {text!r}")
    _assert("OpenAI выпустила GPT-5.1" in text, f"title missing: {text!r}")
    _assert("Plus." in text, f"teaser missing: {text!r}")
    _assert("#ai" in text and "#openai" in text, f"hashtags missing: {text!r}")
    # Standard command list.
    for cmd in ("/approve_tg 42", "/reject_tg 42",
                "/edit_tg 42", "/feedback_tg 42"):
        _assert(cmd in text, f"missing command: {cmd} in {text!r}")
    print(f"  PASS  format_preview_message happy layout")


def test_format_preview_message_blocked() -> None:
    """Blocked draft shows stop-topic hint and HIDES /approve_tg (DD
    shouldn't publish a blocked draft)."""
    from tg_channel_trigger import format_preview_message
    tg_draft = {
        "blocked": True, "reason": "violent",
        "tg_title": "", "tg_teaser": "", "tg_hashtags": [],
    }
    text = format_preview_message(7, tg_draft)
    _assert("🚫" in text, f"missing blocked emoji: {text!r}")
    _assert("violent" in text, f"missing reason: {text!r}")
    _assert(
        "/approve_tg 7" not in text,
        f"/approve_tg should be HIDDEN for blocked draft: {text!r}",
    )
    # Other commands should still be available (reject / edit / feedback).
    for cmd in ("/reject_tg 7", "/edit_tg 7", "/feedback_tg 7"):
        _assert(cmd in text, f"missing command: {cmd}")
    print(f"  PASS  format_preview_message blocked: hides /approve_tg")


def test_format_preview_message_url_truncated() -> None:
    from tg_channel_trigger import format_preview_message
    long_url = "https://media-<deploy-user>.local/" + "x" * 100
    text = format_preview_message(
        1, {"blocked": False, "tg_title": "T", "tg_teaser": "s",
            "tg_hashtags": []}, wp_url=long_url,
    )
    # Should contain "..." truncation marker.
    _assert("..." in text, f"long URL not truncated: {text!r}")
    # The full long URL should NOT appear (only truncated version).
    _assert(
        long_url not in text,
        f"full long URL should not appear: {text!r}",
    )
    print(f"  PASS  format_preview_message truncates long wp_url")


def test_format_preview_message_escapes_html() -> None:
    from tg_channel_trigger import format_preview_message
    text = format_preview_message(1, {
        "blocked": False,
        "tg_title": "A < B & C",
        "tg_teaser": "X < Y && Z > W",
        "tg_hashtags": ["tech"],
    })
    _assert("&lt;" in text, f"< not escaped: {text!r}")
    _assert("&amp;" in text, f"& not escaped: {text!r}")
    print(f"  PASS  format_preview_message escapes HTML")


# --- after_wp_approve happy path --------------------------------------------
def test_after_wp_approve_sends_to_validation_topic() -> None:
    db_path, conn = make_isolated_db(label="trig_ok")
    post_id = _seed_post(str(db_path), marker="ok-1")
    conn.close()

    from tg_channel_trigger import after_wp_approve
    fake_regen_result = {
        "post_id": post_id, "tg_draft_id": 99, "blocked": False,
        "tg_title": "🔴 X", "tg_teaser": "Y.", "tg_hashtags": ["a"],
        "prompt_version": "v1.0", "note": None,
    }
    fake_send_response = {"ok": True, "result": {"message_id": 555}}
    send_calls = []

    def fake_call(method, payload):
        send_calls.append({"method": method, "payload": dict(payload)})
        return fake_send_response

    with _patch_tg(
        bot_token="test-token",
        chat_id="-100XXXXXXXXXX",
        thread_tg_validation="760",
    ), _patch_pipe_db_path(db_path), patch(
        "tg_regenerate.tg_regenerate", return_value=fake_regen_result,
    ), patch("tg_bridge._call", side_effect=fake_call):
        result = after_wp_approve(post_id)

    _assert(result is not None, "after_wp_approve returned None")
    _assert(result["post_id"] == post_id, f"post_id: {result['post_id']}")
    _assert(result["preview_sent"] is True, "preview_sent should be True")
    _assert(result["tg_chat_id"] == "-100XXXXXXXXXX", f"chat_id: {result['tg_chat_id']!r}")
    _assert(result["tg_thread_id"] == 760, f"thread_id: {result['tg_thread_id']!r}")
    _assert(result["message_id"] == 555, f"message_id: {result['message_id']}")
    _assert(result["blocked"] is False, "blocked flag wrong")

    # Verify the actual send payload.
    _assert(len(send_calls) == 1, f"expected 1 send, got {len(send_calls)}")
    sent = send_calls[0]
    _assert(sent["method"] == "sendMessage", f"method: {sent['method']}")
    _assert(
        sent["payload"]["chat_id"] == "-100XXXXXXXXXX",
        f"chat_id in payload: {sent['payload']['chat_id']!r}",
    )
    _assert(
        sent["payload"]["message_thread_id"] == 760,
        f"thread_id in payload: {sent['payload']['message_thread_id']}",
    )
    _assert(sent["payload"]["parse_mode"] == "HTML", "parse_mode")
    # Preview body must include the new draft content + command list.
    body = sent["payload"]["text"]
    _assert("📡" in body and "🔴 X" in body and "Y." in body,
            f"preview body missing content: {body!r}")
    _assert(f"/approve_tg {post_id}" in body, "approve_tg command missing")
    print(f"  PASS  after_wp_approve happy path: chat_id + thread_id correct")


def test_after_wp_approve_passes_note() -> None:
    db_path, conn = make_isolated_db(label="trig_note")
    post_id = _seed_post(str(db_path), marker="note-1")
    conn.close()

    from tg_channel_trigger import after_wp_approve
    fake_regen_result = {
        "post_id": post_id, "tg_draft_id": 1, "blocked": False,
        "tg_title": "T", "tg_teaser": "s", "tg_hashtags": [],
        "prompt_version": "v1.0", "note": "make it shorter",
    }
    regen_calls = []

    def fake_regen(pid, **kw):
        regen_calls.append({"pid": pid, **kw})
        return fake_regen_result

    fake_call = lambda method, payload: {"ok": True, "result": {"message_id": 1}}
    with _patch_tg(bot_token="t", chat_id="c", thread_tg_validation="760"), \
         _patch_pipe_db_path(db_path), \
         patch("tg_regenerate.tg_regenerate", side_effect=fake_regen), \
         patch("tg_bridge._call", side_effect=fake_call):
        after_wp_approve(post_id, note="make it shorter")

    _assert(
        regen_calls[0].get("note") == "make it shorter",
        f"note not passed to tg_regenerate: {regen_calls[0]!r}",
    )
    print(f"  PASS  after_wp_approve passes note through to tg_regenerate")


def test_after_wp_approve_blocked_draft_omits_approve_command() -> None:
    db_path, conn = make_isolated_db(label="trig_blk")
    post_id = _seed_post(str(db_path), marker="blk-1")
    conn.close()

    from tg_channel_trigger import after_wp_approve
    fake_regen_result = {
        "post_id": post_id, "tg_draft_id": 1, "blocked": True,
        "reason": "violent", "tg_title": "", "tg_teaser": "",
        "tg_hashtags": [], "prompt_version": "v1.0", "note": None,
    }
    send_calls = []
    def fake_call(method, payload):
        send_calls.append(payload)
        return {"ok": True, "result": {"message_id": 1}}

    with _patch_tg(bot_token="t", chat_id="c", thread_tg_validation="760"), \
         _patch_pipe_db_path(db_path), \
         patch("tg_regenerate.tg_regenerate", return_value=fake_regen_result), \
         patch("tg_bridge._call", side_effect=fake_call):
        result = after_wp_approve(post_id)

    _assert(result["blocked"] is True, f"blocked flag: {result['blocked']}")
    body = send_calls[0]["text"]
    _assert("🚫" in body, f"missing blocked emoji: {body!r}")
    _assert(f"/approve_tg {post_id}" not in body,
            f"/approve_tg should be hidden: {body!r}")
    print(f"  PASS  after_wp_approve blocked draft: hides /approve_tg")


def test_after_wp_approve_no_config_returns_none() -> None:
    """Empty TG.thread_tg_validation → no-op (returns None). Dev/CI."""
    db_path, conn = make_isolated_db(label="trig_nocfg")
    post_id = _seed_post(str(db_path), marker="nocfg-1")
    conn.close()

    from tg_channel_trigger import after_wp_approve
    with _patch_tg(
        bot_token="t",
        chat_id="c",
        thread_tg_validation="",  # not configured
    ), _patch_pipe_db_path(db_path):
        result = after_wp_approve(post_id)
    _assert(result is None, f"expected None, got {result!r}")
    print(f"  PASS  after_wp_approve no-config short-circuits to None")


def test_after_wp_approve_llm_error_raises_trigger_error() -> None:
    db_path, conn = make_isolated_db(label="trig_llmerr")
    post_id = _seed_post(str(db_path), marker="llmerr-1")
    conn.close()

    from tg_channel_trigger import after_wp_approve, TGTriggerError
    send_calls = []
    def fake_call(method, payload):
        send_calls.append(payload)
        return {"ok": True, "result": {"message_id": 1}}

    with _patch_tg(bot_token="t", chat_id="c", thread_tg_validation="760"), \
         _patch_pipe_db_path(db_path), \
         patch("tg_regenerate.tg_regenerate",
               side_effect=RuntimeError("provider down")), \
         patch("tg_bridge._call", side_effect=fake_call):
        try:
            after_wp_approve(post_id)
        except TGTriggerError as e:
            _assert("provider down" in str(e), f"underlying error not propagated: {e!r}")
            _assert(send_calls == [], f"should NOT send on LLM error: {send_calls!r}")
            print(f"  PASS  after_wp_approve LLM error → TGTriggerError, no TG send")
            return
        raise AssertionError("expected TGTriggerError")


def test_after_wp_approve_send_error_raises_trigger_error() -> None:
    db_path, conn = make_isolated_db(label="trig_senderr")
    post_id = _seed_post(str(db_path), marker="senderr-1")
    conn.close()

    from tg_channel_trigger import after_wp_approve, TGTriggerError
    fake_regen_result = {
        "post_id": post_id, "tg_draft_id": 1, "blocked": False,
        "tg_title": "T", "tg_teaser": "s", "tg_hashtags": [],
        "prompt_version": "v1.0", "note": None,
    }
    def fake_call(method, payload):
        raise RuntimeError("network unreachable")

    with _patch_tg(bot_token="t", chat_id="c", thread_tg_validation="760"), \
         _patch_pipe_db_path(db_path), \
         patch("tg_regenerate.tg_regenerate", return_value=fake_regen_result), \
         patch("tg_bridge._call", side_effect=fake_call):
        try:
            after_wp_approve(post_id)
        except TGTriggerError as e:
            _assert("network unreachable" in str(e), f"error: {e!r}")
            print(f"  PASS  after_wp_approve send error → TGTriggerError")
            return
        raise AssertionError("expected TGTriggerError")


# --- _do_approve integration -------------------------------------------------
def test_do_approve_calls_trigger_on_fresh_approval() -> None:
    """End-to-end: /approve for a draft → calls after_wp_approve and
    reply text mentions TG line."""
    db_path, conn = make_isolated_db(label="do_app_trig")
    post_id = _seed_post(str(db_path), marker="do-app-1")
    conn.close()

    # record_review returns the *previous* status. To trigger the
    # fresh-approval branch we need it to return 'draft' (meaning the
    # post WAS draft and has now transitioned to 'approved').
    fake_regen_result = {
        "post_id": post_id, "tg_draft_id": 1, "blocked": False,
        "tg_title": "🔴 X", "tg_teaser": "Y.", "tg_hashtags": [],
        "prompt_version": "v1.0", "note": None,
    }
    fake_call = lambda method, payload: {"ok": True, "result": {"message_id": 1}}

    with _patch_tg(bot_token="t", chat_id="c", thread_tg_validation="760"), \
         _patch_pipe_db_path(db_path), \
         patch("tg_bridge.record_review", return_value="draft"), \
         patch("tg_bridge._dd_username", return_value="dd"), \
         patch("tg_regenerate.tg_regenerate", return_value=fake_regen_result), \
         patch("tg_bridge._call", side_effect=fake_call):
        reply = _do_approve(post_id, note=None)

    _assert("одобрен" in reply, f"expected approval line: {reply!r}")
    _assert("📡" in reply, f"expected TG line in reply: {reply!r}")
    _assert("760" in reply, f"expected thread id 760 in reply: {reply!r}")
    _assert(f"/approve_tg {post_id}" in reply, "expected /approve_tg hint in reply")
    print(f"  PASS  _do_approve happy path triggers TG preview")


def test_do_approve_already_approved_does_not_re_trigger() -> None:
    """If post was already approved, the trigger branch is skipped."""
    db_path, conn = make_isolated_db(label="do_app_dup")
    post_id = _seed_post(str(db_path), marker="do-dup-1")
    conn.close()

    trigger_calls = []

    def fake_trigger(post_id, **kw):
        trigger_calls.append(post_id)
        return {"preview_sent": True}

    with _patch_tg(bot_token="t", chat_id="c", thread_tg_validation="760"), \
         _patch_pipe_db_path(db_path), \
         patch("tg_bridge.record_review", return_value="approved"), \
         patch("tg_bridge._dd_username", return_value="dd"), \
         patch("tg_channel_trigger.after_wp_approve", side_effect=fake_trigger):
        reply = _do_approve(post_id, note=None)

    _assert(trigger_calls == [], f"trigger should NOT run for already-approved: {trigger_calls!r}")
    _assert("уже одобрен" in reply, f"expected already-approved note: {reply!r}")
    print(f"  PASS  _do_approve already-approved does NOT re-trigger")


def test_do_approve_trigger_failure_does_not_fail_approve() -> None:
    """If the trigger raises, the WP /approve still returns 👍 (best-effort)."""
    db_path, conn = make_isolated_db(label="do_app_fail")
    post_id = _seed_post(str(db_path), marker="do-fail-1")
    conn.close()


    with _patch_tg(bot_token="t", chat_id="c", thread_tg_validation="760"), \
         _patch_pipe_db_path(db_path), \
         patch("tg_bridge.record_review", return_value="draft"), \
         patch("tg_bridge._dd_username", return_value="dd"), \
         patch("tg_channel_trigger.after_wp_approve",
               side_effect=Exception("trigger blew up")):
        reply = _do_approve(post_id, note=None)

    _assert("одобрен" in reply, f"WP approve should still succeed: {reply!r}")
    _assert("⚠️" in reply or "не отправлен" in reply,
            f"reply should mention trigger failure gracefully: {reply!r}")
    print(f"  PASS  _do_approve handles trigger failure gracefully")


def main() -> int:
    tests = [
        test_format_preview_message_happy,
        test_format_preview_message_blocked,
        test_format_preview_message_url_truncated,
        test_format_preview_message_escapes_html,
        test_after_wp_approve_sends_to_validation_topic,
        test_after_wp_approve_passes_note,
        test_after_wp_approve_blocked_draft_omits_approve_command,
        test_after_wp_approve_no_config_returns_none,
        test_after_wp_approve_llm_error_raises_trigger_error,
        test_after_wp_approve_send_error_raises_trigger_error,
        test_do_approve_calls_trigger_on_fresh_approval,
        test_do_approve_already_approved_does_not_re_trigger,
        test_do_approve_trigger_failure_does_not_fail_approve,
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