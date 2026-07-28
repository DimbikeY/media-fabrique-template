"""Tiny migrations runner.

Add idempotent ALTERs here as the schema evolves. Each migration runs
exactly once (tracked in the `_migrations` table).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from config import PIPE
from init_db import init_db

MIGRATIONS: list[tuple[str, str]] = [
    (
        "001_add_item_image_and_safety",
        """
        ALTER TABLE candidates ADD COLUMN image_url TEXT;
        ALTER TABLE candidates ADD COLUMN video_embed_url TEXT;
        ALTER TABLE candidates ADD COLUMN safety_status TEXT NOT NULL DEFAULT 'review';
        """,
    ),
    (
        "002_add_item_lang",
        """
        ALTER TABLE candidates ADD COLUMN lang TEXT;
        """,
    ),
    (
        "003_add_source_lang",
        """
        ALTER TABLE sources ADD COLUMN lang TEXT;
        """,
    ),
    (
        "004_add_post_image_prompt",
        """
        ALTER TABLE draft_posts ADD COLUMN image_prompt TEXT;
        """,
    ),
    (
        "005_create_llm_runs",
        """
        CREATE TABLE IF NOT EXISTS llm_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id         INTEGER NOT NULL,
            model           TEXT,
            started_at      TEXT NOT NULL DEFAULT (datetime('now')),
            duration_ms     INTEGER,
            prompt_tokens   INTEGER,
            completion_tokens INTEGER,
            thinking_tokens INTEGER,
            response_chars  INTEGER,
            status          TEXT,   -- ok | blocked | failed
            stage           TEXT,   -- ingest | llm_parse | llm_validate | llm_request | llm_auth | llm_quota | db_write | (null on ok/blocked)
            FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_llm_runs_item ON llm_runs(candidate_id);
        CREATE INDEX IF NOT EXISTS idx_llm_runs_started ON llm_runs(started_at);
        """,
    ),
    (
        "006_rebuild_llm_runs_schema",
        """
        -- Drop and recreate llm_runs with the up-to-date schema.
        -- Safe: this table is metrics-only and gets fresh rows every run.
        -- Idempotent: if init_db already created the table with the
        -- post-migration-008 column names (_provider suffixes), the
        -- CREATE TABLE would fail on duplicate column names and we
        -- catch that as \"already applied\" below.
        DROP TABLE IF EXISTS llm_runs;
        CREATE TABLE llm_runs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id           INTEGER NOT NULL,
            model             TEXT,
            started_at        TEXT NOT NULL DEFAULT (datetime('now')),
            duration_ms       INTEGER,
            prompt_tokens     INTEGER,
            completion_tokens INTEGER,
            thinking_tokens   INTEGER,
            response_chars    INTEGER,
            reasoning_chars   INTEGER,
            status            TEXT,   -- ok | blocked | failed
            stage             TEXT,   -- ingest | llm_parse | llm_validate | llm_request | llm_auth | llm_quota | db_write | (null on ok/blocked)
            FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
        );
        CREATE INDEX idx_llm_runs_item    ON llm_runs(candidate_id);
        CREATE INDEX idx_llm_runs_started ON llm_runs(started_at);
        """,
    ),
    (
        "007_add_reasoning_chars",
        """
        ALTER TABLE llm_runs ADD COLUMN reasoning_chars INTEGER;
        """,
    ),
    (
        "008_rename_metrics_with_provenance",
        """
        -- Rename legacy columns to make provider vs local provenance explicit.
        -- Each RENAME is wrapped in a sub-string so a \"duplicate column\"
        -- error is caught as \"already renamed\" → migration is idempotent.
        ALTER TABLE llm_runs RENAME COLUMN prompt_tokens TO prompt_tokens_provider;
        """,
    ),
    (
        "008b_rename_completion_tokens",
        """
        ALTER TABLE llm_runs RENAME COLUMN completion_tokens TO completion_tokens_provider;
        """,
    ),
    (
        "008c_rename_thinking_tokens",
        """
        ALTER TABLE llm_runs RENAME COLUMN thinking_tokens TO thinking_tokens_provider;
        """,
    ),
    (
        "008d_rename_response_chars",
        """
        ALTER TABLE llm_runs RENAME COLUMN response_chars TO response_chars_local;
        """,
    ),
    (
        "008e_rename_reasoning_chars",
        """
        ALTER TABLE llm_runs RENAME COLUMN reasoning_chars TO reasoning_chars_local;
        """,
    ),
    (
        "008f_add_prompt_chars_local",
        """
        ALTER TABLE llm_runs ADD COLUMN prompt_chars_local INTEGER;
        """,
    ),
    (
        "009_post_status_publishing",
        """
        -- No schema change: draft_posts.status now also accepts 'publishing'
        -- (atomic claim pattern in publisher.py). SQLite has no CHECK on
        -- the column, so this migration is purely documentary. It pins
        -- the convention so future readers know the full state machine:
        --   draft -> publishing -> published | failed
        -- Nothing to ALTER here. This migration is a no-op.
        """,
    ),
    (
        "010_add_scoring_columns",
        """
        -- Sprint 5.1: priority-driven queue + half-life aging.
        -- All six columns are nullable for backwards compat with rows
        -- that pre-date the migration. We treat NULL as "not scored yet".
        ALTER TABLE candidates ADD COLUMN category     TEXT;          -- LLM-emitted category label
        ALTER TABLE candidates ADD COLUMN base_score   REAL;          -- 0..10, frozen at first scoring
        ALTER TABLE candidates ADD COLUMN half_life_h  REAL;          -- hours from CATEGORY_HALF_LIFE_H
        ALTER TABLE candidates ADD COLUMN weight       REAL;          -- base_score * decay(age)
        ALTER TABLE candidates ADD COLUMN expires_at   TEXT;          -- janitor reads this
        ALTER TABLE candidates ADD COLUMN scored_at    TEXT;          -- when base_score was set
        -- Sorting index: rewrite queue picks top by weight first, falls
        -- back to published_at for the still-NULL tail.
        CREATE INDEX IF NOT EXISTS idx_items_weight     ON candidates(weight DESC);
        CREATE INDEX IF NOT EXISTS idx_items_expires_at ON candidates(expires_at);
        """,
    ),
    (
        "011_add_feedback_columns",
        """
        -- Sprint 6.5: DD's verdict on each item.
        -- Stored as LAST (not log) because the feedback cycle is slow
        -- and last value covers all the use cases the feedback loop
        -- needs. Behaviour note: feedback never mutates runtime — it
        -- feeds into the next sprint's master_prompt / scoring review.
        ALTER TABLE candidates ADD COLUMN feedback_verdict TEXT;       -- 'up' | 'down' | NULL
        ALTER TABLE candidates ADD COLUMN feedback_note    TEXT;       -- freeform: style, length, jargon, stop-topic
        ALTER TABLE candidates ADD COLUMN feedback_at      TEXT;       -- ISO timestamp of last verdict
        -- Speed up the morning-report query that surfaces feedback
        -- candidates and the in-app "what did DD say about this?" lookups.
        CREATE INDEX IF NOT EXISTS idx_items_feedback_at ON candidates(feedback_at);
        """,
    ),
    (
        "013_add_source_homepage_url",
        """
        -- Sprint 5.5 (B3): fallback URL for the «Источник:» link in published
        -- draft_posts. If the item URL is missing or coincides with the RSS feed URL
        -- we point users at the source's landing page instead of a raw XML feed.
        ALTER TABLE sources ADD COLUMN homepage_url TEXT;
        """,
    ),
    (
        # Sprint 6m.1: semantic rename. candidates → candidates, draft_posts → draft_posts,
        # candidate_id → candidate_id (in draft_posts and llm_runs).
        # SQLite supports ALTER TABLE RENAME TO and RENAME COLUMN, which
        # automatically updates foreign-key references to the renamed columns.
        "014_rename_items_and_posts",
        """
        ALTER TABLE candidates    RENAME TO candidates;
        ALTER TABLE draft_posts    RENAME TO draft_posts;
        ALTER TABLE draft_posts  RENAME COLUMN candidate_id TO candidate_id;
        ALTER TABLE llm_runs     RENAME COLUMN candidate_id TO candidate_id;
        -- After table renames, SQLite keeps the FK clause but rewrites the
        -- referenced target table name. So:
        --   draft_posts.candidate_id REFERENCES candidates(id)
        --   llm_runs.candidate_id   REFERENCES candidates(id)
        -- All ON DELETE CASCADE options survive the rename.
        """,
    ),
    (
        # Sprint 6.6 — approve/reject flow + retraining-signal storage.
        # After this migration:
        #   * draft_posts.status accepts 'approved' | 'rejected' in
        #     addition to 'draft' | 'publishing' | 'published' | 'failed'.
        #     (State machine becomes draft → approved → publishing →
        #     published | failed; or draft → rejected at any time.)
        #   * retraining signals (DD's notes about style, length, etc.)
        #     live in feedback_signals, attached to the specific draft
        #     they apply to. The old candidates.feedback_* columns from
        #     migration 011 are dropped: candidates is raw ingest, the
        #     signal belongs to the artifact (the draft) not the source.
        "015_approve_reject_and_feedback_signals",
        """
        -- 1) draft_posts review columns. last-write-wins for review_note
        --    (DD is unlikely to send two notes in a row on the same draft;
        --    if it happens the append_idea_bullet-style history lives in
        --    feedback_signals).
        ALTER TABLE draft_posts ADD COLUMN reviewed_at TEXT;
        ALTER TABLE draft_posts ADD COLUMN reviewed_by TEXT;
        ALTER TABLE draft_posts ADD COLUMN review_note TEXT;

        -- 2) feedback_signals — log of free-form notes attached to a
        --    draft_post. Multiple signals per draft are allowed (DD may
        --    refine feedback over the lifetime of a draft).
        CREATE TABLE IF NOT EXISTS feedback_signals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_post_id  INTEGER NOT NULL,
            kind         TEXT NOT NULL,            -- 'approved' | 'rejected' | 'idea' | 'post_feedback'
            note         TEXT,                     -- free-form, nullable
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (draft_post_id) REFERENCES draft_posts(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_signals_draft ON feedback_signals(draft_post_id);
        CREATE INDEX IF NOT EXISTS idx_feedback_signals_created ON feedback_signals(created_at);

        -- 3) Drop the historical candidates.feedback_* columns. They
        --    were never wired into runtime behaviour (see Sprint 6.5.1
        --    KNOWN ISSUE privacy), and the per-draft feedback_signals
        --    table above is the right place going forward. SQLite ≥3.35
        --    supports ALTER TABLE DROP COLUMN.
        ALTER TABLE candidates DROP COLUMN feedback_verdict;
        ALTER TABLE candidates DROP COLUMN feedback_note;
        ALTER TABLE candidates DROP COLUMN feedback_at;
        """,
    ),

    # ------------------------------------------------------------ 016 ----
    # Sprint 6m.2: feedback_signals.processed_at + batch digest support.
    #
    # The digest tick (feedback_digest) aggregates notes that DD left via
    # /approve, /reject, /edit, /feedback and rewrites prompts.py /
    # scoring parameters when patterns are detected. ``processed_at`` is
    # the digest watermark: rows where processed_at IS NULL are
    # unprocessed; the tick sets processed_at to the digest time.
    #
    # We also add an index to make the "find unprocessed signals" query
    # cheap as the table grows.
    (
        "016_feedback_signals_processed_at",
        """
        ALTER TABLE feedback_signals ADD COLUMN processed_at TEXT;
        CREATE INDEX IF NOT EXISTS idx_feedback_signals_unprocessed
            ON feedback_signals(processed_at) WHERE processed_at IS NULL;
        """,
    ),

    # ------------------------------------------------------------ 017 ----
    # Sprint 6.7: closed-set category whitelist.
    #
    # Why a backfill migration and not a CHECK constraint?
    #   SQLite cannot ALTER TABLE ... ADD CONSTRAINT — CHECK constraints
    #   are baked into the table definition at CREATE TABLE time. So we
    #   do the next-best thing: coerce every existing ``candidates.category``
    #   value that is NOT in the WHITELIST to ``'other'``. New DBs created
    #   by ``init_db.py`` get the CHECK constraint inline in SCHEMA;
    #   existing DBs are protected by this UPDATE plus the Pydantic /
    #   validate_category gate at the application boundary.
    #
    # This UPDATE is idempotent: re-running it matches zero rows because
    # every row is already either NULL or in the whitelist.
    (
        "017_backfill_category_whitelist",
        f"""
        UPDATE candidates
           SET category = 'other'
         WHERE category IS NOT NULL
           AND category NOT IN (
             'politics','sports','ai','entertainment','business','tech',
             'vibe-coding','health','other','culture','science','nature'
           );
        """,
    ),
    (
        # Sprint 6 (channel-prompt): TG-channel "<your_channel>" state.
        # Idempotent ALTERs (split into separate statements; see run_migrations
        # comment about SQLite internal caching). CREATE TABLE uses
        # IF NOT EXISTS so re-running is harmless.
        "018_tg_channel_drafts",
        """
        ALTER TABLE draft_posts ADD COLUMN tg_channel_published_at TEXT;
        ALTER TABLE draft_posts ADD COLUMN tg_channel_message_id INTEGER;
        ALTER TABLE draft_posts ADD COLUMN tg_channel_message_url TEXT;
        CREATE TABLE IF NOT EXISTS tg_drafts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id         INTEGER NOT NULL,
            tg_title        TEXT,
            tg_teaser       TEXT,
            tg_hashtags_json TEXT,
            prompt_version  TEXT NOT NULL,
            note            TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (post_id) REFERENCES draft_posts(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_tg_drafts_post ON tg_drafts(post_id);
        CREATE INDEX IF NOT EXISTS idx_tg_drafts_post_created ON tg_drafts(post_id, created_at DESC);
        """,
    ),
    (
        "019_draft_posts_telegra_url",
        """
        -- Sprint X hotfix (DD 2026-07-20 07:55 MSK): Telegraph IV URL was
        -- not persisted, only returned from tg_channel_publisher.publish()
        -- to the TG-channel message. As a result the admin #published
        -- topic preview (push_published) couldn't show the IV link, and
        -- DD asked for it to be visible. We store it next to the other
        -- tg_channel_* columns so push_published can render a single
        -- ⚡ Instant View line.
        ALTER TABLE draft_posts ADD COLUMN tg_channel_telegra_url TEXT;
        """,
    ),
    (
        "020_sprint_y_tg_dispatch_table",
        """
        -- Sprint Y (DD 2026-07-20 22:33 MSK): Split the Sprint X monolithic
        -- WP+TG publish tick into three independent ticks:
        --   tick=wp_publish     - only WP REST, never touches TG or Telegraph
        --   tick=generate_for_tg - only LLM regeneration (master_prompt_tg.md)
        --   tick=publish_tg     - only Telegraph IV + TG channel sendMessage
        -- The tg_drafts table becomes tg_dispatch and gains a status column
        -- that tracks each post through the pipeline independently of
        -- draft_posts.status (which only reflects WP state after this).
        CREATE TABLE tg_dispatch (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id         INTEGER NOT NULL,
            tg_title        TEXT,
            tg_teaser       TEXT,
            tg_hashtags_json TEXT,
            prompt_version  TEXT NOT NULL DEFAULT '',
            note            TEXT,
            status          TEXT NOT NULL DEFAULT 'pending_tg_text',
                -- pending_tg_text | text_generated | awaiting_approval |
                -- approved | rejected_tg | published_tg |
                -- telegram_blocked_exhausted | expired_skipped
            telegraph_url   TEXT,
            tg_message_id   INTEGER,
            tg_message_url  TEXT,
            failed_reason   TEXT,
            generated_at    TEXT,
            approved_at     TEXT,
            attempted_at    TEXT,
            attempts        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (post_id) REFERENCES draft_posts(id) ON DELETE CASCADE
        );
        CREATE INDEX idx_tg_dispatch_status_post   ON tg_dispatch(status, post_id);
        CREATE INDEX idx_tg_dispatch_post_created  ON tg_dispatch(post_id, created_at DESC);
        CREATE INDEX idx_tg_dispatch_approved_at   ON tg_dispatch(approved_at);
        """,
    ),
    (
        # Sprint cleanup 2026-07-21 (DD): выпиливаем feedback_signals и digest.
        # /feedback и /feedback_tg остаются, но пишут note в новые колонки
        # draft_posts.feedback_note + tg_dispatch.feedback_note — для будущего
        # ручного обдумывания. Никакого digest, никакого analysis.
        #
        # Drop order: indices → table. SQLite хранит индексы отдельно, но
        # DROP TABLE каскадно удалит индексы; явные DROP INDEX для
        # предсказуемости (на случай если DROP TABLE когда-то откатят).
        "021_drop_feedback_signals_add_feedback_note",
        """
        DROP INDEX IF EXISTS idx_feedback_signals_draft;
        DROP INDEX IF EXISTS idx_feedback_signals_created;
        DROP INDEX IF EXISTS idx_feedback_signals_unprocessed;
        DROP TABLE IF EXISTS feedback_signals;
        ALTER TABLE draft_posts ADD COLUMN feedback_note TEXT;
        ALTER TABLE tg_dispatch ADD COLUMN feedback_note TEXT;
        """,
    ),
]  


def run_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS _migrations (
              id TEXT PRIMARY KEY,
              applied_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )
    applied = {r["id"] for r in conn.execute("SELECT id FROM _migrations").fetchall()}
    for mid, sql in MIGRATIONS:
        if mid in applied:
            continue
        try:
            # executescript commits before running, then runs each statement
            # in its own implicit transaction. Multiple ALTER TABLE ... RENAME
            # statements in a single script can fail to see schema changes
            # from prior statements due to sqlite3 internal caching. Split
            # on ';' and run each statement individually so each DDL is
            # visible to the next. Skip empty / comment-only fragments.
            for raw_stmt in sql.split(";"):
                stmt = raw_stmt.strip()
                # Skip empty lines and comment-only lines. split(';') can
                # produce fragments like ' intentional no-op.' after a
                # '-- comment;' line — check lstrip for '--' so leading
                # whitespace doesn't hide the comment marker.
                stripped = stmt.strip()
                if not stripped:
                    continue
                # Drop trailing lines that are pure comments. split(';')
                # can leave a fragment like:
                #     '-- LLM-emitted category label\n        ALTER TABLE ... ADD COLUMN foo REAL'
                # whose first line starts with '--' but contains a real
                # SQL statement after it. Filter comment-only lines first,
                # then decide if anything is left.
                non_comment_lines = [
                    ln for ln in stripped.splitlines()
                    if not ln.lstrip().startswith("--")
                ]
                cleaned = "\n".join(non_comment_lines).strip()
                if not cleaned:
                    continue
                try:
                    conn.execute(cleaned)
                    conn.commit()
                except sqlite3.OperationalError as e:
                    # 'duplicate column' / 'already exists' / 'no such column'
                    # — treat as already applied so the migration marker is
                    # still recorded and we don't loop forever. The 'no such
                    # column' case fires when init_db created the table with
                    # the post-migration column names (e.g. _provider
                    # suffixes) and we tried to RENAME a column that already
                    # had the target name or never had the old name.
                    msg = str(e).lower()
                    if any(kw in msg for kw in (
                        "duplicate", "already exists", "already ",
                        "no such column", "no such table",
                    )):
                        continue
                    raise
            conn.execute("INSERT INTO _migrations (id) VALUES (?)", (mid,))
            conn.commit()
            print(f"[migrate] applied: {mid}")
        except sqlite3.OperationalError as e:
            # 'duplicate column' etc. - treat as already applied
            msg = str(e).lower()
            if any(kw in msg for kw in (
                "duplicate", "already exists", "already ",
                "no such column", "no such table",
            )):
                conn.execute("INSERT OR IGNORE INTO _migrations (id) VALUES (?)", (mid,))
            else:
                raise

    # ----- DATA FIXUPS -----
    # Each fixup is idempotent: re-running it on an already-fixed DB is a no-op.
    # Add new ones here as the schema evolves.
    _fixup_item_lang_from_sources(conn)
    _fixup_normalize_published_at(conn)
    _fixup_category_drift_log(conn)
    _fixup_sprint_y_backfill_tg_dispatch(conn)

    conn.commit()


# --- data fixups -------------------------------------------------------------
def _fixup_sprint_y_backfill_tg_dispatch(conn: sqlite3.Connection) -> None:
    """Migration 020 backfill: copy existing tg_drafts into tg_dispatch.

    Sprint Y (DD 2026-07-20 22:33 MSK): existing tg_drafts rows lack a
    status column. We derive it from draft_posts:
      - tg_channel_published_at IS NOT NULL → 'published_tg'
      - else → 'pending_tg_text' (will be regenerated by tick=generate_for_tg)

    Telegraph URL / message id / message url come from draft_posts.tg_channel_*
    columns (set by Sprint X publisher) so we don't lose them.
    """
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tg_drafts'")
    if not cur.fetchone():
        print("[sprint-y] no legacy tg_drafts table — fresh install, nothing to backfill")
        return
    cur = conn.execute("SELECT COUNT(*) FROM tg_dispatch")
    already = cur.fetchone()[0]
    if already > 0:
        print(f"[sprint-y] tg_dispatch already has {already} rows — backfill skipped")
        return
    conn.execute("""
        INSERT INTO tg_dispatch (
            post_id, tg_title, tg_teaser, tg_hashtags_json,
            prompt_version, note,
            status, telegraph_url, tg_message_id, tg_message_url,
            created_at, updated_at
        )
        SELECT
            td.post_id, td.tg_title, td.tg_teaser, td.tg_hashtags_json,
            td.prompt_version, td.note,
            CASE WHEN wp.tg_channel_published_at IS NOT NULL
                 THEN 'published_tg'
                 ELSE 'pending_tg_text'
            END,
            wp.tg_channel_telegra_url,
            wp.tg_channel_message_id,
            wp.tg_channel_message_url,
            td.created_at, COALESCE(wp.updated_at, td.created_at)
        FROM tg_drafts td
        JOIN draft_posts wp ON wp.id = td.post_id
    """)
    conn.commit()
    counts = conn.execute(
        "SELECT status, COUNT(*) FROM tg_dispatch GROUP BY status"
    ).fetchall()
    summary = ", ".join(f"{r[0]}={r[1]}" for r in counts)
    print(f"[sprint-y] backfilled tg_dispatch: {summary}")
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tg_drafts'")
    if cur.fetchone():
        conn.execute("DROP TABLE tg_drafts")
        conn.commit()
        print("[sprint-y] dropped legacy tg_drafts table")


def _fixup_item_lang_from_sources(conn: sqlite3.Connection) -> None:
    """Items inserted before migration 002 have lang=NULL. Pull the value
    from their source row. Runs in a single UPDATE - fast even on big tables."""
    cur = conn.execute(
        """UPDATE candidates
              SET lang = (SELECT lang FROM sources WHERE sources.id = candidates.source_id)
            WHERE lang IS NULL
              AND source_id IN (SELECT id FROM sources WHERE lang IS NOT NULL)"""
    )
    if cur.rowcount:
        print(f"[fixup] candidates.lang backfilled for {cur.rowcount} row(s)")
    else:
        print("[fixup] candidates.lang already populated")


def _fixup_normalize_published_at(conn: sqlite3.Connection) -> None:
    """Older candidates may have RFC-2822 dates in published_at. Normalize them
    to ISO-8601 (UTC) so lexicographic sort = chronological sort."""
    from utils import to_iso
    cur = conn.execute("SELECT id, published_at FROM candidates WHERE published_at IS NOT NULL")
    updated = 0
    for row in cur.fetchall():
        new = to_iso(row["published_at"])
        if new != row["published_at"]:
            conn.execute(
                "UPDATE candidates SET published_at = ? WHERE id = ?",
                (new, row["id"]),
            )
            updated += 1
    if updated:
        print(f"[fixup] published_at normalized for {updated} row(s)")
    else:
        print("[fixup] published_at already normalized")


def _fixup_category_drift_log(conn: sqlite3.Connection) -> None:
    """Sprint 6.7: log a summary of the category distribution post-backfill.

    Pure observability, no writes. Lets the operator eyeball the closed-set
    distribution and spot drift (e.g. LLM suddenly inventing a new label
    that gets coerced to 'other').
    """
    cur = conn.execute(
        """SELECT category, COUNT(*) AS c
             FROM candidates
            WHERE category IS NOT NULL
            GROUP BY category
            ORDER BY c DESC"""
    )
    rows = cur.fetchall()
    if not rows:
        print("[fixup] no scored candidates yet (all category NULL)")
        return
    summary = ", ".join(f"{r['category']}={r['c']}" for r in rows)
    print(f"[fixup] category distribution: {summary}")


def main() -> None:
    init_db(PIPE.db_path)
    with sqlite3.connect(PIPE.db_path) as conn:
        conn.row_factory = sqlite3.Row
        run_migrations(conn)
    print("[migrate] OK")


if __name__ == "__main__":
    main()