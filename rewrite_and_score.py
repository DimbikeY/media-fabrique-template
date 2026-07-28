"""Sprint 5.1: rewrite AND score.

Single responsibility, single LLM call per item:

  1. Take a NEW item from the queue.
  2. Send it to the LLM with the master_prompt contract.
  3. From the same JSON response, extract BOTH the rewritten article
     AND its priority/category metadata.
  4. Persist a draft ``draft_posts`` row (the rewrite).
  5. Persist scoring columns on ``candidates`` (priority → weight → expires_at).
  6. Move the item through the state machine.

Why one file, two verbs ("rewrite AND score")?

  - It costs us one LLM call instead of two (no double-token burn).
  - The LLM judges priority in the same context where it judged the
    rewrite — consistent, not two contradictory takes.
  - Downstream image_processor.py and publisher.py read score columns
    via JOIN and prioritise top-weight draft_posts first.

State transitions:
    new      -> rewriting      (taken under our process id, optimistic)
    rewriting-> ready          (LLM ok, draft post written, score saved)
    rewriting-> skipped        (LLM flagged blocked=true)
    rewriting-> failed         (LLM error or invalid payload)

The script is safe to run repeatedly. Items already past `new` are skipped.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from config import LLM, PIPE, WP
from llm_client import LLMClient
from models import BlockedOutput, RewriteOutput, parse_llm_payload
from scoring import ScoringResult, score_item

# Stable identifier of the current process for observability. Two cron
# launches on the same host will share hostname but differ in PID; two
# hosts would also differ in hostname. Enough to spot which run grabbed
# a given item when triaging a stuck queue.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


# --- Mapping from LLM "reason" to safety_status -----------------------------
# Keep this conservative: anything we don't recognise goes to "review" so a
# human (Sprint 7) can re-check. We don't want silent black-holes.
_REASON_TO_SAFETY = {
    "violent": "violent",     # SVO, armed conflict, etc.
    "political": "political",
    "vpn": "vpn",
    "inoagent": "inoagent",
    "meta": "meta",
}


# --- DB helpers -------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(PIPE.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _fetch_candidates(conn: sqlite3.Connection, limit: int) -> List[sqlite3.Row]:
    """Pick up to `limit` candidates that are still NEW and awaiting review.

    Ordering (Sprint 5.1):
      1. Scored candidates first, ordered by ``weight DESC`` (highest priority
         freshest-decayed wins).
      2. Unscored candidates last (``weight IS NULL``) ordered by recency
         — they go through the LLM first and join the ranked queue on
         the next tick.

    SQLite has no ``NULLS LAST``. We emulate it with a CASE that puts
    NULL rows at the end of the sort key.
    """
    cur = conn.execute(
        """
        SELECT i.id, i.source_id, i.guid, i.url, i.title, i.summary, i.body,
               i.published_at, i.fetched_at, i.image_url, i.video_embed_url, i.lang,
               i.base_score, i.weight, i.category, i.half_life_h, i.expires_at,
               s.name AS source_name, s.feed_url AS source_url
          FROM candidates i
          JOIN sources s ON s.id = i.source_id
         WHERE i.status = 'new'
           AND i.safety_status = 'review'
         ORDER BY
           CASE WHEN i.weight IS NULL THEN 1 ELSE 0 END,
           i.weight DESC,
           COALESCE(i.published_at, i.fetched_at) DESC
         LIMIT ?
        """,
        (limit,),
    )
    return cur.fetchall()


def _claim(conn: sqlite3.Connection, candidate_id: int) -> bool:
    """Atomically transition new -> rewriting. Returns True if we got it.

    We also stash WORKER_ID into error_reason as a soft 'taken by' marker.
    It's overwritten on every state transition (ready/skipped/failed), so
    the only time you'll see it is while the item is actively being
    processed or if a run died mid-flight — both useful signals.
    """
    cur = conn.execute(
        "UPDATE candidates SET status='rewriting', error_reason=? "
        "WHERE id = ? AND status = 'new'",
        (f"worker:{WORKER_ID}", candidate_id),
    )
    return cur.rowcount == 1


def _row_to_article(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "source_url": row["source_url"],
        "source_name": row["source_name"],
        "title": row["title"] or "",
        "url": row["url"] or "",
        "summary": row["summary"] or "",
        "body": row["body"] or "",
        "published_at": row["published_at"] or "",
        "image_url": row["image_url"] or "",
        "video_embed_url": row["video_embed_url"] or "",
        "lang": row["lang"] or "",
    }


def _store_post(conn: sqlite3.Connection, candidate_id: int, out: RewriteOutput) -> int:
    cur = conn.execute(
        """
        INSERT INTO draft_posts (
            candidate_id, title, slug, excerpt, content_html,
            meta_title, meta_description, image_alt, image_prompt,
            categories_json, tags_json, telegram_teaser,
            status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')
        """,
        (
            candidate_id,
            out.title,
            out.slug,
            out.excerpt,
            out.content,
            out.meta_title,
            out.meta_description,
            out.image_alt,
            out.image_prompt,
            json.dumps(out.categories, ensure_ascii=False),
            json.dumps(out.tags, ensure_ascii=False),
            out.telegram_teaser,
        ),
    )
    return cur.lastrowid


def _mark_ready(conn: sqlite3.Connection, candidate_id: int) -> None:
    """Status-only fast-path kept for tests that don't care about scoring.

    Production code goes through `_mark_ready_with_score` instead. We
    keep the legacy signature so existing tests don't need to be edited
    in lockstep.
    """
    conn.execute(
        "UPDATE candidates SET status='ready', error_reason=NULL WHERE id=?",
        (candidate_id,),
    )


def _mark_ready_with_score(
    conn: sqlite3.Connection,
    candidate_id: int,
    score: "scoring.ScoringResult",
) -> None:
    """Move item into ``ready`` and persist its score/ageing metadata.

    One UPDATE keeps the state machine and the score columns consistent
    — there's no window where the item is 'ready' but unscored. ScoringResult
    carries base_score, category, half_life_h, weight (== base_score at
    t=0), expires_at, scored_at. See scoring.py for the math.
    """
    d = score.as_db_dict()
    conn.execute(
        """
        UPDATE candidates
           SET status='ready',
               error_reason=NULL,
               base_score   = :base_score,
               category     = :category,
               half_life_h  = :half_life_h,
               weight       = :weight,
               expires_at   = :expires_at,
               scored_at    = :scored_at
         WHERE id = :id
        """,
        {**d, "id": candidate_id},
    )


def _mark_skipped(conn: sqlite3.Connection, candidate_id: int, reason: str) -> None:
    safety = _REASON_TO_SAFETY.get(reason, "review")
    conn.execute(
        "UPDATE candidates SET status='skipped', safety_status=?, error_reason=? "
        "WHERE id=?",
        (safety, f"blocked:{reason}", candidate_id),
    )


def _mark_failed(conn: sqlite3.Connection, candidate_id: int, reason: str) -> None:
    # Keep safety_status='review' so a re-run (after prompt/fix) can pick it up
    # if we manually flip status back to 'new'.
    conn.execute(
        "UPDATE candidates SET status='failed', error_reason=? WHERE id=?",
        (reason[:500], candidate_id),
    )


def _record_llm_run(
    conn: sqlite3.Connection,
    candidate_id: int,
    *,
    started_at: float,
    duration_ms: int,
    metrics: Optional[Dict[str, Any]],
    status: str,                # ok | blocked | failed
    stage: Optional[str] = None,  # ingest | llm_parse | llm_validate | llm_request | llm_auth | llm_quota | db_write
) -> None:
    """Write one row into llm_runs for later analysis.

    `metrics` is the dict from RewriteResult.metrics: provider-reported
    numbers (_provider suffix) and our own char counts (_local suffix).
    """
    m = metrics or {}
    conn.execute(
        """
        INSERT INTO llm_runs (
            candidate_id, model, started_at, duration_ms,
            prompt_tokens_provider, completion_tokens_provider,
            thinking_tokens_provider, response_chars_local,
            reasoning_chars_local, prompt_chars_local,
            status, stage
        ) VALUES (?, ?, datetime(?, 'unixepoch'), ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            LLM.model,
            started_at,
            duration_ms,
            m.get("prompt_tokens_provider"),
            m.get("completion_tokens_provider"),
            m.get("thinking_tokens_provider"),
            m.get("response_chars_local"),
            m.get("reasoning_chars_local"),
            m.get("prompt_chars_local"),
            status,
            stage,
        ),
    )


def _summarize_error(e: Exception) -> str:
    """Compress a Pydantic ValidationError (or anything else) into a short
    one-line summary suitable for candidates.error_reason. The full trace still
    goes to loguru via the caller.
    """
    # Pydantic v2
    errors_fn = getattr(e, "errors", None)
    if callable(errors_fn):
        try:
            errs = errors_fn()
            if isinstance(errs, list) and errs:
                missing = sorted({
                    ".".join(str(x) for x in err.get("loc", ()))
                    for err in errs
                    if err.get("type") == "missing"
                })
                too_short = sorted({
                    ".".join(str(x) for x in err.get("loc", ()))
                    for err in errs
                    if err.get("type") == "string_too_short"
                })
                if missing:
                    return f"pydantic: missing fields {missing}"
                if too_short:
                    return f"pydantic: empty fields {too_short}"
                types = sorted({err.get("type", "?") for err in errs})
                return f"pydantic: invalid ({', '.join(types[:3])})"
        except Exception:
            pass
    # Fallback: just the class name + truncated str
    return f"{type(e).__name__}: {str(e)[:200]}"


# --- Core processing --------------------------------------------------------
def process_one(
    client: LLMClient, conn: sqlite3.Connection, row: sqlite3.Row
) -> str:
    """Process a single item. Returns the final status string."""
    candidate_id = row["id"]
    article = _row_to_article(row)

    logger.info(
        "Processing item id={} source={} lang={} title={!r}",
        candidate_id,
        article["source_name"],
        article["lang"],
        (article["title"] or "")[:80],
    )

    started = time.monotonic()
    started_unix = time.time()
    try:
        rewrite_result = client.rewrite(article)
        payload = rewrite_result.data
        metrics = rewrite_result.metrics
    except Exception as e:
        duration_ms = int((time.monotonic() - started) * 1000)
        # Classify the failure stage for the metrics table.
        msg = str(e).lower()
        if "pydantic" in msg or "validation" in msg:
            stage = "llm_validate"
        elif "json" in msg or "no json object" in msg or "unbalanced" in msg:
            stage = "llm_parse"
        elif "auth" in msg or "401" in msg or "403" in msg:
            stage = "llm_auth"
        elif "quota" in msg or "429" in msg:
            stage = "llm_quota"
        elif "connection" in msg or "timeout" in msg or "rate" in msg:
            stage = "llm_request"
        else:
            stage = "llm_request"
        try:
            _record_llm_run(
                conn, candidate_id,
                started_at=started_unix, duration_ms=duration_ms,
                metrics=None, status="failed", stage=stage,
            )
        except sqlite3.Error:
            # Metrics table write is best-effort; never let it block the state machine.
            pass
        logger.error("LLM failed for item {}: {}", candidate_id, e)
        _mark_failed(conn, candidate_id, f"llm:{_summarize_error(e)}")
        conn.commit()
        return "failed"

    duration_ms = int((time.monotonic() - started) * 1000)
    try:
        result = parse_llm_payload(payload)
    except Exception as e:
        # JSON parsed but didn't match the contract — validation failure.
        try:
            _record_llm_run(
                conn, candidate_id,
                started_at=started_unix, duration_ms=duration_ms,
                metrics=metrics, status="failed", stage="llm_validate",
            )
        except sqlite3.Error:
            pass
        logger.error("LLM payload validation failed for item {}: {}", candidate_id, e)
        _mark_failed(conn, candidate_id, f"llm:{_summarize_error(e)}")
        conn.commit()
        return "failed"

    if isinstance(result, BlockedOutput):
        try:
            _record_llm_run(
                conn, candidate_id,
                started_at=started_unix, duration_ms=duration_ms,
                metrics=metrics, status="blocked", stage=None,
            )
        except sqlite3.Error:
            pass
        logger.info(
            "Item {} blocked by LLM (reason={})", candidate_id, result.reason
        )
        _mark_skipped(conn, candidate_id, result.reason)
        conn.commit()
        # Sprint 6.5: notify the observability feedback topic so DD can
        # weigh in on whether the safety decision was right. Best-effort,
        # rate-limited inside the bridge.
        try:
            import tg_bridge
            row = conn.execute(
                "SELECT title FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            title = row["title"] if row else None
            tg_bridge.push_feedback(candidate_id, title or "", result.reason)
        except Exception as e:
            logger.warning("tg_bridge.push_feedback failed: {}", e)
        return "skipped"

    try:
        _store_post(conn, candidate_id, result)
        # Sprint 5.1: persist the scoring data we got back from the LLM
        # in the same JSON as the rewrite. priority is optional (newer
        # models emit it, older fall back to 0); category drives the
        # half-life through CATEGORY_HALF_LIFE_H in scoring.py.
        #
        # Sprint 6.7: ``result.category`` has already been coerced to a
        # WHITELIST key by ``models.RewriteOutput._coerce_category``, so
        # passing it through ``score_item`` is safe — it will only log a
        # drift warning if the model bypassed Pydantic somehow (e.g. by
        # returning a dict-like payload we re-validated).
        fetched_at_str = conn.execute(
            "SELECT fetched_at FROM candidates WHERE id=?", (candidate_id,)
        ).fetchone()[0]
        from datetime import datetime as _dt  # local import, used twice
        try:
            fetched_at = _dt.strptime(fetched_at_str, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            fetched_at = _dt.utcnow()  # corrupted row → use now, no crash
        scoring_result: ScoringResult = score_item(
            priority=result.priority if result.priority is not None else 0.0,
            category=result.category,
            fetched_at=fetched_at,
        )
        _mark_ready_with_score(conn, candidate_id, scoring_result)
        try:
            _record_llm_run(
                conn, candidate_id,
                started_at=started_unix, duration_ms=duration_ms,
                metrics=metrics, status="ok", stage=None,
            )
        except sqlite3.Error:
            pass
        conn.commit()
        # Sprint 6.6: push draft preview to #drafts so DD can approve/reject.
        # Sprint 6.6.1: the WP post is created lazily by publisher.py — at
        # this point draft_posts.wp_post_id is NULL, so we can't link to
        # the WP admin-edit URL yet. Fall back to a slug-based admin search
        # URL which lands DD on the post list filtered to this draft's
        # slug (the post won't exist in WP until publisher publishes it,
        # but the search results page itself is reachable). This is the
        # least-bad link we can produce pre-WP-POST.
        # Best-effort — a failure here must NOT roll back the commit.
        try:
            import tg_bridge
            from urllib.parse import quote as _quote
            post_row = conn.execute(
                "SELECT id, title, slug FROM draft_posts WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if post_row:
                # Slug-based admin search URL: lands on the post list
                # filtered to this slug + draft status only (so the link
                # doesn't auto-jump to an already-published sibling).
                if post_row["slug"]:
                    preview_url = (
                        f"{WP.base_url}/wp-admin/edit.php"
                        f"?post_type=post&post_status=draft&s={_quote(post_row['slug'])}"
                    )
                else:
                    # No slug yet — fallback to admin post list filtered
                    # to drafts only.
                    preview_url = f"{WP.base_url}/wp-admin/edit.php?post_type=post&post_status=draft"
                tg_bridge.push_draft_preview(
                    draft_post_id=post_row["id"],
                    title=post_row["title"] or result.title or "",
                    wp_preview_url=preview_url,
                    weight=scoring_result.base_score,
                    category=result.category,
                )
        except Exception as e:
            logger.warning("push_draft_preview failed for candidate {}: {}", candidate_id, e)

        # Sprint X hotfix (DD 2026-07-20 07:14 MSK): without a tg_dispatch row
        # the auto-publish path in publisher.py hits
        # `PostNotFound: no tg_dispatch for post_id; call tg_regenerate() first`
        # and our new telegraph-required gate rolls the WP publish back. So
        # we generate the tg_dispatch row right here, on the writer side. Best
        # effort: failure here is logged but does NOT mark the candidate
        # failed (the WP draft is still valid; the TG channel draft can be
        # regenerated later via /preview_tg).
        try:
            import tg_regenerate as _tg_regen
            _tg_regen.tg_regenerate(post_row["id"])
            logger.info("tg_dispatch generated for post_id={}", post_row["id"])
        except _tg_regen.PostNotFound:
            logger.warning("tg_regenerate: draft_posts row missing? post_id={}", post_row["id"])
        except Exception as e:
            logger.warning(
                "tg_regenerate failed for post_id={} (non-fatal, /preview_tg can retry): {}",
                post_row["id"], e,
            )

        logger.success(
            "Item {} ready: post written (slug={!r}, priority={}, cat={!r})",
            candidate_id, result.slug, scoring_result.base_score, scoring_result.category,
        )
        return "ready"
    except sqlite3.Error as e:
        # DB write failed after LLM succeeded. Mark failed but record the
        # stage so we know the LLM itself worked.
        try:
            _record_llm_run(
                conn, candidate_id,
                started_at=started_unix, duration_ms=duration_ms,
                metrics=metrics, status="failed", stage="db_write",
            )
        except sqlite3.Error:
            pass
        logger.error("DB error writing post for item {}: {}", candidate_id, e)
        conn.rollback()
        _mark_failed(conn, candidate_id, f"db:{e}")
        conn.commit()
        return "failed"


def run(limit: Optional[int] = None) -> Dict[str, int]:
    """Process up to `limit` candidates (default = PIPE.max_items_per_run)."""
    n = limit if limit is not None else PIPE.max_items_per_run
    counts = {"ready": 0, "skipped": 0, "failed": 0, "skipped_already": 0}

    try:
        client = LLMClient()
    except RuntimeError as e:
        logger.error("{}", e)
        return counts

    with _connect() as conn:
        rows = _fetch_candidates(conn, n)
        if not rows:
            logger.info("No NEW candidates to process.")
            return counts

        for row in rows:
            if not _claim(conn, row["id"]):
                # Another process beat us to it (shouldn't happen with one
                # process, but be defensive).
                counts["skipped_already"] += 1
                continue
            conn.commit()  # persist the claim
            status = process_one(client, conn, row)
            counts[status] = counts.get(status, 0) + 1

        # Sprint 6.5: flush any feedback candidates accumulated during this
        # tick. Safe to call even when nothing was blocked (it's a no-op
        # on an empty bucket). Doing this inside the same tick that
        # generated the events is what keeps DD's chat from being silent
        # for hours when a burst of candidates is rejected in one run.
        try:
            import tg_bridge
            tg_bridge.flush_feedback_now()
        except Exception as e:
            logger.warning("tg_bridge.flush_feedback_now failed: {}", e)

    logger.info("Run summary: {}", counts)
    return counts


def _parse_args(argv: Optional[list[str]] = None) -> int:
    """Parse --limit and return the override (or -1 if not provided)."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="rewrite_and_score",
        description="Rewrite candidates via LLM AND persist score metadata.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max candidates to process this run. Overrides MAX_ITEMS_PER_RUN.",
    )
    args = parser.parse_args(argv)
    return args.limit if args.limit is not None else -1


def main() -> int:
    # Don't crash the cron if there's nothing to do — that's a normal state.
    try:
        limit_override = _parse_args()
        if limit_override is not None and limit_override >= 0:
            run(limit=limit_override)
        else:
            run()
    except SystemExit:
        # argparse exits via SystemExit on --help / parse error
        raise
    except Exception:
        logger.error("Unhandled exception:\n{}", traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())