"""Sprint 5.1: scoring + aging.

Single-pass scoring: LLM returns `priority` (0..10) and `category` in the
same JSON as the rewrite. We translate those into:

  - ``base_score``  — LLM's raw verdict, stored unchanged.
  - ``category``    — drives the half-life.
  - ``half_life_h`` — hours after which weight halves.
  - ``weight``      — `base_score * decay(age)`. On insert = `base_score`.
  - ``expires_at``  — when weight drops below MIN_THRESHOLD.

Everything here is *pure functions of declared inputs*. No DB calls, no
LLM, no logger. Easy to test, easy to reason about.

Why half-life and not linear decay?

  Half-life maps cleanly to "when does this stop being relevant?". Linear
  decay forces you to pick a cliff (24h? 48h?) and a slope. With half-life
  you pick *one* knob — how many hours until the news loses half its
  pull — and that single number reads naturally across all categories.

Why write ``expires_at`` instead of computing it on every read?

  Pre-computation means the Janitor can use a single, idempotent SQL
  statement (`WHERE expires_at < now`) without joining anything or
  scanning category tables. It also means we freeze the contract with
  the LLM at scoring time — if the model over-prizes something, the
  item still gets a generous but finite lifetime, instead of living
  forever because nobody re-evaluated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


# --- Category → half-life (hours) -------------------------------------------
# Sprint 6.7: this dict is the SINGLE SOURCE OF TRUTH for the closed-set
# category whitelist. The same keys are used as:
#   1) WP term slugs (master_prompt rule #7 → draft_posts.categories_json)
#   2) half-life labels (master_prompt rule #11 → candidates.category)
#   3) Pydantic Literal validation in models.RewriteOutput
#
# LLM MUST return only these. Anything else is coerced to "other" so the
# downstream scoring + WP lookup stay consistent.
#
# Tuned by intuition, not from data. Adjust one value here and everything
# downstream (decay, expires_at, ordering, WP slug resolution) re-derives
# automatically.
#
# Rule of thumb: hot-topic categories (politics, sports, entertainment)
# live for hours. Slow-burn categories (science, nature) live for days.
# Tech and business sit in the middle — news cycles are ~1 day.
CATEGORY_HALF_LIFE_H: dict[str, float] = {
    "politics":      6.0,
    "sports":        12.0,
    "ai":            12.0,   # Sprint 5.5 (B2): AI-news halves every 12h
    "entertainment": 18.0,
    "business":      24.0,
    "tech":          24.0,
    "vibe-coding":   24.0,   # Sprint 5.5 (B2): coding-tools evolve monthly
    "health":        36.0,
    "other":         36.0,
    "culture":       48.0,
    "science":       72.0,
    "nature":        168.0,   # one week
}

# Closed-set whitelist exposed as a frozenset for O(1) membership tests
# (Pydantic Literal validation, scoring drift warnings, smoke tests).
# Always derived from CATEGORY_HALF_LIFE_H so they can never drift apart.
WHITELIST: frozenset[str] = frozenset(CATEGORY_HALF_LIFE_H.keys())

DEFAULT_HALF_LIFE_H = CATEGORY_HALF_LIFE_H["other"]
DEFAULT_CATEGORY = "other"


# --- Decision thresholds ----------------------------------------------------
# Below this weight, janitor deletes the item.
# 0.5 was chosen because at the half-life mark weight == base_score / 2,
# so MIN_THRESHOLD = 0.5 means "give it one more half-life after publication"
# for a baseline 1.0 score, and ~3-4 half-lives for a 9.0 score.
MIN_THRESHOLD: float = 0.5


def normalize_category(category: Optional[str]) -> str:
    """Lowercase + strip a category label. Empty / None → "".

    Pure normalization, no whitelist check. Use this for inputs you trust
    (your own code) and use ``validate_category`` for LLM output.
    """
    if not category:
        return ""
    return category.strip().lower()


def validate_category(category: Optional[str]) -> str:
    """Normalize and coerce to whitelist. Returns DEFAULT_CATEGORY on drift.

    Sprint 6.7: this is the gatekeeper for ``candidates.category``. The LLM
    may invent a new label (or misspell one); we DO NOT raise — we coerce
    to ``DEFAULT_CATEGORY`` so the row remains scorable. The caller
    (``score_item``) additionally logs a warning so drift is visible.

    Why coerce and not raise? A valid rewrite with a one-off category is
    still a valid rewrite. Failing the whole job because the model said
    ``"sport"`` instead of ``"sports"`` would be a regression — half-life
    lookup must stay graceful (Sprint 5.1 contract).
    """
    key = normalize_category(category)
    if key in WHITELIST:
        return key
    return DEFAULT_CATEGORY


def category_was_drift(category: Optional[str]) -> bool:
    """True if the LLM-emitted category is NOT in the whitelist.

    Drift = normalization changed the value OR the value was not in WHITELIST.
    Used by ``score_item`` to log a one-liner warning without spamming
    (caller is expected to log once per rewrite, not per unknown label).
    """
    if not category:
        return False  # missing category is not drift; it's the default path
    key = normalize_category(category)
    return key not in WHITELIST


def half_life_for(category: Optional[str]) -> float:
    """Look up the half-life in hours for a category string from the LLM.

    Unknown / missing categories get ``DEFAULT_HALF_LIFE_H``. We do NOT
    raise — the LLM may invent new category labels and we don't want a
    valid rewrite to fail because of a one-off string.
    """
    key = validate_category(category)
    return CATEGORY_HALF_LIFE_H[key]


def decay(base_score: float, age_h: float, half_life_h: float) -> float:
    """Compute current weight from base score, age, and half-life.

    Formula:  w = base_score * 0.5 ^ (age_h / half_life_h)

    Edge cases:
    - negative age (clock skew, future timestamps): clamp to 0, return
      base_score unchanged.
    - half_life_h <= 0: degenerate input; return 0 (item is dead).
    - negative base_score: ignore sign, weight is non-negative.

    Returns 0.0 when the result is effectively zero (underflow) so the
    caller can treat it as "dead".
    """
    if age_h < 0:
        age_h = 0.0
    if half_life_h <= 0:
        return 0.0
    base = abs(float(base_score))
    if base == 0:
        return 0.0
    w = base * (0.5 ** (age_h / half_life_h))
    # Underflow guard: anything under 1e-12 is effectively 0.
    return 0.0 if w < 1e-12 else w


def compute_expires_at(
    base_score: float,
    fetched_at: datetime,
    half_life_h: float,
    min_threshold: float = MIN_THRESHOLD,
) -> datetime:
    """When the item's weight drops below ``min_threshold``.

    Solves the equation:
        base_score * 0.5 ^ (t_h / half_life_h) = min_threshold
                                  ↓
        t_h = half_life_h * log2(min_threshold / base_score)

    Since min_threshold < base_score (otherwise the item is already
    dead), the logarithm is negative and t_h is positive. Good.

    Edge cases:
    - base_score <= 0 → item is worthless, expires immediately.
      Janitor will pick it up on its next tick.
    - base_score <= min_threshold → item is *already* below threshold at
      fetched_at. We give it 1 hour of grace so the LLM has time to
      "really" mark it bad on a retry, rather than letting janitor snipe
      before rewriter can claim it.

    We return a naive UTC datetime; the DB stores it via ``datetime('now')``
    arithmetic. If the caller wants a tz-aware value, format it before
    passing in.
    """
    base = abs(float(base_score))
    if base <= 0:
        return fetched_at
    if base <= min_threshold:
        return fetched_at + timedelta(hours=1)

    lifetime_h = half_life_h * math.log(min_threshold / base, 0.5)
    # Round up to the next whole minute so expires_at values are easy
    # to eyeball in the DB ("did this thing get a weird 6.7 minutes?").
    lifetime_td = timedelta(hours=float(lifetime_h))
    rounded = timedelta(seconds=math.ceil(lifetime_td.total_seconds() / 60) * 60)
    return fetched_at + rounded


@dataclass(frozen=True)
class ScoringResult:
    """Bundle of values to persist onto candidates after a successful rewrite."""
    base_score: float
    category: str
    half_life_h: float
    weight: float          # = base_score at insert time; refactor later if needed
    expires_at: datetime
    scored_at: datetime

    def as_db_dict(self) -> dict[str, object]:
        """Column → value mapping suitable for ``UPDATE candidates SET ...``."""
        return {
            "base_score":   self.base_score,
            "category":     self.category,
            "half_life_h":  self.half_life_h,
            "weight":       self.weight,
            "expires_at":   self.expires_at.strftime("%Y-%m-%d %H:%M:%S"),
            "scored_at":    self.scored_at.strftime("%Y-%m-%d %H:%M:%S"),
        }


def score_item(
    *,
    priority: float,
    category: Optional[str],
    fetched_at: datetime,
    min_threshold: float = MIN_THRESHOLD,
    now: Optional[datetime] = None,
) -> ScoringResult:
    """One-call scoring: returns everything rewrite_and_score needs to persist.

    The LLM only knows ``priority`` (0..10) and ``category``. We translate
    that into the full row that gets written to ``candidates``.

    Sprint 6.7: ``category`` is run through ``validate_category`` so the
    persisted value is always one of the WHITELIST keys. Anything else is
    coerced to ``DEFAULT_CATEGORY`` (``"other"``) and a warning is logged
    once via ``loguru`` so the operator can spot prompt drift.

    `now` is injectable so tests can pin it; defaults to ``datetime.utcnow()``
    (naive UTC, same convention as the DB's ``datetime('now')``).
    """
    base = _clamp_priority(priority)
    cat = validate_category(category)
    if category_was_drift(category):
        # Lazy import so scoring.py stays free of side-effects at import time
        # (the file is imported from many places, including pure-function
        # tests in test_scoring.py).
        try:
            from loguru import logger
            logger.warning(
                "score_item: LLM category {!r} not in WHITELIST, coerced to {!r}",
                category, cat,
            )
        except Exception:
            # If loguru is unavailable we silently swallow — drift reporting
            # is observability, not correctness.
            pass
    hl = CATEGORY_HALF_LIFE_H[cat]
    when = (now or datetime.utcnow()).replace(microsecond=0)
    fetched = fetched_at.replace(microsecond=0) if fetched_at else when
    return ScoringResult(
        base_score=base,
        category=cat,
        half_life_h=hl,
        weight=base,                       # weight == base_score at t=0
        expires_at=compute_expires_at(base, fetched, hl, min_threshold),
        scored_at=when,
    )


def _clamp_priority(priority: object) -> float:
    """Defensive: LLM may return a string, a float, or something out of range."""
    try:
        v = float(priority)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if v < 0:
        return 0.0
    if v > 10:
        return 10.0
    return v
