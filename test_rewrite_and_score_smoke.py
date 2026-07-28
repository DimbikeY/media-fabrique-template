"""Sprint 2 smoke test: run rewrite_and_score with a mocked LLMClient.

Verifies:
  - state machine transitions (new -> rewriting -> ready / skipped / failed)
  - post row is inserted with the expected columns
  - blocked payload routes through safety_status='violent' and status='skipped'
  - Pydantic validation failures end up as status='failed', not crash
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).parent
sys.path.insert(0, str(PROJECT))

from config import PIPE  # noqa: E402
import rewrite_and_score  # noqa: E402
from _smoke_lib import (  # noqa: E402
    make_isolated_db,
    patch_db_path,
    restore_db_path,
)
import uuid  # noqa: E402


# --- Mock LLMClient -------------------------------------------------------
class MockLLMClient:
    """Acts like LLMClient but returns canned responses keyed by URL/title."""

    def __init__(self, plan: dict) -> None:
        self.plan = plan  # {candidate_id -> "rewrite" | "blocked" | "bad_json" | "bad_schema"}
        self.calls = 0

    def rewrite(self, article: dict):
        from llm_client import RewriteResult
        self.calls += 1
        # Pick behaviour by inspecting the title the rewrite_and_score passes us.
        # We don't have candidate_id here, so we identify by URL.
        url = article.get("url") or article.get("source_url")
        for marker, mode in self.plan.items():
            if marker in (url or ""):
                break
        else:
            mode = "rewrite"

        if mode == "rewrite":
            data = {
                "blocked": False,
                "title": "Сгенерированный заголовок",
                "slug": "generated-title",
                "excerpt": "Краткий пересказ для превью.",
                "content": "<p>Тестовый контент достаточно длинный чтобы пройти min_length=1 и базово выглядеть как HTML.</p>"
                           "<p>Источник: " + (article.get("source_url") or "") + "</p>",
                "image_alt": "alt",
                "image_prompt": "A neutral stock-style photo, no text",
                "categories": ["tech"],
                "tags": ["test"],
                "meta_title": "Сгенерированный заголовок — обзор",
                "meta_description": "SEO описание для проверки пайплайна достаточно длинное.",
                "telegram_teaser": "Тизер. #test",
            }
        elif mode == "blocked":
            data = {"blocked": True, "reason": "violent", "title": "", "content": ""}
        elif mode == "bad_json":
            data = {"this": "is not", "matching": "schema"}
        elif mode == "bad_schema":
            data = {"blocked": False, "title": "", "slug": "x"}
        else:
            raise RuntimeError(f"unknown mock mode: {mode}")
        usage = {
            "prompt_tokens_provider": 100,
            "completion_tokens_provider": 50,
            "thinking_tokens_provider": 30,
            "response_chars_local": 1024,
            "reasoning_chars_local": 256,
        }
        metrics = {
            **usage,
            "prompt_chars_local": 7000,  # system_prompt (~5.6KB) + user JSON
        }
        return RewriteResult(data=data, usage=usage, metrics=metrics)


# --- Helpers --------------------------------------------------------------
def _connect():
    conn = sqlite3.connect(PIPE.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _pick_one(conn, where_status="new") -> dict:
    """Backwards-compat shim: kept for legacy callers. Picks the FIRST
    row from the smoke seed table (which is empty in isolated mode).
    New code should use ``_seed_test_item`` directly."""
    row = conn.execute(
        "SELECT id, url FROM candidates WHERE status=? AND safety_status='review' "
        "ORDER BY id LIMIT 1",
        (where_status,),
    ).fetchone()
    return dict(row) if row else None


def _seed_test_item(conn, *, title: str, url: str) -> int:
    """Sprint 5.2.2: every scenario seeds its OWN item with a unique
    guid + url. No more picking from prod, no more 'no NEW item
    available' failures when the prod queue is empty."""
    guid = f"[smoke-{uuid.uuid4().hex[:12]}]"
    src_row = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()
    assert src_row, "smoke DB must have at least one source (see _smoke_lib)"
    cur = conn.execute(
        """
        INSERT INTO candidates (
            source_id, guid, url, title, summary, status, safety_status,
            lang, video_embed_url
        ) VALUES (?, ?, ?, ?, '', 'new', 'review', 'en', NULL)
        RETURNING id
        """,
        (src_row["id"], guid, url, title),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    return new_id


def _load_item(conn, candidate_id: int) -> sqlite3.Row:
    """Load the row shape that ``process_one`` expects (joined with
    sources)."""
    return conn.execute(
        """
        SELECT i.*, s.name AS source_name, s.feed_url AS source_url
          FROM candidates i JOIN sources s ON s.id=i.source_id
         WHERE i.id=?
        """,
        (candidate_id,),
    ).fetchone()


def _reset_to_new(conn, candidate_id: int) -> None:
    conn.execute(
        "UPDATE candidates SET status='new', safety_status='review', error_reason=NULL "
        "WHERE id=?",
        (candidate_id,),
    )
    conn.execute("DELETE FROM draft_posts WHERE candidate_id=?", (candidate_id,))
    conn.commit()


def _state(conn, candidate_id: int) -> dict:
    r = conn.execute(
        "SELECT status, safety_status, error_reason FROM candidates WHERE id=?",
        (candidate_id,),
    ).fetchone()
    return dict(r) if r else {}


def _post(conn, candidate_id: int) -> dict | None:
    r = conn.execute(
        "SELECT title, slug, status, categories_json, tags_json "
        "FROM draft_posts WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    return dict(r) if r else None


# --- Scenarios ------------------------------------------------------------
def scenario_rewrite() -> None:
    print("\n[scenario] rewrite -> ready")
    with _connect() as conn:
        # Sprint 5.2.2: seed our own item, don't read from prod.
        url = f"https://smoke.example/rewrite-{uuid.uuid4().hex[:8]}"
        candidate_id = _seed_test_item(conn, title="Smoke rewrite article", url=url)
        row = _load_item(conn, candidate_id)
        client = MockLLMClient(plan={url: "rewrite"})
        if not rewrite_and_score._claim(conn, candidate_id):
            raise SystemExit("could not claim item")
        conn.commit()
        status = rewrite_and_score.process_one(client, conn, row)
        assert status == "ready", f"expected ready, got {status}"
        st = _state(conn, candidate_id)
        assert st["status"] == "ready"
        assert st["safety_status"] == "review"
        p = _post(conn, candidate_id)
        assert p and p["slug"] == "generated-title"
        assert json.loads(p["categories_json"]) == ["tech"]
        print("  item state:", st)
        print("  post row:", p)


def scenario_blocked() -> None:
    print("\n[scenario] blocked -> skipped / violent")
    with _connect() as conn:
        url = f"https://smoke.example/blocked-{uuid.uuid4().hex[:8]}"
        candidate_id = _seed_test_item(conn, title="Smoke blocked article", url=url)
        row = _load_item(conn, candidate_id)
        client = MockLLMClient(plan={url: "blocked"})
        rewrite_and_score._claim(conn, candidate_id); conn.commit()
        status = rewrite_and_score.process_one(client, conn, row)
        assert status == "skipped", status
        st = _state(conn, candidate_id)
        assert st["status"] == "skipped"
        assert st["safety_status"] == "violent"
        assert "blocked:violent" in (st["error_reason"] or "")
        assert _post(conn, candidate_id) is None
        print("  item state:", st)


def scenario_bad_schema() -> None:
    print("\n[scenario] bad_schema -> failed")
    with _connect() as conn:
        url = f"https://smoke.example/bad-schema-{uuid.uuid4().hex[:8]}"
        candidate_id = _seed_test_item(conn, title="Smoke bad-schema article", url=url)
        row = _load_item(conn, candidate_id)
        client = MockLLMClient(plan={url: "bad_schema"})
        rewrite_and_score._claim(conn, candidate_id); conn.commit()
        status = rewrite_and_score.process_one(client, conn, row)
        assert status == "failed", status
        st = _state(conn, candidate_id)
        assert st["status"] == "failed"
        assert "pydantic" in (st["error_reason"] or "").lower() or \
               "validation" in (st["error_reason"] or "").lower()
        print("  item state:", st)


# --- Main -----------------------------------------------------------------
def main() -> int:
    # Sprint 5.2.2: rewrite_and_score smoke runs against an isolated
    # DB. The temp directory is auto-removed at process exit.
    db_path, conn = make_isolated_db(label="rewrite")
    conn.close()
    original_db_path = patch_db_path(db_path)
    try:
        scenarios = [scenario_rewrite, scenario_blocked, scenario_bad_schema]
        failed = 0
        for fn in scenarios:
            try:
                fn()
            except AssertionError as e:
                print(f"  FAIL: {e}")
                failed += 1
            except Exception as e:
                print(f"  ERROR: {type(e).__name__}: {e}")
                failed += 1

        # Verify llm_runs metrics were written
        try:
            with _connect() as conn:
                runs = list(conn.execute(
                    "SELECT candidate_id, status, stage, duration_ms, "
                    "prompt_tokens_provider, completion_tokens_provider, "
                    "thinking_tokens_provider, response_chars_local, "
                    "reasoning_chars_local, prompt_chars_local "
                    "FROM llm_runs ORDER BY id"
                ))
                cols = [c[1] for c in conn.execute("PRAGMA table_info(llm_runs)").fetchall()]
            print("\n[scenario] llm_runs rows recorded:")
            for r in runs:
                print(" ", tuple(r))
            # Expect at least one row per scenario (3+ rows total).
            if len(runs) < 3:
                print(f"  FAIL: expected >=3 llm_runs rows, got {len(runs)}")
                failed += 1
            # Expect the new schema with explicit _provider/_local suffixes
            for required in (
                "prompt_tokens_provider", "completion_tokens_provider",
                "thinking_tokens_provider", "response_chars_local",
                "reasoning_chars_local", "prompt_chars_local", "stage",
            ):
                if required not in cols:
                    print(f"  FAIL: llm_runs missing column {required}")
                    failed += 1
            # Expect legacy column names to be gone
            for legacy in ("prompt_tokens", "completion_tokens", "thinking_tokens",
                           "response_chars", "reasoning_chars", "total_tokens"):
                if legacy in cols:
                    print(f"  FAIL: legacy column '{legacy}' still present")
                    failed += 1
            # Expect no row with stage='pydantic' or 'json' (renamed)
            for r in runs:
                if r[2] in ("pydantic", "json"):
                    print(f"  FAIL: legacy stage value '{r[2]}' still in use")
                    failed += 1
        except Exception as e:
            print(f"  ERROR checking llm_runs: {type(e).__name__}: {e}")
            failed += 1

        if failed:
            print(f"\n{failed} scenario(s) failed")
            return 1
        print("\nAll Sprint 2 smoke scenarios passed.")
        return 0
    finally:
        restore_db_path(original_db_path)


if __name__ == "__main__":
    sys.exit(main())