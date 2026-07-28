"""Sprint 5.2.2: shared helpers for smoke tests.

Background: prior to this sprint every smoke test connected directly to
``PIPE.db_path`` (``data/news_memory.db``). That meant:

  - smoke runs left fixtures in the production DB (cleaned at the end,
    but the contract was fragile — add a new test, forget the cleanup,
    and you pollute prod);
  - any smoke that read from prod (e.g. ``test_rewrite_and_score_smoke``
    picking the first ``new`` item) silently broke whenever prod was
    empty (see Sprint 5.1 prod gotcha);
  - parallel test runs were unsafe (SQLite single-writer).

This module gives every smoke the same one-liner to set up a sealed,
throwaway database:

    from _smoke_lib import make_isolated_db
    db_path, conn = make_isolated_db()
    # ... do stuff ...
    # file is auto-removed at process exit

The fixture:

  1. Creates a temp directory under ``tempfile.gettempdir()`` prefixed
     with ``media<deploy-user>_smoke_<uuid8>`` so parallel runs don't collide.
  2. Writes an empty SQLite file at ``<dir>/smoke.db``.
  3. Applies ``init_db.SCHEMA`` (idempotent CREATE TABLE IF NOT EXISTS).
  4. Runs ``migrate.run_migrations`` (idempotent, tracks in
     ``_migrations`` table).
  5. Inserts a default ``sources`` row so tests that read
     ``(SELECT id FROM sources LIMIT 1)`` have something to find.
  6. Returns ``(db_path, conn)`` and registers ``atexit`` to remove
     the temp directory.

Important: this module does NOT touch ``PIPE.db_path``. Each smoke
test that needs the production code path to read from the isolated
DB must do its own ``monkeypatch.setattr(PIPE, 'db_path', str(db_path))``
(or pass the conn through to functions that take it explicitly). The
helpers below include small ``patch_db_path`` and ``restore_db_path``
for tests that want the wrapper.
"""
from __future__ import annotations

import atexit
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Tuple

from loguru import logger

PROJECT = Path(__file__).resolve().parent
import sys
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from init_db import init_db as _init_db_apply  # noqa: E402
from migrate import run_migrations  # noqa: E402


def _default_source_sql() -> str:
    """Idempotent INSERT OR IGNORE for a default test source. The
    unique constraint on ``feed_url`` means re-running on an
    already-populated test DB is a no-op."""
    return (
        "INSERT OR IGNORE INTO sources(name, feed_url, kind, enabled) "
        "VALUES (?, ?, 'rss', 1)"
    )


def make_isolated_db(
    *,
    label: str = "smoke",
    seed_default_source: bool = True,
) -> Tuple[Path, sqlite3.Connection]:
    """Create a throwaway SQLite DB with the production schema and
    migrations applied. Returns ``(db_path, conn)``.

    The temp directory is removed automatically at process exit. To
    inspect a failing DB after the smoke runs, set
    ``MEDIAFAB_KEEP_SMOKE_DB=1`` in the environment — the atexit
    cleanup is skipped in that case.

    Args:
        label: short tag for the temp dir name (helps when several
            smoke processes run in parallel and ``/tmp`` is full of
            ``media<deploy-user>_smoke_*`` folders).
        seed_default_source: insert one ``sources`` row so tests that
            do ``SELECT id FROM sources LIMIT 1`` work out of the box.
            Defaults to True — every existing smoke needs this.
    """
    tmp_root = Path(tempfile.gettempdir())
    tag = uuid.uuid4().hex[:8]
    work_dir = tmp_root / f"media<deploy-user>_smoke_{label}_{tag}"
    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = work_dir / "smoke.db"

    _init_db_apply(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    if seed_default_source:
        conn.execute(
            _default_source_sql(),
            ("Smoke Default Source", f"https://smoke.example/{tag}.xml"),
        )
        conn.commit()

    import os
    if os.getenv("MEDIAFAB_KEEP_SMOKE_DB") != "1":
        atexit.register(_safe_rmtree, work_dir)
    else:
        logger.warning(
            "MEDIAFAB_KEEP_SMOKE_DB=1 → smoke DB kept at {}", work_dir,
        )
    if os.getenv("MEDIAFAB_SMOKE_DEBUG") == "1":
        logger.info("smoke DB ready: {}", db_path)
    return db_path, conn


def _safe_rmtree(path: Path) -> None:
    """atexit handler: best-effort cleanup, swallow errors so we don't
    shadow the real test exit code."""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


# --- Helpers for tests that need PIPE.db_path to point at the smoke DB ----
def patch_db_path(db_path: Path) -> object:
    """Replace ``config.PIPE.db_path`` with the smoke DB and return a
    token for ``restore_db_path``. Use as::

        token = patch_db_path(db_path)
        try:
            ...code that reads PIPE.db_path...
        finally:
            restore_db_path(token)

    We snapshot the original value so concurrent smokes don't trample
    each other (the dataclass instance is shared process-wide)."""
    from config import PIPE
    original = PIPE.db_path
    PIPE.db_path = db_path
    return original


def restore_db_path(original: object) -> None:
    from config import PIPE
    PIPE.db_path = original  # type: ignore[assignment]