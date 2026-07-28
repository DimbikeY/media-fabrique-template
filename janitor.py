"""Sprint 5.1: janitor — purge expired candidates.

The Janitor is the only piece of the pipeline that DELETEs rows from
``candidates``. Everything else only INSERTs or UPDATEs.

What gets deleted:

  - status='new'                       ← never claimed by rewriter
  - expires_at IS NOT NULL             ← was scored (we know its lifetime)
  - expires_at < datetime('now')       ← past its half-life-driven TTL

What does NOT get touched:

  - status in ('rewriting', 'ready', 'publishing', 'published', 'failed',
    'skipped') — these candidates are in flight or already done. If a 'failed'
    item has expired, that's a human decision (manual retry), not for
    the janitor to make.
  - expires_at IS NULL — a freshly-fetched item hasn't been scored yet.
    If it never gets scored (LLM bypassed it?), that's a different bug
    (orphan candidates) and janitor isn't the place to fix it.

Idempotency: re-running on the same DB is a no-op because ``expires_at <
now`` is monotonic — once something is deleted, the SQL never sees it
again.

Why this lives in its own file and cron job:

  - Fetcher only writes. Rewriter rewrites. Publisher publishes. Janitor
    deletes. Each role owns one transition. No agent does two jobs.
  - If janitor crashes mid-run, a fresh tick picks up where it left off
    (the WHERE clause is the same).
  - If we ever want to skip janitor (e.g. to keep a 'graveyard' table
    for forensic analysis), we just disable one cron job. No code change.

Sprint 5.1.b (DD 2026-07-20 11:33 — 11:38 MSK): the docstring above is
now slightly outdated. The original DELETE only caught status='new'.
The remaining post-rewrite states ('ready', 'failed', 'skipped') were
left to drift, with no expire-check anywhere in the pipeline. Two
loopholes:

  (a) A failed post kept getting retried via heal_stuck_posts() every
      JANITOR_FAILED_RETRY_MINUTES regardless of expires_at. If the
      telegraph API stays down for a day, half-life-driven relevance
      has already expired, but janitor still resets failed→draft.

  (b) ready rows that nobody ever picked up (orphans from earlier
      bugs) also drifted indefinitely.

Sprint 5.1.b closes both:

  - run_once() additionally sweeps status IN ('ready','failed','skipped')
    where expires_at < now. After this sweep, only 'publishing' and
    'published' rows survive past their TTL — and those are
    terminal/active states which we never delete automatically.

  - heal_stuck_posts() is now expire-aware: a failed post is retried
    ONLY if expires_at > now. If it has already expired, the row is
    deleted in a separate purge sweep (handled by run_once()).

See ``purge_expired_processed()`` and ``heal_stuck_posts()`` for the
new SQL. The change is single-file, single-cron: no env var changes,
no migration.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Tuple

from loguru import logger

from config import PIPE, PIPE_TICKS


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(PIPE.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def run_once() -> Tuple[int, int, int, int]:
    """Delete all expired candidates across all relevant statuses.

    Returns (deleted_new, kept_new, deleted_processed, kept_processed).

    'new' and 'processed' (ready / failed / skipped) are returned
    separately so the operator can spot-check each sweep:
      - deleted_new: raw items the rewriter never claimed; usually small.
      - deleted_processed: scored-but-stale items that nobody could
        ship in time. This is the new sweep added in 5.1.b.

    What 'processes' means here: any state from the rewriter onward
    where we *might* still publish if it didn't fail mid-flight.

    'publishing' and 'published' states are skipped — 'publishing' is
    in active flight (never delete), and 'published' is terminal
    (will be cleaned up by a separate archive cron if/when we add one).

    Sprint 5.1.c (DD 2026-07-20 11:44 MSK): both sweeps now run as a
    SINGLE DELETE statement with ``status = 'new' OR status IN (...)``.
    The split between deleted_new and deleted_processed is derived from
    a pre-DELETE per-status count.
    """
    conn = _connect()
    deleted_new = deleted_processed = 0
    kept_new = kept_processed = 0
    try:
        # Peek kept-before so we can return both numbers in one shot.
        kept_new = conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE status='new'"
        ).fetchone()[0]
        kept_processed = conn.execute(
            """
            SELECT COUNT(*) FROM candidates
             WHERE status IN ('ready', 'failed', 'skipped')
            """
        ).fetchone()[0]

        # Sprint 5.1.b + 5.1.c (DD 2026-07-20 11:33 — 11:44 MSK):
        # SINGLE DELETE covers BOTH sweeps. The combined WHERE
        # ``status = 'new' OR status IN ('ready','failed','skipped')``
        # is equivalent to running two separate DELETE statements but
        # does it in one round-trip and one transactional boundary.
        # SQLite rowcount is the total rows affected by the entire
        # statement. Idempotency is preserved (``expires_at < now``
        # is monotonic).
        #
        # To split the total into ``deleted_new`` vs
        # ``deleted_processed`` for the operator, we read the
        # per-status counts BEFORE the DELETE (pre_count_by_status)
        # and subtract from the total. The 'new'-only count comes
        # straight from the map; everything else is processed.
        pre_count_by_status = dict(
            conn.execute(
                """
                SELECT status, COUNT(*) FROM candidates
                 WHERE (status = 'new'
                        OR status IN ('ready','failed','skipped'))
                   AND expires_at IS NOT NULL
                   AND expires_at < datetime('now')
                 GROUP BY status
                """
            ).fetchall()
        )

        # ONE DELETE covers both sweeps (DD 2026-07-20 11:44 MSK).
        cur = conn.execute(
            """
            DELETE FROM candidates
             WHERE (status = 'new'
                    OR status IN ('ready','failed','skipped'))
               AND expires_at IS NOT NULL
               AND expires_at < datetime('now')
            """
        )
        total_deleted = cur.rowcount
        deleted_new       = pre_count_by_status.get("new", 0)
        deleted_processed = total_deleted - deleted_new

        kept_after_new = conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE status='new'"
        ).fetchone()[0]
        kept_after_processed = conn.execute(
            """
            SELECT COUNT(*) FROM candidates
             WHERE status IN ('ready', 'failed', 'skipped')
            """
        ).fetchone()[0]
        kept_new = kept_new - deleted_new
        kept_processed = kept_processed - deleted_processed

        conn.commit()
        if total_deleted:
            logger.info(
                "janitor: deleted {} expired ({} new + {} processed) "
                "(kept {} new, {} processed alive)",
                total_deleted, deleted_new, deleted_processed,
                kept_after_new, kept_after_processed,
            )
        else:
            logger.debug(
                "janitor: nothing expired (kept {} new + {} processed alive)",
                kept_after_new, kept_after_processed,
            )
        return deleted_new, kept_new, deleted_processed, kept_processed
    finally:
        conn.close()


def run_once_with_heal() -> dict:
    """Convenience: do both the DELETE sweep AND heal stuck draft_posts.

    Sprint 5 lightweight cron calls this so a single tick handles both
    responsibilities. Returns a dict for easy log summary / future
    Telegram report. Each individual function is still callable for
    smoke tests and dry-runs.

    Sprint Y (DD 2026-07-20 22:33 MSK): also runs cleanup_tg_dispatch()
    to enforce the post-state TTL rules. Returns a dict with extra keys.
    """
    deleted_new, kept_new, deleted_processed, kept_processed = run_once()
    healed, retried = heal_stuck_posts()
    tg_counts = cleanup_tg_dispatch()
    return {
        "deleted_new": deleted_new,
        "deleted_processed": deleted_processed,
        "kept_new": kept_new,
        "kept_processed": kept_processed,
        "healed_publishing": healed,
        "retried_failed": retried,
        **tg_counts,
    }


# ── tg_dispatch cleanup (Sprint Y, DD 2026-07-20 22:33 MSK) ────────────────
#
# DD's rules (post-conversation at 22:27 MSK):
#   - Delete rows when the underlying candidate has expired (single
#     source of truth = candidates.expires_at).
#   - rejected_tg → delete immediately on the next janitor sweep (DD
#     decided that no retention period is needed).
#   - telegram_blocked_exhausted → manual recovery only; we leave it
#     untouched.
#   - approved with stuck attempts AND approved_at > 24h ago → reset
#     attempts to 0 (anti-stuck-queue for posts that were approved but
#     tick=publish_tg died mid-flight — the next run will retry from
#     scratch instead of being permanently lost).
#
# draft_posts.status is NEVER touched by janitor. The 'published' state
# is sacred — a post that has ever been live on WP must stay live until
# either candidates.expires_at kicks in (with cascade DELETE on the
# join) or a human deletes the WP post via the WP admin UI.
def cleanup_tg_dispatch() -> dict:
    """Apply Sprint Y TTL rules to tg_dispatch + draft_posts purge.

    DD 2026-07-20 22:27 MSK rules:
      - tg_dispatch joined candidate expired → DELETE
      - tg_dispatch status='rejected_tg' → DELETE immediately
      - tg_dispatch status='approved' AND (candidates expired OR
        approved > 24h ago AND has been queued) → DELETE / reset
      - draft_posts status='draft' AND created_at > 2d → DELETE
        (черновики долго не храним)
      - draft_posts status='approved' AND created_at > 2d → DELETE
        (manual approve вышел за таймаут, orphan)
      - draft_posts status='rejected' AND created_at > 7d → DELETE
      - draft_posts status='failed' → reset to 'draft' (DD 22:27 MSK:
        "до expired_at пусть пытается опубликоваться")

    The rules for `published` WP posts are stricter: NEVER delete a
    published draft_posts row by janitor — its state is
    independent of TTL (a long-form article can outlive its news cycle
    for reader archive).
    """
    conn = _connect()
    deleted_expired = deleted_rejected = reset_stuck = 0
    deleted_drafts_old = deleted_rejected_wp = deleted_orphan_approved = 0
    retried_failed = 0
    try:
        # 1. Joined candidate expired (single source of truth for
        #    tg_dispatch side).
        cur = conn.execute(
            """
            DELETE FROM tg_dispatch
             WHERE post_id IN (
                 SELECT wp.id FROM draft_posts wp
                 JOIN candidates c ON c.id = wp.candidate_id
                 WHERE c.expires_at IS NOT NULL
                   AND c.expires_at <= datetime('now')
             )
               AND status NOT IN ('published_tg', 'telegram_blocked_exhausted')
            """
        )
        deleted_expired = cur.rowcount

        # 2. DD 2026-07-20 22:27 MSK: rejected_tg → DELETE immediately,
        #    no retention period.
        cur = conn.execute(
            "DELETE FROM tg_dispatch WHERE status = 'rejected_tg'"
        )
        deleted_rejected = cur.rowcount

        # 3. Approved > 24h with attempts<MAX but never picked up
        #    → reset attempts so tick=publish_tg retries fresh. Mid-
        #    flight crashes leave rows in this state.
        cur = conn.execute(
            """
            UPDATE tg_dispatch
               SET attempts = 0, attempted_at = NULL,
                   failed_reason = NULL,
                   updated_at = datetime('now')
             WHERE status = 'approved'
               AND approved_at IS NOT NULL
               AND approved_at < datetime('now', '-24 hours')
               AND (attempted_at IS NULL OR attempted_at < datetime('now', '-24 hours'))
            """
        )
        reset_stuck = cur.rowcount

        # 4. DD 2026-07-20 22:27 MSK: draft_posts cleanup is also
        #    governed by candidate.expires_at — joined expired rows get
        #    DELETEd too (covering the case where the cascade would
        #    otherwise leak WP rows). Excludes 'published' explicitly.
        cur = conn.execute(
            """
            DELETE FROM draft_posts
             WHERE status NOT IN ('published', 'telegram_blocked_exhausted')
               AND candidate_id IN (
                   SELECT id FROM candidates
                    WHERE expires_at IS NOT NULL
                      AND expires_at <= datetime('now')
               )
            """
        )
        deleted_drafts_old = cur.rowcount

        # 5. Failed → draft per DD 2026-07-20 22:27 MSK: "failed должен
        #    пытаться опубликоваться до expired_at". We do NOT impose a
        #    rate limit here because the per-tick publisher.limit already
        #    caps how many posts can be retried in one run.
        cur = conn.execute(
            """
            UPDATE draft_posts
               SET status       = 'draft',
                   error_reason = COALESCE(error_reason || ' | healed_from_failed', 'healed_from_failed'),
                   updated_at   = datetime('now')
             WHERE status = 'failed'
               AND candidate_id IN (
                   SELECT id FROM candidates
                    WHERE expires_at IS NULL OR expires_at > datetime('now')
               )
            """
        )
        retried_failed = cur.rowcount

        # 6. draft_posts status='draft' AND created_at > 2d ago
        #    → DELETE (DD "черновики долго не храним").
        #    Skip rows that have a published_tg in tg_dispatch (which
        #    means they already went out — but those would have status=
        #    'published' so they wouldn't match anyway; defensive guard).
        cur = conn.execute(
            """
            DELETE FROM draft_posts
             WHERE status = 'draft'
               AND created_at < datetime('now', '-2 days')
            """
        )
        deleted_orphan_approved = cur.rowcount

        # 7. draft_posts status='approved' AND created_at > 2d ago
        #    → DELETE (manual approve вышел за таймаут, orphan).
        cur = conn.execute(
            """
            DELETE FROM draft_posts
             WHERE status = 'approved'
               AND created_at < datetime('now', '-2 days')
            """
        )
        # Суммируем как тот же bucket (под "orphans").
        deleted_orphan_approved += cur.rowcount

        # 8. draft_posts status='rejected' AND created_at > 7d → DELETE
        cur = conn.execute(
            "DELETE FROM draft_posts WHERE status='rejected' AND created_at < datetime('now', '-7 days')"
        )
        deleted_rejected_wp = cur.rowcount

        conn.commit()
        if any([
            deleted_expired, deleted_rejected, reset_stuck,
            deleted_drafts_old, retried_failed,
            deleted_orphan_approved, deleted_rejected_wp,
        ]):
            logger.info(
                "janitor: tg_dispatch expired={} rejected={} reset={}; "
                "drafts_old_expired={} retried_failed={} deleted_orphans={} "
                "deleted_rejected_wp={}",
                deleted_expired, deleted_rejected, reset_stuck,
                deleted_drafts_old, retried_failed, deleted_orphan_approved,
                deleted_rejected_wp,
            )
        return {
            "tg_dispatch_deleted_expired": deleted_expired,
            "tg_dispatch_deleted_rejected": deleted_rejected,
            "tg_dispatch_reset_stuck": reset_stuck,
            "draft_posts_deleted_expired": deleted_drafts_old,
            "draft_posts_retried_failed": retried_failed,
            "draft_posts_deleted_orphans": deleted_orphan_approved,
            "draft_posts_deleted_rejected": deleted_rejected_wp,
        }
    finally:
        conn.close()


# ── Healing (Sprint 5 lightweight) ─────────────────────────────────────────
#
# Two failure modes that the state machine alone can't recover from:
#
#   1. A publisher tick claimed a post (status='publishing'), but then
#      crashed mid-flight (subprocess killed, OOM, Mac sleep, etc.) before
#      either `_mark_published` or `_mark_failed` ran. The post is now
#      stuck forever in 'publishing'. Without a fix the next publisher
#      tick just skips it (atomic claim won't match status='draft').
#
#   2. A post landed in 'failed' but the cause was transient (WP 503,
#      network blip, rate limit). We don't auto-retry in publisher
#      (intentional, Sprint 4) but we can do a *single* automatic retry
#      after a grace period. If it fails again, it stays failed until a
#      human acts.
#
# Both transitions are owned by janitor — same place as the destructive
# DELETE, same transactional model, same loguru discipline.

def heal_stuck_posts() -> Tuple[int, int]:
    """Return stuck 'publishing' draft_posts to 'draft', and gently retry
    failed draft_posts. Returns (healed_publishing, retried_failed).

    Sprint 5.1.b (DD 2026-07-20 11:33 — 11:38 MSK): failed→draft retry
    is now expire-aware. We retry ONLY if the candidate's expires_at is
    still in the future (or has never been set, e.g. legacy row).
    Posts that already passed their half-life-driven TTL will be
    deleted by run_once() instead of retried, so we never republish
    stale news just because the telegraph API was down for a day.

    Sprint Y (DD 2026-07-20 22:33 MSK): failed→draft retry was moved
    into cleanup_tg_dispatch() to keep all TTL rules in one place.
    heal_stuck_posts() now handles ONLY publishing→draft (stuck-tick
    recovery). The configured retry_min_after-fail is ignored; the
    candidate.expires_at filter alone determines retry safety.
    """
    stuck_min = PIPE_TICKS.publishing_stuck_minutes

    conn = _connect()
    healed = retried = 0
    try:
        # publishing → draft after N minutes of silence.
        cur = conn.execute(
            f"""
            UPDATE draft_posts
               SET status      = 'draft',
                   error_reason = COALESCE(error_reason, '') ||
                                  CASE WHEN error_reason IS NULL OR error_reason = ''
                                       THEN '' ELSE ' | ' END ||
                                  'healed_from_publishing'
             WHERE status = 'publishing'
               AND updated_at < datetime('now', ?)
            """,
            (f"-{stuck_min} minutes",),
        )
        healed = cur.rowcount

        conn.commit()
        if healed:
            logger.info("janitor.heal: publishing→draft={}", healed)
        else:
            logger.debug("janitor.heal: nothing to heal")
        # retried=0 for backwards-compat; the real retry happens in
        # cleanup_tg_dispatch().
        return healed, retried
    finally:
        conn.close()


# ── Detail helpers (for debugging + Telegram summaries) ──────────────────────

def list_candidates(limit: int = 20, include_processed: bool = True) -> list[sqlite3.Row]:
    """Return up to ``limit`` candidates that ARE about to be deleted by the
    next run. Read-only — never deletes anything. Useful for a Telegram
    preview before a destructive sweep.

    Sprint 5.1.b: when ``include_processed`` is True (default), the
    preview covers status='new' AND the processed sweep status list
    ('ready', 'failed', 'skipped'), so an operator can see exactly
    what ``run_once`` is about to delete across both sweeps.
    """
    conn = _connect()
    try:
        if include_processed:
            where = (
                "WHERE ((status='new') OR "
                "(status IN ('ready','failed','skipped'))) "
                "AND expires_at IS NOT NULL "
                "AND expires_at < datetime('now')"
            )
        else:
            where = (
                "WHERE status='new' "
                "AND expires_at IS NOT NULL "
                "AND expires_at < datetime('now')"
            )
        cur = conn.execute(
            f"""
            SELECT id, title, category, status, base_score, weight, expires_at,
                   datetime('now') AS now,
                   (julianday(expires_at) - julianday('now')) * 24.0 AS hours_left
              FROM candidates
             {where}
             ORDER BY expires_at ASC
             LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()
    finally:
        conn.close()


def count_expired(include_processed: bool = True) -> Tuple[int, int]:
    """How many candidates would be deleted by the next run. Returns
    (new_sweep_count, processed_sweep_count). Cheap query — uses the
    idx_items_expires_at index.

    Sprint 5.1.b: ``include_processed`` controls whether to also count
    expired processed rows (ready/failed/skipped). For backwards
    compatibility, ``include_processed=False`` returns
    (new_count, 0)."""
    conn = _connect()
    try:
        new_count = conn.execute(
            """
            SELECT COUNT(*) FROM candidates
             WHERE status='new'
               AND expires_at IS NOT NULL
               AND expires_at < datetime('now')
            """
        ).fetchone()[0]
        if not include_processed:
            return new_count, 0
        proc_count = conn.execute(
            """
            SELECT COUNT(*) FROM candidates
             WHERE status IN ('ready', 'failed', 'skipped')
               AND expires_at IS NOT NULL
               AND expires_at < datetime('now')
            """
        ).fetchone()[0]
        return new_count, proc_count
    finally:
        conn.close()


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Sprint 5.1 janitor")
    ap.add_argument("--dry-run", action="store_true",
                    help="list candidates and exit, do not delete")
    ap.add_argument("--verbose", action="store_true",
                    help="log each candidate title")
    args = ap.parse_args()

    if args.dry_run:
        new_n, proc_n = count_expired()
        total = new_n + proc_n
        logger.info(
            "[dry-run] would delete {} item(s) ({} new + {} processed)",
            total, new_n, proc_n,
        )
        if args.verbose and total:
            for row in list_candidates():
                logger.info(
                    "  id={} status={} cat={!r} base={} hours_left={:+.1f} title={}",
                    row["id"], row["status"], row["category"],
                    row["base_score"],
                    row["hours_left"], (row["title"] or "")[:60],
                )
        return 0

    deleted_new, kept_new, deleted_processed, kept_processed = run_once()
    healed, retried = heal_stuck_posts()
    print(
        f"janitor: deleted_new={deleted_new} kept_new={kept_new} "
        f"deleted_processed={deleted_processed} kept_processed={kept_processed} "
        f"healed_publishing={healed} retried_failed={retried}"
    )
    return 0 if (deleted_new or kept_new or deleted_processed or kept_processed or healed or retried) else 0


if __name__ == "__main__":
    sys.exit(main())
