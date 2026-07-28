"""Smoke tests for telegram_receiver (Sprint cleanup 2026-07-21).

What's still tested:
  * /approve   <draft_post_id>           — no note (cleanup removed [note] arg)
  * /edit      <draft_post_id> <правка>  — overwrites draft, persists feedback_note
  * /feedback  <wp_post_id>   <note>     — writes draft_posts.feedback_note
  * /feedback_tg <draft_post_id> <note>  — writes tg_dispatch.feedback_note
  * /help                              — banner reflects new command set
  * /bad → re-prompt hint
  * parse_command() honours @botname suffix (group forms)
  * DD safety: mid-sentence slashes are NOT executed

What was REMOVED (Sprint cleanup 2026-07-21):
  * /reject  <draft_post_id> [note]
  * /reject_tg
  * /down legacy shim
  * feedback_signals table + INSERT paths + digest
  * aggregate_feedback + _fmt_feedback in morning_report
  * feedback_digest.py tick + cron + archival dir

Run: .venv/bin/python test_feedback_receiver_smoke.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import _smoke_lib


def _force_dry_mode() -> None:
    """No real Telegram API + no real DD username filter."""
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    os.environ["TG_CHAT_ID"] = ""
    os.environ["TG_THREAD_PUBLISHED"] = ""
    os.environ["TG_THREAD_FEEDBACK"] = ""
    os.environ["TG_THREAD_MORNING_REPORT"] = ""
    os.environ["TG_THREAD_IDEAS"] = ""
    os.environ["TG_THREAD_DRAFTS"] = ""
    os.environ["TG_DD_USERNAME"] = "your_tg_username"
    for mod in ("config", "tg_bridge", "morning_report",
                "feedback_receiver", "notes_ideas"):
        sys.modules.pop(mod, None)


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open isolated DB. make_isolated_db() applies migration 021 which
    drops feedback_signals and adds feedback_note to both tables."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _insert_draft_post(conn: sqlite3.Connection, title: str, slug: str,
                       wp_post_id: int | None = None,
                       status: str = "draft") -> int:
    """Insert source + ready candidate + draft post. Returns draft_post.id."""
    src_id = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
    conn.execute(
        """INSERT INTO candidates(source_id, guid, url, title, status,
                             category, base_score, weight, published_at)
           VALUES (?, ?, ?, ?, 'ready',
                   'tech', 7.0, 6.5, datetime('now','-1h'))""",
        (src_id, f"guid-{title}", f"https://example/{title}", title),
    )
    candidate_id = conn.execute("SELECT id FROM candidates ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.execute(
        """INSERT INTO draft_posts(candidate_id, title, slug, status,
                             wp_post_id, wp_post_url,
                             created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?,
                   datetime('now'), datetime('now'))""",
        (candidate_id, title, slug, status, wp_post_id,
         f"https://example.com/?p={wp_post_id}" if wp_post_id else None),
    )
    conn.commit()
    return conn.execute("SELECT id FROM draft_posts ORDER BY id DESC LIMIT 1").fetchone()[0]


def _insert_tg_dispatch(conn: sqlite3.Connection, post_id: int,
                        status: str = "published_tg") -> int:
    """Insert a tg_dispatch row so /feedback_tg has something to attach to."""
    conn.execute(
        """INSERT INTO tg_dispatch(post_id, prompt_version, status)
           VALUES (?, 'test', ?)""",
        (post_id, status),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM tg_dispatch ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]


# --------------------------------------------------------------------------- #
# parse_command()
# --------------------------------------------------------------------------- #

def test_parse_commands():
    from telegram_receiver import parse_command
    cases = {
        # Sprint 6.6.1 + cleanup 2026-07-21 + reject restore 2026-07-21 10:48 MSK:
        "/approve 510": ("approve", {"draft_post_id": 510}),
        "/approve 510 коротко": ("approve", {"draft_post_id": 510}),  # note arg no longer parsed
        # /reject restored: parses to cmd='reject' with draft_post_id + optional note.
        "/reject 612\nслишком длинно": ("reject", {"draft_post_id": 612, "note": "слишком длинно"}),
        "/reject 612": ("reject", {"draft_post_id": 612, "note": None}),
        # /down legacy (Sprint 6.6): still noop
        "/down 612": ("noop", {"raw": "/down 612"}),
        # /feedback targets wp_post_id (the integer from ?p=17 in permalink).
        # /feedback without text parses fine; _do_feedback re-prompts on
        # note=None (refined validation moved to the handler).
        "/feedback 17": ("feedback", {"wp_post_id": 17, "note": None}),
        "/feedback 17 нужны источники":
            ("feedback", {"wp_post_id": 17, "note": "нужны источники"}),
        # Sprint 6m.2: /edit <id> <feedback> — feedback required.
        # /edit without a trailing note falls through to noop; the
        # handler is invoked via _do_edit_no_feedback() (see test below).
        "/edit 195 сделай введение короче":
            ("edit", {"draft_post_id": 195, "feedback": "сделай введение короче"}),
        "/edit 195": ("noop", {"raw": "/edit 195"}),
        "/help": ("help", {}),
        # /up legacy shim (kept one cycle) — note arg no longer parsed
        "/up 510": ("approve_legacy", {"draft_post_id": 510}),
        # Removed commands: /ideas, /status — drop straight to noop
        "/ideas нужна категория": ("noop", {"raw": "/ideas нужна категория"}),
        "/status": ("noop", {"raw": "/status"}),
        "/status 12": ("noop", {"raw": "/status 12"}),
        # Non-commands
        "привет": ("noop", {}),
        "/bla": ("noop", {"raw": "/bla"}),
        "/approve abc": ("noop", {"raw": "/approve abc"}),
        "/feedback abc": ("noop", {"raw": "/feedback abc"}),
        "": ("noop", {}),
    }
    for txt, expected in cases.items():
        got = parse_command(txt)
        assert got == expected, f"{txt!r}: got {got}, expected {expected}"
    print("  parse_commands: OK")


# --------------------------------------------------------------------------- #
# /approve
# --------------------------------------------------------------------------- #

def test_do_approve_via_parser(db_path: Path):
    """End-to-end: /approve → status='approved'. No feedback_signals side-effect."""
    import telegram_receiver as feedback_receiver
    import config as _config
    conn = _connect(db_path)
    post_id = _insert_draft_post(conn, "Test draft", "test-draft")
    conn.close()

    orig_path = _config.PIPE.db_path
    orig_flag = _config.PIPE_TICKS.wp_publish_auto_approve
    _config.PIPE.db_path = db_path
    _config.PIPE_TICKS.wp_publish_auto_approve = False  # manual-review mode
    try:
        result = feedback_receiver._do_approve(post_id)
    finally:
        _config.PIPE.db_path = orig_path
        _config.PIPE_TICKS.wp_publish_auto_approve = orig_flag

    assert "одобрен" in result or "approved" in result, result

    conn = _connect(db_path)
    status = conn.execute(
        "SELECT status FROM draft_posts WHERE id=?", (post_id,)
    ).fetchone()[0]
    assert status == "approved", status
    # No feedback_signals row should exist (table is gone in 021).
    has_sig_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='feedback_signals'"
    ).fetchone()
    assert has_sig_table is None, "feedback_signals should be gone"
    conn.close()
    print("  _do_approve via parser: OK")


def test_record_review_not_found(db_path: Path):
    """/approve on a non-existent post returns error text."""
    import telegram_receiver as feedback_receiver
    import config as _config
    orig_path = _config.PIPE.db_path
    orig_flag = _config.PIPE_TICKS.wp_publish_auto_approve
    _config.PIPE.db_path = db_path
    _config.PIPE_TICKS.wp_publish_auto_approve = False
    try:
        result = feedback_receiver._do_approve(99999)
    finally:
        _config.PIPE.db_path = orig_path
        _config.PIPE_TICKS.wp_publish_auto_approve = orig_flag
    assert "не найден" in result, result
    print("  _do_approve non-existent: OK")


def test_do_approve_noop_when_review_disabled(db_path: Path):
    """/approve is a no-op redirect when WP_PUBLISH_AUTO_APPROVE=true."""
    import telegram_receiver as feedback_receiver
    import config as _config

    conn = _connect(db_path)
    post_id = _insert_draft_post(conn, "Auto draft", "auto-draft")
    conn.close()

    orig_path = _config.PIPE.db_path
    orig_flag = _config.PIPE_TICKS.wp_publish_auto_approve
    _config.PIPE.db_path = db_path
    _config.PIPE_TICKS.wp_publish_auto_approve = True  # auto-publish mode
    try:
        result = feedback_receiver._do_approve(post_id)
    finally:
        _config.PIPE.db_path = orig_path
        _config.PIPE_TICKS.wp_publish_auto_approve = orig_flag

    assert "ревью выключено" in result or "auto-publish" in result, result
    conn = _connect(db_path)
    status = conn.execute(
        "SELECT status FROM draft_posts WHERE id=?", (post_id,)
    ).fetchone()[0]
    assert status == "draft", f"status must NOT transition when review disabled, got {status}"
    conn.close()
    print("  _do_approve noop when review disabled: OK")


# --------------------------------------------------------------------------- #
# /feedback (WP)
# --------------------------------------------------------------------------- #

def test_do_feedback_attached(db_path: Path):
    """/feedback <wp_post_id> <note> writes draft_posts.feedback_note."""
    import telegram_receiver as feedback_receiver
    conn = _connect(db_path)
    post_id = _insert_draft_post(conn, "Pub draft", "pub-draft", wp_post_id=42)
    conn.close()

    import config as _config
    orig_path = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        result = feedback_receiver._do_feedback(42, note="классная подборка источников")
    finally:
        _config.PIPE.db_path = orig_path

    assert "записан" in result, result

    conn = _connect(db_path)
    fb = conn.execute(
        "SELECT feedback_note FROM draft_posts WHERE id=?", (post_id,),
    ).fetchone()[0]
    assert fb == "классная подборка источников", fb
    conn.close()
    print("  _do_feedback WP attached: OK")


def test_do_feedback_requires_note(db_path: Path):
    """/feedback without note returns the re-prompt hint (no DB write)."""
    import telegram_receiver as feedback_receiver
    import config as _config
    conn = _connect(db_path)
    post_id = _insert_draft_post(conn, "NoNote draft", "nn", wp_post_id=43)
    conn.close()
    orig_path = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        result = feedback_receiver._do_feedback(43, note=None)
    finally:
        _config.PIPE.db_path = orig_path
    assert "❌" in result and "/feedback" in result, result
    # Nothing was written
    conn = _connect(db_path)
    fb = conn.execute(
        "SELECT feedback_note FROM draft_posts WHERE id=?", (post_id,),
    ).fetchone()[0]
    assert fb is None, fb
    conn.close()
    print("  _do_feedback WP requires note: OK")


def test_do_feedback_unknown_wp_post_id(db_path: Path):
    """/feedback <unknown wp_post_id> returns error, no DB write."""
    import telegram_receiver as feedback_receiver
    import config as _config
    orig_path = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        result = feedback_receiver._do_feedback(99999, note="привет")
    finally:
        _config.PIPE.db_path = orig_path
    assert "не найден" in result, result
    print("  _do_feedback WP unknown wp_post_id: OK")


# --------------------------------------------------------------------------- #
# /feedback_tg
# --------------------------------------------------------------------------- #

def test_do_feedback_tg_attached(db_path: Path):
    """/feedback_tg <draft_post_id> <note> writes tg_dispatch.feedback_note."""
    import telegram_receiver as feedback_receiver
    conn = _connect(db_path)
    post_id = _insert_draft_post(conn, "TG draft", "tg-slug")
    td_id = _insert_tg_dispatch(conn, post_id)
    conn.close()

    import config as _config
    orig_path = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        result = feedback_receiver._do_feedback_tg(post_id, note="слишком рекламно")
    finally:
        _config.PIPE.db_path = orig_path

    assert "записан" in result, result

    conn = _connect(db_path)
    fb = conn.execute(
        "SELECT feedback_note FROM tg_dispatch WHERE id=?", (td_id,),
    ).fetchone()[0]
    assert fb == "слишком рекламно", fb
    conn.close()
    print("  _do_feedback_tg attached: OK")


def test_do_feedback_tg_requires_note(db_path: Path):
    """/feedback_tg without note → re-prompt hint, no write."""
    import telegram_receiver as feedback_receiver
    import config as _config
    conn = _connect(db_path)
    post_id = _insert_draft_post(conn, "TG-NN draft", "tg-nn")
    td_id = _insert_tg_dispatch(conn, post_id)
    conn.close()
    orig_path = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        result = feedback_receiver._do_feedback_tg(post_id, note=None)
    finally:
        _config.PIPE.db_path = orig_path
    assert "❌" in result and "/feedback_tg" in result, result
    conn = _connect(db_path)
    fb = conn.execute(
        "SELECT feedback_note FROM tg_dispatch WHERE id=?", (td_id,),
    ).fetchone()[0]
    assert fb is None, fb
    conn.close()
    print("  _do_feedback_tg requires note: OK")


def test_do_feedback_tg_no_dispatch_row(db_path: Path):
    """/feedback_tg on a post with no tg_dispatch row → 'не сгенерирован'."""
    import telegram_receiver as feedback_receiver
    import config as _config
    conn = _connect(db_path)
    post_id = _insert_draft_post(conn, "NoTG draft", "no-tg")
    conn.close()
    orig_path = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        result = feedback_receiver._do_feedback_tg(post_id, note="hi")
    finally:
        _config.PIPE.db_path = orig_path
    assert "❌" in result and "tg_dispatch" in result, result
    print("  _do_feedback_tg no dispatch row: OK")


# --------------------------------------------------------------------------- #
# /edit (still works, persists feedback_note instead of feedback_signals)
# --------------------------------------------------------------------------- #

def test_edit_overwrites_content_via_record_edit(db_path: Path):
    """tg_bridge.record_edit() overwrites content + writes feedback_note."""
    import tg_bridge
    import config as _config
    orig = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    conn = _connect(db_path)
    pid = _insert_draft_post(conn, "Old title", "old-slug")
    conn.execute(
        "UPDATE draft_posts SET excerpt='old excerpt', "
        "content_html='<p>old</p>', meta_title='old meta', "
        "image_alt='old alt', categories_json='[\"tech\"]' WHERE id=?",
        (pid,))
    conn.commit()
    conn.close()
    new_data = {
        "title": "New title",
        "excerpt": "new excerpt",
        "content": "<p>new content</p>",
        "meta_title": "new meta",
        "meta_description": "new meta desc",
        "image_alt": "new alt",
        "image_prompt": "new prompt",
        "categories": ["science"],
        "tags": ["ai"],
        "telegram_teaser": "new teaser",
    }
    outcome = tg_bridge.record_edit(
        pid, new_data, "сделай короче",
        reviewer="your_tg_username", db_path=db_path,
    )
    _config.PIPE.db_path = orig
    assert outcome == "updated", outcome
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT title, excerpt, content_html, status, categories_json, "
        "feedback_note FROM draft_posts WHERE id=?", (pid,),
    ).fetchone()
    assert row[0] == "New title", row
    assert row[1] == "new excerpt", row
    assert row[2] == "<p>new content</p>", row
    assert row[3] == "draft", row
    assert row[5] == "сделай короче", row  # feedback persisted to feedback_note
    # feedback_signals row should NOT exist
    has_sig = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='feedback_signals'"
    ).fetchone()
    assert has_sig is None, has_sig
    conn.close()
    print("  record_edit overwrite + feedback_note: OK")


def test_edit_parser_no_feedback_hint(db_path: Path):
    """/edit <id> without feedback returns the re-prompt hint."""
    import telegram_receiver as feedback_receiver
    import config as _config
    orig = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        # Empty/None feedback → handler returns the re-prompt hint.
        result = feedback_receiver._do_edit(99999, "")
    finally:
        _config.PIPE.db_path = orig
    assert "/edit" in result, result
    assert "нужен текст правки" in result or "правка" in result, result
    print("  /edit parser + no-feedback hint: OK")


def test_edit_no_such_draft(db_path: Path):
    """/edit on a missing id returns 'не найден' without LLM call."""
    import telegram_receiver as feedback_receiver
    import config as _config
    orig = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        result = feedback_receiver._do_edit(88888, "сделай короче")
    finally:
        _config.PIPE.db_path = orig
    assert "не найден" in result, result
    print("  /edit missing-id path: OK")


def test_edit_terminal_status_blocked(db_path: Path):
    """/edit on a non-draft status (approved/published) refuses to overwrite."""
    import telegram_receiver as feedback_receiver
    import config as _config
    conn = _connect(db_path)
    pid = _insert_draft_post(conn, "Already approved", "already-approved",
                             wp_post_id=None, status="approved")
    conn.close()
    orig = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        result = feedback_receiver._do_edit(pid, "сделай короче")
    finally:
        _config.PIPE.db_path = orig
    assert "только для draft" in result or "approved" in result, result
    print("  /edit terminal-status block: OK")


# --------------------------------------------------------------------------- #
# Helpers (/help, /is_dd, bad-command hint)
# --------------------------------------------------------------------------- #

def test_is_dd_username_filter():
    """Only the configured DD username may trigger handlers."""
    import telegram_receiver as feedback_receiver
    update = {
        "message": {
            "from": {"username": "your_tg_username", "id": 1},
            "text": "/approve 1",
        }
    }
    assert feedback_receiver._is_dd(update), "DD username should match (case-insensitive)"
    update["message"]["from"]["username"] = "stranger"
    assert not feedback_receiver._is_dd(update), "stranger must not match"
    update["message"]["from"].pop("username")
    assert not feedback_receiver._is_dd(update), "no username → reject"
    print("  DD username filter: OK")


def test_help_lists_new_commands(db_path: Path):
    """/help enumerates the new command set."""
    import telegram_receiver as feedback_receiver
    import config as _config
    orig = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        result = feedback_receiver._do_help()
    finally:
        _config.PIPE.db_path = orig

    def _assert(cond: bool, msg: str) -> None:
        assert cond, msg

    assert "/approve" in result, result
    assert "/feedback" in result, result
    assert "/feedback_tg" in result, result
    assert "/edit" in result, result
    assert "/help" in result, result
    # DD 2026-07-21 10:48 MSK: /reject and /reject_tg restored as live
    # moderation commands in addition to /approve (state-machine moderation
    # works in auto-publish mode too).
    assert "/reject" in result, result
    assert "/reject_tg" in result, result
    # Removed commands must NOT appear as live commands. They may appear
    # inside the trailing Sprint-cleanup note; split off that note so the
    # assertion only checks the live command list.
    live_block = result.split("<i>Sprint cleanup")[0]
    for banned in ("/ideas", "/status", "/up ", "/down "):
        _assert(banned not in live_block, f"/help live block must not mention {banned!r}")
    print("  /help new commands: OK")


def test_bad_command_hint_in_noop():
    """/foo returns the re-prompt hint instead of 'принял' (DD contract)."""
    import telegram_receiver as feedback_receiver
    update = {"message": {"text": "/foo"}}
    result = feedback_receiver._handle(update)
    assert result is not None
    assert "/approve" in result and "/feedback" in result, result
    assert "/reject" not in result, result  # /reject must NOT appear in hint
    assert "/edit" in result, result
    plain = feedback_receiver._handle({"message": {"text": "привет"}})
    assert plain is None, plain
    print("  bad-command hint + plain chat ignore: OK")


# --------------------------------------------------------------------------- #
# Morning report — feedback section is gone
# --------------------------------------------------------------------------- #

def test_morning_report_no_feedback_section(db_path: Path):
    """Sprint cleanup 2026-07-21: aggregate_feedback + _fmt_feedback are gone."""
    from morning_report import build_report
    conn = _connect(db_path)
    report = build_report(conn, since_h=24)
    conn.close()
    assert "Feedback" not in report or "💬" not in report, (
        f"feedback section must not appear; got:\n{report}"
    )
    print("  morning_report has no Feedback section: OK")


# --------------------------------------------------------------------------- #
# Sprint 5.5e: _normalize_tg_command() + group-context slash forms
# (Sprint 5.5e was added before the cleanup; existing invariants still hold).
# --------------------------------------------------------------------------- #

def test_normalize_strips_inline_botname():
    """'/preview@your_bot 174' → '/preview 174'."""
    from telegram_receiver import _normalize_tg_command
    assert _normalize_tg_command("/preview@your_bot 174") == "/preview 174"
    assert _normalize_tg_command("/approve@your_bot 169") == "/approve 169"
    assert _normalize_tg_command("/feedback@your_bot 17 привет") == "/feedback 17 привет"


def test_normalize_strips_trailing_botname():
    """'/preview 174 @your_bot' → '/preview 174'."""
    from telegram_receiver import _normalize_tg_command
    assert _normalize_tg_command("/preview 174 @your_bot") == "/preview 174"
    assert _normalize_tg_command("/approve 169 @your_bot") == "/approve 169"


def test_normalize_case_insensitive():
    """@BOTNAME in any case is recognized (TG client can vary)."""
    from telegram_receiver import _normalize_tg_command
    assert _normalize_tg_command("/preview@Your_Bot 174") == "/preview 174"
    assert _normalize_tg_command("/preview@YOUR_BOT 174") == "/preview 174"


def test_normalize_idempotent_and_passthrough():
    """/preview 174 is unchanged; empty/non-slash left untouched."""
    from telegram_receiver import _normalize_tg_command
    assert _normalize_tg_command("/preview 174") == "/preview 174"
    once = _normalize_tg_command("/preview@your_bot 174")
    twice = _normalize_tg_command(once)
    assert once == twice == "/preview 174"


def test_normalize_safety_mid_sentence():
    """'бла-бла /preview 175' must NOT parse as a command (DD safety)."""
    from telegram_receiver import _normalize_tg_command, parse_command
    assert _normalize_tg_command("бла-бла /preview 175") == "бла-бла /preview 175"
    assert _normalize_tg_command("привет, ты видел /approve 175?") == "привет, ты видел /approve 175?"
    assert parse_command("бла-бла /approve 175")[0] == "noop"
    assert parse_command("привет, ты видел /approve 175?")[0] == "noop"


def test_parse_approve_id():
    """Sprint 5.5e: /approve id; /approve id <note> (note no longer parsed)."""
    from telegram_receiver import parse_command
    cmd, kwargs = parse_command("/approve 169")
    assert cmd == "approve"
    assert kwargs == {"draft_post_id": 169}, kwargs

    # Even with a trailing "note" word it just parses the id (note arg removed).
    cmd, kwargs = parse_command("/approve 169 норм пост")
    assert cmd == "approve"
    assert kwargs["draft_post_id"] == 169, kwargs

    cmd, kwargs = parse_command("/approve@your_bot 169")
    assert cmd == "approve" and kwargs["draft_post_id"] == 169, kwargs
    cmd, kwargs = parse_command("/approve 169 @your_bot")
    assert cmd == "approve" and kwargs["draft_post_id"] == 169, kwargs


def test_parse_feedback_id_and_note():
    """/feedback id <note> — note is REQUIRED post-cleanup."""
    from telegram_receiver import parse_command
    cmd, kwargs = parse_command("/feedback 17 отлично написано")
    assert cmd == "feedback"
    assert kwargs["wp_post_id"] == 17
    assert kwargs["note"] == "отлично написано", kwargs


def test_reject_dropped_to_noop():
    """Legacy: kept as a no-op-name placeholder; see /reject tests below.

    DD 2026-07-21 10:48 MSK restored /reject and /reject_tg. This
    test was renamed from test_reject_dropped_to_noop to
    test_legacy_down_still_noop to match the actual contract: only
    /down (legacy from Sprint 6.6) stays dropped.
    """
    from telegram_receiver import parse_command
    # Only /down is still noop; /reject and /reject_tg are live again.
    cmd, _ = parse_command("/down 7")
    assert cmd == "noop", cmd
    print("  /down still noop (legacy): OK")


def test_reject_parser_routes_to_reject(db_path: Path):
    """/reject <id> [note] parses to cmd='reject' with draft_post_id + optional note."""
    from telegram_receiver import parse_command
    for txt, exp_id, exp_note in (
        ("/reject 7", 7, None),
        ("/reject 42 спам", 42, "спам"),
        ("/reject  9   какая-то причина", 9, "какая-то причина"),
    ):
        cmd, kw = parse_command(txt)
        assert cmd == "reject", (txt, cmd)
        assert kw["draft_post_id"] == exp_id, (txt, kw)
        assert kw["note"] == exp_note, (txt, kw)
    print("  /reject parser → reject: OK")


def test_reject_tg_parser_routes_to_reject_tg(db_path: Path):
    """/reject_tg <id> [note] parses to cmd='reject_tg'."""
    from telegram_receiver import parse_command
    cmd, kw = parse_command("/reject_tg 5 не для TG")
    assert cmd == "reject_tg", cmd
    assert kw["draft_post_id"] == 5, kw
    assert kw["note"] == "не для TG", kw
    print("  /reject_tg parser → reject_tg: OK")


def test_reject_marks_draft_as_rejected(db_path: Path):
    """/reject transitions draft_posts.status → 'rejected' regardless of prev status."""
    import telegram_receiver as feedback_receiver
    import config as _config
    conn = _connect(db_path)
    post_id = _insert_draft_post(conn, "Reject draft", "reject-draft", status="draft")
    conn.close()

    orig_path = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        result = feedback_receiver._do_reject(post_id, note="низкое качество")
    finally:
        _config.PIPE.db_path = orig_path
    assert "rejected" in result.lower() or "отклонён" in result.lower(), result
    assert "низкое качество" in result, result

    conn = _connect(db_path)
    row = conn.execute(
        "SELECT status, review_note FROM draft_posts WHERE id=?", (post_id,)
    ).fetchone()
    assert row["status"] == "rejected", row["status"]
    assert row["review_note"] == "низкое качество", row["review_note"]
    conn.close()
    print("  /reject draft → rejected with note: OK")


def test_reject_works_on_published_post(db_path: Path):
    """Auto-publish mode (WP_PUBLISH_AUTO_APPROVE=1) puts posts in 'published'
    state. /reject must still be able to flag them as 'rejected' for
    future analytics — the WP URL stays live, only our DB state changes."""
    import telegram_receiver as feedback_receiver
    import config as _config

    conn = _connect(db_path)
    post_id = _insert_draft_post(conn, "Auto-published", "auto-pub",
                                  wp_post_id=2346, status="published")
    conn.close()

    orig_path = _config.PIPE.db_path
    orig_flag = _config.PIPE_TICKS.wp_publish_auto_approve
    _config.PIPE.db_path = db_path
    _config.PIPE_TICKS.wp_publish_auto_approve = True
    try:
        result = feedback_receiver._do_reject(post_id, note="after auto-publish")
    finally:
        _config.PIPE.db_path = orig_path
        _config.PIPE_TICKS.wp_publish_auto_approve = orig_flag
    assert "rejected" in result.lower(), result
    assert "2346" not in result, "should not mention wp_post_id in moderation reply"

    conn = _connect(db_path)
    status = conn.execute(
        "SELECT status FROM draft_posts WHERE id=?", (post_id,)
    ).fetchone()[0]
    assert status == "rejected", status
    conn.close()
    print("  /reject on published post (auto-publish mode): OK")


def test_reject_tg_marks_dispatch_as_rejected_tg(db_path: Path):
    """/reject_tg transitions latest tg_dispatch row → 'rejected_tg' + feedback_note."""
    import telegram_receiver as feedback_receiver
    import config as _config
    # Ensure tg_dispatch schema exists (isolation DB from _smoke_lib
    # already runs the migrations, but be defensive for hand-crafted DBs).
    conn = _connect(db_path)
    has_td = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tg_dispatch'"
    ).fetchone()
    if has_td is None:
        import init_db
        from migrate import run_migrations as _run_migs
        init_db.init_db(db_path)
        with sqlite3.connect(str(db_path)) as c:
            _run_migs(c)
    conn.close()

    conn = _connect(db_path)
    post_id = _insert_draft_post(conn, "TG-reject", "tg-reject", wp_post_id=99,
                                  status="published")
    # Seed a tg_dispatch row awaiting_approval.
    conn.execute(
        """INSERT INTO tg_dispatch
              (post_id, status, tg_title, tg_teaser, tg_hashtags_json,
               prompt_version, generated_at)
           VALUES (?, 'awaiting_approval', 'Test title', 'Test teaser.', '["ai"]',
                   'master_prompt_tg.md@v1.0', datetime('now'))""",
        (post_id,),
    )
    conn.commit()
    conn.close()

    orig_path = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        result = feedback_receiver._do_reject_tg(post_id, note="плохой заголовок")
    finally:
        _config.PIPE.db_path = orig_path
    assert "rejected" in result.lower() or "отклонен" in result.lower() or "отклонён" in result.lower(), result
    assert "плохой заголовок" in result, result

    conn = _connect(db_path)
    row = conn.execute(
        "SELECT status, feedback_note, failed_reason FROM tg_dispatch "
        "WHERE post_id=? ORDER BY id DESC LIMIT 1",
        (post_id,),
    ).fetchone()
    assert row["status"] == "rejected_tg", row["status"]
    assert row["feedback_note"] == "плохой заголовок", row["feedback_note"]
    assert "плохой заголовок" in (row["failed_reason"] or ""), row["failed_reason"]
    conn.close()
    print("  /reject_tg awaiting_approval → rejected_tg with note: OK")


def test_reject_tg_on_published_tg_is_informational(db_path: Path):
    """/reject_tg on an already-published TG post is informational only."""
    import telegram_receiver as feedback_receiver
    import config as _config
    conn = _connect(db_path)
    has_td = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tg_dispatch'"
    ).fetchone()
    if has_td is None:
        import init_db
        from migrate import run_migrations as _run_migs
        init_db.init_db(db_path)
        with sqlite3.connect(str(db_path)) as c:
            _run_migs(c)
    conn.close()

    conn = _connect(db_path)
    post_id = _insert_draft_post(conn, "Already published", "already-pub",
                                  wp_post_id=100, status="published")
    conn.execute(
        """INSERT INTO tg_dispatch
              (post_id, status, tg_title, tg_teaser, tg_hashtags_json,
               prompt_version, generated_at, tg_message_id, tg_message_url)
           VALUES (?, 'published_tg', 'X', 'Y.', '[]', 'v1.0', datetime('now'),
                   42, 'https://t.me/your_channel/42')""",
        (post_id,),
    )
    conn.commit()
    conn.close()

    orig_path = _config.PIPE.db_path
    _config.PIPE.db_path = db_path
    try:
        result = feedback_receiver._do_reject_tg(post_id, note="постфактум")
    finally:
        _config.PIPE.db_path = orig_path
    assert "уже опубликовано" in result, result
    # Verify status is still published_tg (not changed).
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT status FROM tg_dispatch WHERE post_id=? ORDER BY id DESC LIMIT 1",
        (post_id,),
    ).fetchone()
    assert row["status"] == "published_tg", row["status"]
    conn.close()
    print("  /reject_tg on published_tg → informational, no mutation: OK")


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #

def main():
    _force_dry_mode()
    db_path, _conn = _smoke_lib.make_isolated_db(label="fb_receiver_smoke")
    # Apply migration 021 on top of the isolated DB.
    conn = _connect(db_path)
    conn.close()
    with tempfile.TemporaryDirectory():
        print("== Sprint cleanup 2026-07-21 feedback_receiver smoke ==")
        # Parsers
        test_parse_commands()
        # /approve
        test_do_approve_via_parser(db_path)
        test_record_review_not_found(db_path)
        test_do_approve_noop_when_review_disabled(db_path)
        # /feedback (WP)
        test_do_feedback_attached(db_path)
        test_do_feedback_requires_note(db_path)
        test_do_feedback_unknown_wp_post_id(db_path)
        # /feedback_tg
        test_do_feedback_tg_attached(db_path)
        test_do_feedback_tg_requires_note(db_path)
        test_do_feedback_tg_no_dispatch_row(db_path)
        # /edit
        test_edit_parser_no_feedback_hint(db_path)
        test_edit_no_such_draft(db_path)
        test_edit_terminal_status_blocked(db_path)
        test_edit_overwrites_content_via_record_edit(db_path)
        # helpers
        test_is_dd_username_filter()
        test_help_lists_new_commands(db_path)
        test_bad_command_hint_in_noop()
        # morning report
        test_morning_report_no_feedback_section(db_path)
        # Sprint 5.5e normalization (still valid)
        test_normalize_strips_inline_botname()
        test_normalize_strips_trailing_botname()
        test_normalize_case_insensitive()
        test_normalize_idempotent_and_passthrough()
        test_normalize_safety_mid_sentence()
        test_parse_approve_id()
        test_parse_feedback_id_and_note()
        # /reject + /reject_tg (restored DD 2026-07-21 10:48 MSK)
        test_reject_dropped_to_noop()
        test_reject_parser_routes_to_reject(db_path)
        test_reject_tg_parser_routes_to_reject_tg(db_path)
        test_reject_marks_draft_as_rejected(db_path)
        test_reject_works_on_published_post(db_path)
        test_reject_tg_marks_dispatch_as_rejected_tg(db_path)
        test_reject_tg_on_published_tg_is_informational(db_path)
    print("OK")


if __name__ == "__main__":
    sys.exit(main() or 0)
