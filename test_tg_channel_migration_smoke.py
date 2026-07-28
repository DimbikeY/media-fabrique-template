"""Sprint 6 smoke: migration 018_tg_channel_drafts.

Verifies:
  - Migration is idempotent (running twice = no errors, no duplicate rows)
  - draft_posts has the 3 new TG-channel columns
  - tg_dispatch table exists with the expected columns
  - Indexes idx_tg_drafts_post and idx_tg_drafts_post_created exist
  - FK from tg_dispatch.post_id → draft_posts.id works (CASCADE on delete)
  - After insert + select roundtrip, columns behave as expected (NULL by default)

Uses _smoke_lib.make_isolated_db() so it never touches the prod DB.
"""
from __future__ import annotations

import sqlite3
import sys

from _smoke_lib import make_isolated_db


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {r[0] for r in rows}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_draft_posts_has_tg_channel_columns() -> None:
    """After migration 018, draft_posts has 3 new columns for TG-channel state."""
    db_path, conn = make_isolated_db(label="tg_migration_cols")
    try:
        cols = _column_names(conn, "draft_posts")
        for expected in (
            "tg_channel_published_at",
            "tg_channel_message_id",
            "tg_channel_message_url",
        ):
            _assert(
                expected in cols,
                f"draft_posts missing column {expected!r}; have: {sorted(cols)}",
            )
        print(f"  PASS  draft_posts has all 3 TG-channel columns")
    finally:
        conn.close()


def test_tg_drafts_table_exists() -> None:
    """tg_dispatch table is created with the expected columns."""
    db_path, conn = make_isolated_db(label="tg_migration_table")
    try:
        tables = _tables(conn)
        _assert("tg_dispatch" in tables, f"tg_dispatch table missing; have: {sorted(tables)}")

        cols = _column_names(conn, "tg_dispatch")
        for expected in (
            "id", "post_id", "tg_title", "tg_teaser",
            "tg_hashtags_json", "prompt_version", "note", "created_at",
        ):
            _assert(
                expected in cols,
                f"tg_dispatch missing column {expected!r}; have: {sorted(cols)}",
            )
        print(f"  PASS  tg_dispatch table exists with all expected columns")
    finally:
        conn.close()


def test_tg_drafts_indexes_exist() -> None:
    """Both indexes are created (used for /edit_tg history + latest lookup)."""
    db_path, conn = make_isolated_db(label="tg_migration_idx")
    try:
        idx = _index_names(conn)
        _assert("idx_tg_drafts_post" in idx, f"idx_tg_drafts_post missing")
        _assert(
            "idx_tg_drafts_post_created" in idx,
            f"idx_tg_drafts_post_created missing",
        )
        print(f"  PASS  tg_dispatch indexes exist")
    finally:
        conn.close()


def test_migration_is_idempotent() -> None:
    """Running migrate.py twice doesn't fail and doesn't double-apply.

    run_migrations() records applied ids in _migrations; second run should
    skip them silently. We simulate by directly re-running run_migrations.
    """
    from migrate import run_migrations

    db_path, conn = make_isolated_db(label="tg_migration_idem")
    try:
        # First run already happened in make_isolated_db. Re-run.
        run_migrations(conn)

        # _migrations table has 018 exactly once.
        rows = conn.execute(
            "SELECT COUNT(*) FROM _migrations WHERE id='018_tg_channel_drafts'"
        ).fetchone()
        _assert(
            rows[0] == 1,
            f"migration 018 should be recorded exactly once, got {rows[0]}",
        )

        # Columns still present (no duplicate-column errors would crash here).
        cols = _column_names(conn, "draft_posts")
        _assert("tg_channel_published_at" in cols, "column dropped after re-migrate")
        print(f"  PASS  migration 018 idempotent (re-run is no-op)")
    finally:
        conn.close()


def test_tg_draft_roundtrip_with_fk_cascade() -> None:
    """Insert candidate → draft_post → tg_draft, verify roundtrip.

    Then delete the draft_post → tg_draft should cascade-delete (FK ON DELETE CASCADE).
    Also confirms default columns (NULLs, default created_at, prompt_version NOT NULL).
    """
    db_path, conn = make_isolated_db(label="tg_migration_rt")
    # SQLite default is FK OFF per connection; turn it on to actually test CASCADE.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # Need a candidate first (FK from draft_posts).
        source_id = conn.execute(
            "SELECT id FROM sources LIMIT 1"
        ).fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO candidates (source_id, guid, url, title)
            VALUES (?, ?, ?, ?)
            """,
            (source_id, "tg-rt-1", "https://example.com/tg-rt-1", "TG RT Test"),
        )
        candidate_id = cur.lastrowid

        cur = conn.execute(
            """
            INSERT INTO draft_posts (candidate_id, title, slug, status)
            VALUES (?, ?, ?, 'published')
            """,
            (candidate_id, "Test post", "test-post"),
        )
        post_id = cur.lastrowid

        # Default columns are NULL.
        row = conn.execute(
            "SELECT tg_channel_published_at, tg_channel_message_id, "
            "tg_channel_message_url FROM draft_posts WHERE id=?",
            (post_id,),
        ).fetchone()
        _assert(row[0] is None, f"tg_channel_published_at should default to NULL")
        _assert(row[1] is None, f"tg_channel_message_id should default to NULL")
        _assert(row[2] is None, f"tg_channel_message_url should default to NULL")

        # Insert tg_draft. prompt_version is NOT NULL.
        cur = conn.execute(
            """
            INSERT INTO tg_dispatch (
                post_id, tg_title, tg_teaser, tg_hashtags_json,
                prompt_version, note
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                "🔴 Test title",
                "Test teaser for round-trip.",
                '["ai", "test"]',
                "master_prompt_tg.md@v1.0",
                "first draft",
            ),
        )
        draft_id = cur.lastrowid

        # Roundtrip read.
        row = conn.execute(
            "SELECT tg_title, tg_teaser, prompt_version, note "
            "FROM tg_dispatch WHERE id=?",
            (draft_id,),
        ).fetchone()
        _assert(row[0] == "🔴 Test title", f"tg_title mismatch: {row[0]!r}")
        _assert(row[1] == "Test teaser for round-trip.", f"tg_teaser mismatch")
        _assert(row[2] == "master_prompt_tg.md@v1.0", f"prompt_version mismatch")
        _assert(row[3] == "first draft", f"note mismatch")
        _assert(
            conn.execute(
                "SELECT created_at FROM tg_dispatch WHERE id=?", (draft_id,)
            ).fetchone()[0] is not None,
            "created_at should default to datetime('now'), not NULL",
        )

        # FK CASCADE: delete the draft_post → tg_draft should disappear.
        conn.execute("DELETE FROM draft_posts WHERE id=?", (post_id,))
        rows = conn.execute(
            "SELECT COUNT(*) FROM tg_dispatch WHERE id=?", (draft_id,)
        ).fetchone()[0]
        _assert(
            rows == 0,
            f"FK CASCADE failed: tg_draft should be deleted with draft_post, got {rows}",
        )

        print(f"  PASS  tg_dispatch roundtrip + FK CASCADE works")
    finally:
        conn.close()


def test_index_supports_latest_tg_draft_lookup() -> None:
    """The composite index on (post_id, created_at DESC) is the right shape
    for the 'latest draft per post' query that /approve_tg will run.

    SQLite's planner may pick seq scan over the index on tiny tables, so we
    verify (a) the query returns the right row (logic) and (b) the index
    definition matches what the query needs (structural). Both together are
    enough to know /approve_tg's query pattern is supported at scale.
    """
    db_path, conn = make_isolated_db(label="tg_migration_query")
    try:
        source_id = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO candidates (source_id, guid, url, title) "
            "VALUES (?, ?, ?, ?)",
            (source_id, "tg-q-1", "https://example.com/q1", "Q1"),
        )
        candidate_id = conn.execute(
            "SELECT id FROM candidates WHERE guid='tg-q-1'"
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO draft_posts (candidate_id, title, slug, status) "
            "VALUES (?, ?, ?, 'published')",
            (candidate_id, "Q post", "q-post"),
        )
        post_id = cur.lastrowid

        # Insert 3 drafts; latest by created_at DESC should be 'v3'.
        for note in ["v1", "v2", "v3"]:
            conn.execute(
                "INSERT INTO tg_dispatch (post_id, tg_title, tg_teaser, "
                "prompt_version, note) VALUES (?, ?, ?, ?, ?)",
                (post_id, f"title-{note}", f"teaser-{note}",
                 "master_prompt_tg.md@v1.0", note),
            )

        latest = conn.execute(
            "SELECT note FROM tg_dispatch WHERE post_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (post_id,),
        ).fetchone()[0]
        _assert(
            latest == "v3",
            f"latest draft should be 'v3' (most recent), got {latest!r}",
        )

        # Index definition check: the composite index must be on
        # (post_id, created_at DESC) — that's what the query needs.
        idx_rows = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_tg_drafts_post_created'"
        ).fetchone()
        _assert(idx_rows is not None, "idx_tg_drafts_post_created not registered")
        sql = idx_rows[0].upper()
        _assert("POST_ID" in sql, f"index sql missing post_id: {sql}")
        _assert("CREATED_AT" in sql, f"index sql missing created_at: {sql}")
        _assert("DESC" in sql, f"index sql missing DESC: {sql}")
        print(f"  PASS  latest-draft query + composite index structure correct")
    finally:
        conn.close()


def main() -> int:
    tests = [
        test_draft_posts_has_tg_channel_columns,
        test_tg_drafts_table_exists,
        test_tg_drafts_indexes_exist,
        test_migration_is_idempotent,
        test_tg_draft_roundtrip_with_fk_cascade,
        test_index_supports_latest_tg_draft_lookup,
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