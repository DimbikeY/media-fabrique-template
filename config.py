"""Single source of truth for all tunables.

Every script in this project reads from here. To change behavior,
edit this file or the matching env vars in .env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Load .env from the project root (one level above this file's parent is workspace,
# but we keep .env in the project folder for portability).
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

ROOT = Path(__file__).parent.resolve()
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR = ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def _strip_inline_comment(v: str) -> str:
    """Drop ``  # comment`` from the end of a .env value.

    python-dotenv does NOT parse inline comments by design. We want them to
    work in our hand-written .env / .env.example files, so we strip them
    ourselves before the empty-check. dotenv has already trimmed leading
    whitespace from the value, so we look for any remaining ``#`` and treat
    it as a comment delimiter.
    """
    s = v.strip()
    idx = s.find("#")
    if idx == -1:
        return s
    # A '#' right at the start means this was a comment line, but dotenv
    # would have left the key empty — be safe and return empty.
    if idx == 0:
        return ""
    return s[:idx].rstrip()


def _env(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return _strip_inline_comment(raw) if raw.strip() else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse an env var as a permissive boolean (DD 2026-07-20 07:05 MSK fix).

    Accepts "1", "true", "yes", "on" (case-insensitive) as True.
    Anything else (including "0", "false", "no", "off", "", or
    garbage) is False.

    Sprint X bug: `wp_publish_auto_approve` was originally read as
    `_env("WP_PUBLISH_AUTO_APPROVE", "1") == "1"`, which silently
    rejected the canonical .env value `"true"` and disabled
    auto-publish without raising any error.

    Convention (DD 2026-07-20 11:46 MSK): the **documented canonical
    form** in ``.env.example`` and the live ``/opt/<deploy-user>/.env`` is
    the integer string ``"1"`` (true) or ``"0"`` (false) — terser
    for ops diffs and grep-friendly. ``true``/``false``/``yes``/``on``
    are accepted here for backwards compatibility but are NOT the
    documented form. See ``notes/technical/env-vars-registry.md``
    for the canonical list of bool env-vars.
    """
    raw = _env(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_list(name: str) -> List[str]:
    v = os.getenv(name, "")
    return [x.strip() for x in v.split(",") if x.strip()]


# --- LLM ---------------------------------------------------------------------
@dataclass
class LLMConfig:
    base_url: str = _env("LLM_BASE_URL")
    api_key: str = _env("LLM_API_KEY")
    model: str = _env("LLM_MODEL")
    # 0 means "no limit" — we omit the max_tokens field entirely so the
    # provider can emit as much reasoning + JSON as it needs. We default to
    # 0 (unbounded) because the model uses long chain-of-thought and we
    # rarely publish more than a few candidates per hour.
    max_tokens: int = _env_int("LLM_MAX_TOKENS", 0)
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.6"))


# --- Images (Sprint 3) -------------------------------------------------------
@dataclass
class ImageConfig:
    base_url: str = _env("IMAGE_BASE_URL")
    api_key: str = _env("IMAGE_API_KEY")
    model: str = _env("IMAGE_MODEL")
    fallback_base_url: str = _env("IMAGE_FALLBACK_BASE_URL")
    fallback_api_key: str = _env("IMAGE_FALLBACK_API_KEY")
    fallback_model: str = _env("IMAGE_FALLBACK_MODEL")
    out_dir: Path = DATA_DIR / "images"
    # Discover / OG: ~1200x630 is the safe universal size
    width: int = 1200
    height: int = 630
    # Sprint 5.2.1: WebP is the only output format (Plan A and B/C).
    # q=82 ≈ JPEG q=88 visually, method=6 is the slowest-but-smallest
    # libwebp encoder. See notes/technical/sprint-5.2.1-webp.md.
    quality_webp: int = 82


# --- WordPress (Sprint 4) ----------------------------------------------------
@dataclass
class WPConfig:
    base_url: str = _env("WP_BASE_URL").rstrip("/")
    username: str = _env("WP_USERNAME")
    app_password: str = _env("WP_APP_PASSWORD")
    default_status: str = _env("WP_DEFAULT_STATUS", "draft")
    default_categories: List[str] = field(default_factory=lambda: _env_list("WP_DEFAULT_CATEGORIES"))
    # Whitelist of hosts we trust for oEmbed embeds (Sprint 1)
    allowed_embed_hosts: tuple = (
        "youtube.com", "youtu.be",
        "player.vimeo.com", "vimeo.com",
        "rutube.ru",
        "vk.com",
    )


# --- Telegram (Sprint 6.5: observability group + 4 topics) -----------------
# One bot, one private group, four message threads (topics). The bot draft_posts
# to TG_CHAT_ID with the matching TG_THREAD_*; thread_id is what TG calls
# message_thread_id. The chat_id for a supergroup looks like -100<digits>;
# the topic thread id is a separate integer per topic.
@dataclass
class TelegramConfig:
    bot_token: str = _env("TELEGRAM_BOT_TOKEN")
    # Common chat_id of the private group. Empty -> tg_bridge is a no-op
    # (so dev machines and CI smoke tests can run without a real TG bot).
    chat_id: str = _env("TG_CHAT_ID")
    # Per-topic message_thread_id. Set to "" to disable a topic.
    thread_published: str = _env("TG_THREAD_PUBLISHED")
    thread_feedback: str = _env("TG_THREAD_FEEDBACK")
    thread_morning_report: str = _env("TG_THREAD_MORNING_REPORT")
    # (thread_ideas removed 2026-07-20 09:26 MSK — TG #ideas topic deleted
    # to stop a daily placeholder push that nobody was reading.)
    # DD 2026-07-20 09:38 MSK: #published_tg topic (977) — аналог #published,
    # but for telegram-channel posts to @your_channel (vs WP posts which go
    # to #published / TG_THREAD_PUBLISHED=3). When WP publish rolls back
    # because Telegraph API is down, we still send a #published_tg
    # notification that says "TG publish blocked: Telegraph API
    # timed out" so DD sees the cadence instead of silence.
    thread_published_tg: str = _env("TG_THREAD_PUBLISHED_TG")
    # Sprint 6.6: #drafts topic for approve/reject previews of new posts.
    # Empty -> push_draft_preview is a no-op (so dev / CI without the
    # drafts topic doesn't break).
    thread_drafts: str = _env("TG_THREAD_DRAFTS")
    # Sprint 6 (channel-prompt): TG-channel "<your_channel>" validation
    # topic. When DD /approves a WP post, after_wp_approve() pushes the
    # TG-preview here for curation. Empty -> trigger is a no-op.
    # Operator sets TG_THREAD_TG_VALIDATION to the #validation topic's
    # message_thread_id for the admin supergroup.
    thread_tg_validation: str = _env("TG_THREAD_TG_VALIDATION")
    # Anti-spam: aggregate candidates into one push per (window, reason) bucket
    # for the feedback topic so a burst of 30 skipped candidates doesn't
    # blow up the chat. Other topics are 1-message-per-event by design.
    feedback_aggregation_window_s: int = _env_int("TG_FEEDBACK_AGG_WINDOW_S", 300)
    # Morning-report schedule is owned by the cron job, but we keep the
    # tz + hour here for documentation + the morning_report.py fallback
    # that fires if cron was missed.
    morning_report_tz: str = _env("TG_MORNING_REPORT_TZ", "Europe/Moscow")
    # Token-to-USD ratecard. Used by morning_report until we land the
    # model_pricing table (deferred). Defaults are conservative for the
    # MiniMax-M3 provider we use today; override via .env.
    llm_cost_per_1k_prompt_usd: float = float(os.getenv("LLM_COST_PER_1K_PROMPT_USD", "0.0003"))
    llm_cost_per_1k_completion_usd: float = float(os.getenv("LLM_COST_PER_1K_COMPLETION_USD", "0.0012"))
    # HTTP for the bot API: same shape as PIPE so retries feel uniform.
    http_timeout_seconds: int = _env_int("TG_HTTP_TIMEOUT_SECONDS", 15)
    http_retries: int = _env_int("TG_HTTP_RETRIES", 3)
    http_retry_backoff_seconds: float = float(os.getenv("TG_HTTP_RETRY_BACKOFF_SECONDS", "1.5"))
    # Sprint 6 (channel-prompt): public channel where we
    # publish the curated TG-channel drafts. Empty -> publish is a
    # no-op (so dev/CI without the channel doesn't break the pipeline).
    # Operator sets the channel chat_id (numeric, -100 prefix) via env
    # var TG_CHANNEL_ID.
    your_channel_channel_id: str = _env("TG_CHANNEL_ID")
    # Default fallback username is intentionally empty — the operator
    # MUST set TG_CHANNEL_USERNAME to their channel's @handle so the
    # bot can build @mentions and link previews correctly. Leaving it
    # blank here means "unconfigured", not "your_channel".
    your_channel_username: str = _env("TG_CHANNEL_USERNAME", "")


# --- Pipeline ----------------------------------------------------------------
@dataclass
class PipelineConfig:
    db_path: Path = Path(_env("DB_PATH", str(DATA_DIR / "news_memory.db"))).resolve()
    log_level: str = _env("LOG_LEVEL", "INFO")
    run_interval_minutes: int = _env_int("RUN_INTERVAL_MINUTES", 60)
    max_items_per_run: int = _env_int("MAX_ITEMS_PER_RUN", 5)
    # Field length caps for candidates.* columns. Keeps the DB tight and the
    # downstream LLM prompt bounded. Tune via .env without touching code.
    max_summary_chars: int = _env_int("MAX_SUMMARY_CHARS", 600)
    max_body_chars: int = _env_int("MAX_BODY_CHARS", 8000)
    # HTTP
    http_timeout_seconds: int = _env_int("HTTP_TIMEOUT_SECONDS", 20)
    http_retries: int = _env_int("HTTP_RETRIES", 3)
    http_retry_backoff_seconds: float = float(os.getenv("HTTP_RETRY_BACKOFF_SECONDS", "1.5"))
    user_agent: str = _env("HTTP_USER_AGENT", (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15"
    ))
    # Always include a "Source:" link in the post body (compliance + trust)
    always_link_source: bool = True


# --- Pipeline ticks (Sprint 5 lightweight) ----------------------------------
# Single source of truth for cron schedules and per-tick limits.
# Edit values here (or via .env overrides), then re-run `openclaw cron add`
# with the resulting expr — the tick command itself reads PIPE_TICKS so we
# don't have to keep cron expr and CLI flags in sync.
@dataclass
class PipelineTicksConfig:
    # Per-tick batch sizes. Rewriter/publisher never operate on "everything
    # available" — they take a small slice so a single slow tick can't
    # starve other ticks. Fetcher and janitor don't need a limit: fetcher
    # iterates RSS feeds (bounded by feed size), janitor deletes by SQL.
    rewriter_limit: int = _env_int("TICK_REWRITER_LIMIT", 5)
    publisher_limit: int = _env_int("TICK_PUBLISHER_LIMIT", 3)
    fetcher_max: int = _env_int("TICK_FETCHER_MAX", 50)

    # Cron schedules (5-field cron expr, local timezone). Mirrored by the
    # actual cron-job definitions in OpenClaw Gateway — keep them in sync
    # if you change these.
    fetcher_cron: str = _env("TICK_FETCHER_CRON", "*/30 * * * *")
    rewriter_cron: str = _env("TICK_REWRITER_CRON", "*/7 * * * *")
    publisher_cron: str = _env("TICK_PUBLISHER_CRON", "*/5 * * * *")
    janitor_cron: str = _env("TICK_JANITOR_CRON", "0 * * * *")
    # Sprint Y (DD 2026-07-20 22:33 MSK): two new ticks for the three-stage
    # WP/TG/Telegraph pipeline. tick=wp_publish is the renamed tick=publish.
    # tick=generate_for_tg runs every 5 min; tick=publish_tg runs every 10 min.
    generate_for_tg_cron: str = _env("TICK_GENERATE_FOR_TG_CRON", "*/5 * * * *")
    publish_tg_cron: str = _env("TICK_PUBLISH_TG_CRON", "*/10 * * * *")
    # Per-tick limits for the two new ticks. Each call to the LLM costs
    # tokens so generate_for_tg_limit stays modest. publish_tg has no LLM
    # so it can be higher.
    generate_for_tg_limit: int = _env_int("TICK_GENERATE_FOR_TG_LIMIT", 5)
    publish_tg_limit: int = _env_int("TICK_PUBLISH_TG_LIMIT", 5)

    # Janitor v2 (Sprint 5 lightweight): how long a post can stay in
    # status='publishing' before we assume the tick crashed and return
    # it to status='draft'. Normal publish is ~5-10s; 15 min is generous
    # for slow Plan-C image generation. Tunable via .env.
    publishing_stuck_minutes: int = _env_int("JANITOR_PUBLISHING_STUCK_MINUTES", 15)

    # Failed-retry grace: a post in status='failed' is moved back to
    # 'draft' (for the next publisher tick) after this many minutes.
    # We retry only once automatically; further failures stay failed
    # and wait for a human.
    failed_retry_after_minutes: int = _env_int("JANITOR_FAILED_RETRY_MINUTES", 60)

    # Sprint 6.6: when True, publisher.py only picks draft_posts where
    # status='approved' (DD's explicit /up verdict in #drafts). When False,
    # the legacy behaviour returns: any 'draft' is auto-published.
    # Toggle this once you trust the pipeline; default is True because
    # we want a manual review period before going automatic.
    wp_publish_auto_approve: bool = _env_bool("WP_PUBLISH_AUTO_APPROVE", True)
    # Sprint X (DD 2026-07-20 07:09 MSK): hard-gate WP publish on Telegraph
    # availability. When ``TELEGRAPH_REQUIRED_FOR_PUBLISH=1`` (default,
    # canonical form, see ``_env_bool`` docstring), if
    # ``TG_PUBLISH_AUTO_APPROVE=1`` AND Telegraph IV creation fails (e.g.
    # api.telegra.ph unreachable from VPS-B on IPv6, or token revoked),
    # publisher.py rolls back the WP post via client.delete_post() and
    # marks the draft as failed with
    # ``error_reason='telegraph_required_but_unavailable'``. The next tick
    # will retry the same draft (failed→draft transition via janitor).
    # When ``TELEGRAPH_REQUIRED_FOR_PUBLISH=0``, WP publish goes through
    # regardless of TG/Telegraph state — TG publish failures become
    # best-effort. Mirrors the "no IV → no post" rule from
    # Sprint 6d.7 hard-rule.
    telegraph_required_for_publish: bool = _env_bool("TELEGRAPH_REQUIRED_FOR_PUBLISH", True)
    # Sprint Y (DD 2026-07-20 22:33 MSK): telegraph-required becomes redundant
    # in the three-stage pipeline because Telegraph failure no longer rolls
    # back the WP post — tick=publish_tg simply queues the row in
    # tg_dispatch with attempts+=1 and retries on the next tick. We keep
    # the env var for one cycle so older /opt/<deploy-user>/.env files don't break;
    # the code path that honoured it (in publisher.py) has been deleted.
    # (See config.py._env_bool docstring — canonical form 1/0.)
    #
    # TG-side auto-approve. Sprint Y.1 hotfix (DD 2026-07-21 08:05 MSK):
    # canonical env name is TG_PUBLISH_AUTO_APPROVE (matches
    # WP_PUBLISH_AUTO_APPROVE introduced in Sprint X; see gotchas.md
    # gotcha #12 — both rename-bug classes). The previous name
    # TG_AUTOAPPROVE_TG_PUBLISH is gone; .env was already updated on
    # 2026-07-19 21:54 with the new name. Set 0 to keep manual /approve_tg.
    tg_autoapprove_tg_publish: bool = _env_bool("TG_PUBLISH_AUTO_APPROVE", False)
    # Sprint Y: maximum Telegraph attempts before a row is parked as
    # 'telegram_blocked_exhausted' for manual review (DD resets with
    # an UPDATE — see notes/technical/sprint-Y-three-stage.md). Each
    # tick adds 1 on failure; after this cap we stop retrying so we
    # don't burn LLM/Telegraph on a row that may never succeed.
    tg_max_telegraph_attempts: int = _env_int("TG_MAX_TELEGRAPH_ATTEMPTS", 5)


LLM = LLMConfig()
IMAGE = ImageConfig()
WP = WPConfig()
TG = TelegramConfig()
PIPE = PipelineConfig()
PIPE_TICKS = PipelineTicksConfig()
