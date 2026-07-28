"""Initialize the SQLite database with all tables the pipeline needs.

Run once before the first run:
    python init_db.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from config import PIPE, LOGS_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    feed_url      TEXT    NOT NULL UNIQUE,
    kind          TEXT    NOT NULL DEFAULT 'rss',     -- rss | html | telegram
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL,
    guid          TEXT    NOT NULL,            -- RSS guid / url
    url           TEXT,
    title         TEXT,
    summary       TEXT,
    body          TEXT,
    published_at  TEXT,
    fetched_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    status        TEXT    NOT NULL DEFAULT 'new',  -- new|rewriting|ready|published|failed|skipped
    error_reason  TEXT,
    -- Sprint 6.7: closed-set whitelist for the scoring label. Mirrors
    -- scoring.WHITELIST in Python. Defense-in-depth: the rewrite pipeline
    -- already validates via Pydantic + validate_category, this CHECK
    -- is a backstop against direct-SQL writes (test fixtures, manual
    -- edits, migrations that forget to normalize).
    category      TEXT    CHECK (category IS NULL OR category IN (
        'politics','sports','ai','entertainment','business','tech',
        'vibe-coding','health','other','culture','science','nature'
    )),
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE,
    UNIQUE (source_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_candidates_status   ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_pub_at   ON candidates(published_at);

CREATE TABLE IF NOT EXISTS draft_posts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id     INTEGER NOT NULL,
    wp_post_id       INTEGER,
    wp_post_url      TEXT,
    title            TEXT,
    slug             TEXT,
    excerpt          TEXT,
    content_html     TEXT,
    meta_title       TEXT,
    meta_description TEXT,
    featured_image_path TEXT,
    image_alt        TEXT,
    image_prompt     TEXT,
    categories_json  TEXT,
    tags_json        TEXT,
    telegram_teaser  TEXT,
    status           TEXT NOT NULL DEFAULT 'draft', -- draft|publishing|published|failed
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    telegram_sent    INTEGER NOT NULL DEFAULT 0,
    error_reason     TEXT,
    -- Sprint 6 (channel-prompt): TG-channel "<your_channel>" publication state.
    -- Idempotency: once tg_channel_published_at is set, /approve_tg won't
    -- double-post. message_id+message_url kept for audit + /preview link.
    tg_channel_published_at TEXT,
    tg_channel_message_id   INTEGER,
    tg_channel_message_url  TEXT,
    FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_draft_posts_status ON draft_posts(status);
CREATE INDEX IF NOT EXISTS idx_draft_posts_tg_pub ON draft_posts(tg_channel_published_at);

-- Sprint 6 (channel-prompt): TG-channel drafts (separate from
-- telegram_teaser in draft_posts, which is for the observability group).
-- Sprint Y (DD 2026-07-20 22:33 MSK): renamed tg_drafts → tg_dispatch and
-- added a status column. The fresh-install path now creates tg_dispatch
-- directly; the legacy tg_drafts table is created by migration 018 only
-- (preserved verbatim for backward compatibility with old installs).
CREATE TABLE IF NOT EXISTS tg_dispatch (
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
CREATE INDEX IF NOT EXISTS idx_tg_dispatch_status_post   ON tg_dispatch(status, post_id);
CREATE INDEX IF NOT EXISTS idx_tg_dispatch_post_created  ON tg_dispatch(post_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tg_dispatch_approved_at   ON tg_dispatch(approved_at);

-- Reserved for Sprint 7 (smart sentiment of comments)
CREATE TABLE IF NOT EXISTS comments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id      INTEGER NOT NULL,
    author       TEXT,
    text         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    sentiment    TEXT,   -- positive|neutral|negative
    score        REAL,   -- -1.0..1.0
    FOREIGN KEY (post_id) REFERENCES draft_posts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);

-- Per-LLM-call metrics. Used to spot long thinking, token bloat, repeated
-- failures of a particular provider/model so we can tune later.
CREATE TABLE IF NOT EXISTS llm_runs (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id              INTEGER NOT NULL,
    model                     TEXT,
    started_at                TEXT NOT NULL DEFAULT (datetime('now')),
    duration_ms               INTEGER,
    prompt_tokens_provider    INTEGER,
    completion_tokens_provider INTEGER,
    thinking_tokens_provider  INTEGER,
    response_chars_local      INTEGER,
    reasoning_chars_local     INTEGER,
    prompt_chars_local        INTEGER,
    status                    TEXT,   -- ok | blocked | failed
    stage                     TEXT,   -- ingest | llm_parse | llm_validate | llm_request | llm_auth | llm_quota | db_write | (null on ok/blocked)
    FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_llm_runs_candidate ON llm_runs(candidate_id);
CREATE INDEX IF NOT EXISTS idx_llm_runs_started  ON llm_runs(started_at);
"""


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        # If the DB already has a legacy 'items' table, skip the SCHEMA
        # script: CREATE TABLE IF NOT EXISTS would be a no-op for the
        # existing tables, but the CREATE INDEX statements would still try
        # to reference columns (e.g. candidate_id) that don't exist yet
        # on the legacy schema. Migrations 014 onward handle the rename.
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='items'"
        )
        if cur.fetchone() is not None:
            print(f"[init_db] legacy schema detected, skipping SCHEMA apply: {path}")
            return
        conn.executescript(SCHEMA)
        conn.commit()
    print(f"[init_db] OK: {path}")


if __name__ == "__main__":
    init_db(PIPE.db_path)
    sys.exit(0)
