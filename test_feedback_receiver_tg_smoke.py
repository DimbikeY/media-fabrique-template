"""Sprint 6 smoke: feedback_receiver's TG-channel handlers + parse_command.

Verifies:
  1. parse_command recognises /approve_tg, /reject_tg, /edit_tg, /feedback_tg
     (and does NOT confuse them with /approve, /reject, /edit, /feedback).
  2. /approve_tg without id returns noop.
  3. /edit_tg without note text returns the bad-command hint (no LLM call).
  4. /feedback_tg without text returns the bad-command hint.
  5. _do_approve_tg on success returns message_id + URL, surfaces
     AlreadyPublished on 2nd call.
  6. _do_reject_tg persists trace; refuses missing post.
  7. _do_edit_tg calls tg_regenerate with the note; surfaces LLM error.
  8. _do_feedback_tg persists the comment; refuses missing post.
  9. _handle() dispatches the four _tg commands correctly.
 10. _do_help() output mentions all 4 _tg commands.
"""
from __future__ import annotations

import sqlite3
import sys
from typing import Any, Dict, Optional
from unittest.mock import patch

from _smoke_lib import make_isolated_db

# parse_command + _do_* helpers live in telegram_receiver; importing at
# module load is the historical pattern these tests depend on. (The
# functions are also imported lazily inside each test as a defensive
# measure against earlier module-reload skips in _force_dry_mode.)
from telegram_receiver import (  # noqa: E402
    parse_command,
    _do_approve_tg,
    _do_edit_tg,
    _do_feedback_tg,
    _do_help,
    _handle,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _seed_draft_post(db_path, *, marker: str = "seed-1") -> int:
    """Insert candidate + draft_posts; return the draft_posts.id."""
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
            "VALUES (?, ?, ?, ?, 'published')",
            (candidate_id, "T", "t", f"https://media-<deploy-user>.local/{marker}"),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _seed_tg_draft(
    db_path: str,
    post_id: int,
    *,
    title: str = "🔴 TG title",
    teaser: str = "TG teaser.",
    hashtags=None,
    status: str = "text_generated",
) -> int:
    """Insert a tg_dispatch row.

    Default ``status='text_generated'`` matches what
    :func:`_do_approve_tg` consumes (mark_tg_dispatch_approved only
    transitions out of awaiting_approval / text_generated / approved).
    For 'pending_tg_text' state tests, pass ``status='pending_tg_text'``.
    """
    from pathlib import Path
    if hashtags is None:
        hashtags = ["ai", "test"]
    import json as _json
    conn = sqlite3.connect(Path(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO tg_dispatch (post_id, tg_title, tg_teaser, "
            "tg_hashtags_json, prompt_version, status) VALUES (?, ?, ?, ?, ?, ?)",
            (post_id, title, teaser, _json.dumps(hashtags),
             "master_prompt_tg.md@v1.0", status),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _patch_tg(**kwargs: str):
    """Patch TG dataclass attributes for tests."""
    from contextlib import contextmanager
    from unittest.mock import patch as _patch
    from config import TG
    @contextmanager
    def _cm():
        with _patch.multiple(TG, **kwargs):
            yield
    return _cm()


def _patch_publish_db_path(db_path):
    """Make PIPE.db_path point at the test DB for modules that read it."""
    from unittest.mock import patch as _patch
    from config import PIPE
    return _patch.object(PIPE, "db_path", db_path)


# --- parse_command tests -----------------------------------------------------
def test_parse_command_recognises_tg_variants() -> None:
    """Sprint cleanup 2026-07-21: /reject_tg is gone. /approve_tg, /edit_tg
    and /feedback_tg still parse."""
    cases = [
        ("/approve_tg 42", ("approve_tg", {"draft_post_id": 42})),
        ("/feedback_tg 9 нравится заголовок", ("feedback_tg", {"draft_post_id": 9, "note": "нравится заголовок"})),
        ("/edit_tg 5 сделай покороче", ("edit_tg", {"draft_post_id": 5, "feedback": "сделай покороче"})),
        # /reject_tg now drops to noop (handled by test_reject_dropped_to_noop).
        ("/reject_tg 7", ("noop", {"raw": "/reject_tg 7"})),
        ("/reject_tg 7 слишком резко", ("noop", {"raw": "/reject_tg 7 слишком резко"})),
    ]
    for text, expected in cases:
        cmd, kwargs = parse_command(text)
        _assert(
            cmd == expected[0] and kwargs == expected[1],
            f"parse_command({text!r}) = ({cmd!r}, {kwargs!r}), "
            f"expected {expected!r}",
        )
    print(f"  PASS  parse_command recognises the live _tg variants with kwargs")


def test_parse_command_no_confusion_with_wp_twins() -> None:
    """Make sure /approve_tg never accidentally matches /approve etc.

    /reject_tg now drops to noop (Sprint cleanup 2026-07-21)."""
    cmd, kwargs = parse_command("/approve 42")
    _assert(cmd == "approve", f"/approve matched as {cmd!r}")
    cmd, kwargs = parse_command("/approve_tg 42")
    _assert(cmd == "approve_tg", f"/approve_tg matched as {cmd!r}")
    # /reject_tg must NOT collide with /edit_tg or any other TG twin.
    cmd, kwargs = parse_command("/reject_tg 7 note")
    _assert(cmd == "noop", f"/reject_tg should now drop to noop, got {cmd!r}")
    cmd, kwargs = parse_command("/edit 5 текст")
    _assert(cmd == "edit", f"/edit matched as {cmd!r}")
    cmd, kwargs = parse_command("/edit_tg 5 текст")
    _assert(cmd == "edit_tg", f"/edit_tg matched as {cmd!r}")
    print(f"  PASS  parse_command: _tg variants do not collide with WP twins")


def test_parse_command_edit_tg_without_note_is_noop() -> None:
    """Sprint 6m.2 pattern: /edit without note is noop (re-prompt hint)."""
    cmd, _ = parse_command("/edit_tg 5")
    # edit_tg without feedback should fall through to noop (we don't want
    # to accidentally call LLM with empty feedback).
    _assert(cmd == "noop", f"/edit_tg without note should be noop, got {cmd!r}")
    print(f"  PASS  parse_command: /edit_tg without note = noop")


def test_parse_command_approve_tg_without_id_is_noop() -> None:
    cmd, _ = parse_command("/approve_tg")
    _assert(cmd == "noop", f"/approve_tg without id should be noop, got {cmd!r}")
    cmd, _ = parse_command("/approve_tg ")
    _assert(cmd == "noop", f"/approve_tg with empty args should be noop, got {cmd!r}")
    print(f"  PASS  parse_command: /approve_tg without id = noop")


# --- _do_edit_tg without note: returns bad-command hint --------------------
def test_edit_tg_without_note_returns_hint() -> None:
    """Don't hit LLM when there's nothing to feed it."""
    import telegram_receiver as feedback_receiver
    from telegram_receiver import _do_edit_tg
    reply = _do_edit_tg(5, feedback=None)
    _assert("нужен" in reply.lower() or "правк" in reply.lower(),
            f"expected bad-command hint, got: {reply!r}")
    print(f"  PASS  _do_edit_tg without note returns hint, no LLM call")


def test_feedback_tg_without_text_returns_hint() -> None:
    import telegram_receiver as feedback_receiver
    from telegram_receiver import _do_feedback_tg
    reply = _do_feedback_tg(5, note=None)
    _assert("нужен" in reply.lower() or "комментар" in reply.lower(),
            f"expected bad-command hint, got: {reply!r}")
    print(f"  PASS  _do_feedback_tg without text returns hint")


# --- _do_approve_tg happy path ----------------------------------------------
# Sprint Y (DD 2026-07-20 22:33 MSK) split the monolithic publish flow:
# /approve_tg only flips tg_dispatch.status → 'approved'; tick=publish_tg
# is responsible for Telegraph IV + TG-channel sendMessage. The tests
# below verify the status side-effect and the "no TG preview yet" /
# "already approved" edge cases — NOT a synchronous sendMessage call.
def test_approve_tg_happy_path() -> None:
    db_path, conn = make_isolated_db(label="fr_tg_app_ok")
    post_id = _seed_draft_post(str(db_path), marker="app-ok-1")
    _seed_tg_draft(str(db_path), post_id)  # status='text_generated' (default)
    conn.close()

    with _patch_publish_db_path(db_path):
        reply = _do_approve_tg(post_id)
    _assert("📡" in reply, f"missing success marker: {reply!r}")
    _assert("одобрено" in reply, f"missing 'одобрено': {reply!r}")
    _assert(str(post_id) in reply, f"missing post_id in reply: {reply!r}")
    # And the side-effect: tg_dispatch row flipped to 'approved'.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM tg_dispatch WHERE post_id=?", (post_id,),
    ).fetchone()
    conn.close()
    _assert(
        row and row["status"] == "approved",
        f"tg_dispatch.status must be 'approved', got {row['status'] if row else None!r}",
    )
    print(f"  PASS  _do_approve_tg → tg_dispatch.status='approved'")


def test_approve_tg_alreadypublished_surfaces_existing() -> None:
    db_path, conn = make_isolated_db(label="fr_tg_app_dup")
    post_id = _seed_draft_post(str(db_path), marker="app-dup-1")
    _seed_tg_draft(str(db_path), post_id,
                    status="published_tg")  # already shipped
    conn.close()

    with _patch_publish_db_path(db_path):
        reply = _do_approve_tg(post_id)
    _assert("уже опубликовано" in reply, f"expected idempotency note: {reply!r}")
    print(f"  PASS  _do_approve_tg on already-published_tg → friendly reply")


def test_approve_tg_no_tg_preview_yet() -> None:
    """No tg_dispatch row → 'нет TG-preview' (Sprint Y edge case)."""
    db_path, conn = make_isolated_db(label="fr_tg_app_none")
    post_id = _seed_draft_post(str(db_path), marker="app-none-1")
    # NB: no _seed_tg_draft call — no row in tg_dispatch.
    conn.close()

    with _patch_publish_db_path(db_path):
        reply = _do_approve_tg(post_id)
    _assert("❌" in reply or "нет TG" in reply,
            f"expected missing-row error, got: {reply!r}")
    print(f"  PASS  _do_approve_tg without tg_dispatch row → friendly ❌")


def test_approve_tg_config_error_friendly() -> None:
    """/approve_tg on a non-existent post returns 'не найден'."""
    db_path, conn = make_isolated_db(label="fr_tg_app_cfg")
    # No draft post at all.
    conn.close()

    with _patch_publish_db_path(db_path):
        reply = _do_approve_tg(99999)
    _assert("❌" in reply, f"expected error emoji: {reply!r}")
    _assert("не найден" in reply, f"missing 'не найден': {reply!r}")
    print(f"  PASS  _do_approve_tg on missing post → friendly ❌")


def test_approve_tg_blocked_draft_returns_skip_message() -> None:
    """Sprint Y changed /approve_tg from synchronous publish to status-flip;
    a 'blocked' TG draft is the responsibility of tick=publish_tg now.
    We just verify the row stays in 'rejected_tg' territory if a
    rejected_tg row exists, or that /approve_tg returns the empty-state
    message if the dispatcher has nothing to approve yet."""
    db_path, conn = make_isolated_db(label="fr_tg_app_blk")
    post_id = _seed_draft_post(str(db_path), marker="app-blk-1")
    # Seed an empty (pending) TG text; /approve_tg has nothing to do yet.
    _seed_tg_draft(str(db_path), post_id, title="", teaser="",
                    status="pending_tg_text")
    conn.close()

    with _patch_publish_db_path(db_path):
        reply = _do_approve_tg(post_id)
    # Either 'нет TG-preview' / 'state=pending_tg_text' (depending on
    # state-machine impl) or '🚫 stop' (if rejection check is run). We
    # only assert that it's NOT a success message.
    _assert("📡" not in reply or "одобрено" not in reply,
            f"approve_tg should not silently approve an empty draft, got: {reply!r}")
    print(f"  PASS  _do_approve_tg on pending_tg_text → no silent approve")


# --- _do_edit_tg happy path --------------------------------------------------
def test_edit_tg_calls_tg_regenerate_with_note() -> None:
    db_path, conn = make_isolated_db(label="fr_tg_edit")
    post_id = _seed_draft_post(str(db_path), marker="edit-1")
    conn.close()

    import telegram_receiver as feedback_receiver

    from telegram_receiver import _do_edit_tg
    fake_result = {
        "post_id": post_id, "tg_draft_id": 42, "blocked": False,
        "tg_title": "🔴 Updated title", "tg_teaser": "Updated teaser.",
        "tg_hashtags": ["ai", "news"], "prompt_version": "v1.0", "note": "tt",
    }
    with _patch_publish_db_path(db_path), patch(
        "tg_regenerate.tg_regenerate",
        return_value=fake_result,
    ) as regen_mock:
        reply = _do_edit_tg(post_id, feedback="tighten wording")

    _assert(regen_mock.called, "tg_regenerate was not called")
    _assert(
        regen_mock.call_args.kwargs.get("note") == "tighten wording",
        f"note not passed: {regen_mock.call_args!r}",
    )
    _assert("♻️" in reply, f"missing regen emoji: {reply!r}")
    _assert("Updated title" in reply, f"new title not in reply: {reply!r}")
    _assert("Updated teaser" in reply, f"new teaser not in reply: {reply!r}")
    _assert("#ai" in reply and "#news" in reply, f"hashtags not in reply: {reply!r}")
    print(f"  PASS  _do_edit_tg calls tg_regenerate with note + shows preview")


def test_edit_tg_propagates_llm_error() -> None:
    db_path, conn = make_isolated_db(label="fr_tg_edit_err")
    post_id = _seed_draft_post(str(db_path), marker="edit-err-1")
    conn.close()

    import telegram_receiver as feedback_receiver

    from telegram_receiver import _do_edit_tg
    with _patch_publish_db_path(db_path), patch(
        "tg_regenerate.tg_regenerate",
        side_effect=RuntimeError("provider down"),
    ):
        reply = _do_edit_tg(post_id, feedback="whatever")
    _assert("❌" in reply, f"missing error emoji: {reply!r}")
    _assert("provider down" in reply, f"missing underlying error: {reply!r}")
    print(f"  PASS  _do_edit_tg propagates LLM error as ❌")


# --- _do_feedback_tg ---------------------------------------------------------
def test_feedback_tg_persists_comment() -> None:
    """Sprint cleanup 2026-07-21: /feedback_tg writes to the new
    tg_dispatch.feedback_note column instead of draft_posts.error_reason."""
    db_path, conn = make_isolated_db(label="fr_tg_fb")
    post_id = _seed_draft_post(str(db_path), marker="fb-1")
    td_id = _seed_tg_draft(str(db_path), post_id)
    conn.close()

    import telegram_receiver as feedback_receiver

    from telegram_receiver import _do_feedback_tg
    with _patch_publish_db_path(db_path):
        reply = _do_feedback_tg(post_id, note="заголовок норм, тизер скучный")

    _assert("💬" in reply, f"missing feedback emoji: {reply!r}")
    _assert("скучный" in reply, f"comment text missing: {reply!r}")

    from pathlib import Path
    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT feedback_note FROM tg_dispatch WHERE id=?", (td_id,),
    ).fetchone()
    conn.close()
    _assert(
        row["feedback_note"] and "скучный" in row["feedback_note"],
        f"feedback_note not written: {row['feedback_note']!r}",
    )
    print(f"  PASS  _do_feedback_tg persists comment in tg_dispatch.feedback_note")


def test_feedback_tg_missing_post() -> None:
    db_path, conn = make_isolated_db(label="fr_tg_fb404")
    conn.close()
    import telegram_receiver as feedback_receiver
    from telegram_receiver import _do_feedback_tg
    with _patch_publish_db_path(db_path):
        reply = _do_feedback_tg(99999, note="anything")
    _assert("❌" in reply, f"missing error emoji: {reply!r}")
    _assert("не найден" in reply or "не сгенерирован" in reply, f"missing 'not found' / 'not generated': {reply!r}")
    print(f"  PASS  _do_feedback_tg refuses missing post / no dispatch row")


# --- _handle() dispatch ------------------------------------------------------
def test_handle_dispatches_approve_tg() -> None:
    """End-to-end: feed an update dict through _handle; verify tg_dispatch
    row flipped to 'approved'. Sprint Y: /approve_tg no longer publishes
    synchronously; tick=publish_tg does that next."""
    db_path, conn = make_isolated_db(label="fr_tg_h_app")
    post_id = _seed_draft_post(str(db_path), marker="h-app-1")
    _seed_tg_draft(str(db_path), post_id)  # text_generated
    conn.close()

    update = {
        "message": {
            "chat": {"id": 12345},
            "text": f"/approve_tg {post_id}",
            "message_id": 1,
        }
    }
    with _patch_publish_db_path(db_path):
        reply = _handle(update)

    _assert(reply is not None, "_handle returned None")
    _assert("📡" in reply, f"TG-flavoured reply expected: {reply!r}")
    _assert("одобрено" in reply, f"approve marker missing: {reply!r}")
    # And the side-effect: row flipped.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    status = conn.execute(
        "SELECT status FROM tg_dispatch WHERE post_id=?", (post_id,),
    ).fetchone()["status"]
    conn.close()
    _assert(status == "approved", f"tg_dispatch.status must be 'approved', got {status!r}")
    print(f"  PASS  _handle dispatches /approve_tg → tg_dispatch.status='approved'")


def test_handle_dispatches_edit_tg() -> None:
    db_path, conn = make_isolated_db(label="fr_tg_h_edit")
    post_id = _seed_draft_post(str(db_path), marker="h-edit-1")
    conn.close()

    update = {
        "message": {
            "chat": {"id": 12345},
            "text": f"/edit_tg {post_id} покороче",
            "message_id": 1,
        }
    }
    import telegram_receiver as feedback_receiver
    from telegram_receiver import _handle
    fake_result = {
        "post_id": post_id, "tg_draft_id": 7, "blocked": False,
        "tg_title": "🔴 Short", "tg_teaser": "Teaser",
        "tg_hashtags": ["tech"], "prompt_version": "v1.0", "note": "покороче",
    }
    with _patch_publish_db_path(db_path), patch(
        "tg_regenerate.tg_regenerate", return_value=fake_result,
    ):
        reply = _handle(update)
    _assert(reply is not None, "_handle returned None")
    _assert("♻️" in reply, f"regen reply expected: {reply!r}")
    _assert("Short" in reply, f"new title missing: {reply!r}")
    print(f"  PASS  _handle dispatches /edit_tg")


def test_handle_dispatches_feedback_tg() -> None:
    db_path, conn = make_isolated_db(label="fr_tg_h_fb")
    post_id = _seed_draft_post(str(db_path), marker="h-fb-1")
    _seed_tg_draft(str(db_path), post_id)  # dispatcher needs a tg_dispatch row
    conn.close()

    update = {
        "message": {
            "chat": {"id": 12345},
            "text": f"/feedback_tg {post_id} норм заголовок",
            "message_id": 1,
        }
    }
    with _patch_publish_db_path(db_path):
        reply = _handle(update)
    _assert(reply is not None, "_handle returned None")
    _assert("💬" in reply, f"feedback reply expected: {reply!r}")
    _assert("норм заголовок" in reply, f"text missing: {reply!r}")
    print(f"  PASS  _handle dispatches /feedback_tg")


# --- /help mentions _tg commands --------------------------------------------
def test_help_mentions_tg_commands() -> None:
    text = _do_help()
    for cmd in ("/approve_tg", "/edit_tg", "/feedback_tg"):
        _assert(cmd in text, f"/help missing {cmd}: {text!r}")
    # Sprint cleanup 2026-07-21: /reject_tg was removed. The cleanup
    # note at the bottom of /help may still mention it historically,
    # so split off that trailing note before asserting.
    live_block = text.split("<i>Sprint cleanup")[0]
    _assert("/reject_tg" not in live_block, f"/help live block must not mention /reject_tg: {live_block!r}")
    print(f"  PASS  /help mentions only the live _tg commands")


# --- FEEDBACK_BOT_COMMANDS includes _tg --------------------------------------
def test_bot_commands_registered() -> None:
    """Sprint 6 setMyCommands: bot menu must list _tg commands so DD
    sees them in the TG app's command picker.

    feedback_webhook.py has a hard-coded /var/log/<project> path that
    doesn't exist on the dev Mac, so we can't import it directly. Instead
    we read telegram_receiver.py and check the _BOT_COMMANDS constant.
    This is fine because the constant is a module-level literal —
    it doesn't depend on runtime state.
    """
    from pathlib import Path
    src = Path("telegram_receiver.py").read_text(encoding="utf-8")
    for cmd in ("approve_tg", "edit_tg", "feedback_tg"):
        _assert(
            f'"{cmd}"' in src,
            f"telegram_receiver.py _BOT_COMMANDS missing {cmd!r}",
        )
    # reject_tg was removed by Sprint cleanup 2026-07-21.
    _assert(
        '"reject_tg"' not in src,
        f"telegram_receiver.py _BOT_COMMANDS must NOT include reject_tg",
    )
    print(f"  PASS  _BOT_COMMANDS lists the live _tg commands")


def main() -> int:
    tests = [
        test_parse_command_recognises_tg_variants,
        test_parse_command_no_confusion_with_wp_twins,
        test_parse_command_edit_tg_without_note_is_noop,
        test_parse_command_approve_tg_without_id_is_noop,
        test_edit_tg_without_note_returns_hint,
        test_feedback_tg_without_text_returns_hint,
        test_approve_tg_happy_path,
        test_approve_tg_alreadypublished_surfaces_existing,
        test_approve_tg_config_error_friendly,
        test_approve_tg_blocked_draft_returns_skip_message,
        test_edit_tg_calls_tg_regenerate_with_note,
        test_edit_tg_propagates_llm_error,
        test_feedback_tg_persists_comment,
        test_feedback_tg_missing_post,
        test_handle_dispatches_approve_tg,
        test_handle_dispatches_edit_tg,
        test_handle_dispatches_feedback_tg,
        test_help_mentions_tg_commands,
        test_bot_commands_registered,
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