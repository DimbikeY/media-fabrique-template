"""Unit tests for scoring.py.

Run with: ``python test_scoring.py`` from the project venv. No DB, no
LLM, no network — pure-function tests. We do not use pytest; this
matches the convention of the existing test_*.py smoke files in the repo.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Make sibling imports work regardless of cwd
sys.path.insert(0, str(Path(__file__).parent))

from scoring import (  # noqa: E402
    CATEGORY_HALF_LIFE_H,
    DEFAULT_HALF_LIFE_H,
    DEFAULT_CATEGORY,
    MIN_THRESHOLD,
    ScoringResult,
    WHITELIST,
    category_was_drift,
    compute_expires_at,
    decay,
    half_life_for,
    normalize_category,
    score_item,
    validate_category,
)


# Tiny assertion helper, zero deps
def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_decay_at_zero_age_returns_base() -> None:
    _assert(decay(7.5, age_h=0.0, half_life_h=6.0) == 7.5,
            f"expected 7.5 at age=0, got {decay(7.5, 0, 6)}")


def test_decay_at_one_half_life_is_half() -> None:
    w = decay(10.0, age_h=24.0, half_life_h=24.0)
    _assert(abs(w - 5.0) < 1e-9, f"expected 5.0, got {w}")


def test_decay_at_two_half_lives_is_quarter() -> None:
    w = decay(10.0, age_h=48.0, half_life_h=24.0)
    _assert(abs(w - 2.5) < 1e-9, f"expected 2.5, got {w}")


def test_decay_negative_age_clamped_to_zero() -> None:
    # Clock skew happens. Should not throw, should equal base.
    w = decay(8.0, age_h=-5.0, half_life_h=6.0)
    _assert(w == 8.0, f"expected 8.0 after clamp, got {w}")


def test_decay_zero_half_life_is_zero() -> None:
    _assert(decay(8.0, age_h=1.0, half_life_h=0) == 0.0,
            "half_life=0 must produce 0 weight")


def test_decay_negative_half_life_is_zero() -> None:
    _assert(decay(8.0, age_h=1.0, half_life_h=-1.0) == 0.0,
            "half_life<0 must produce 0 weight")


def test_decay_zero_base_is_zero() -> None:
    _assert(decay(0.0, age_h=5.0, half_life_h=6.0) == 0.0,
            "base=0 must produce 0 weight")


def test_decay_underflow_below_threshold_becomes_zero() -> None:
    # Very old age → extremely tiny result → we clamp to 0.0
    w = decay(1.0, age_h=10000.0, half_life_h=6.0)
    _assert(w == 0.0, f"expected 0.0 from underflow, got {w}")


def test_decay_unknown_category_uses_default() -> None:
    # Category happens to have half_life_h=36.0 ("other").
    _assert(half_life_for("nonexistent-category") == DEFAULT_HALF_LIFE_H,
            "unknown category must return DEFAULT_HALF_LIFE_H")


def test_half_life_for_all_known_categories() -> None:
    for cat, hl in CATEGORY_HALF_LIFE_H.items():
        _assert(half_life_for(cat) == hl, f"{cat}: {half_life_for(cat)} != {hl}")
        # case-insensitive
        _assert(half_life_for(cat.upper()) == hl, f"{cat.upper()} lookup failed")


def test_half_life_for_empty_string_uses_default() -> None:
    _assert(half_life_for("") == DEFAULT_HALF_LIFE_H, "empty must fall back to default")
    _assert(half_life_for(None) == DEFAULT_HALF_LIFE_H, "None must fall back to default")


def test_compute_expires_at_higher_score_lives_longer() -> None:
    """Politics, 6h half-life. A 9.0 must outlive a 5.0 — by a lot."""
    base_t = datetime(2026, 7, 7, 12, 0, 0)
    politics_hl = CATEGORY_HALF_LIFE_H["politics"]

    exp_9  = compute_expires_at(9.0, base_t, politics_hl)
    exp_5  = compute_expires_at(5.0, base_t, politics_hl)
    exp_1  = compute_expires_at(1.0, base_t, politics_hl)

    _assert(exp_9 > exp_5 > exp_1, f"ordering wrong: {exp_9} {exp_5} {exp_1}")
    _assert((exp_9 - base_t).total_seconds() > (exp_5 - base_t).total_seconds(),
            "9.0 should outlive 5.0 in absolute terms too")


def test_compute_expires_at_known_boundary() -> None:
    """Algebraic check: math.log2(0.5/8.0) = -4 → lifetime = 4 * half_life_h."""
    base_t = datetime(2026, 7, 7, 12, 0, 0)
    hl = 6.0
    # Solve by hand:
    #   weight == MIN_THRESHOLD
    #   8 * 0.5^(t/6) = 0.5
    #   0.5^(t/6) = 1/16
    #   t/6 = 4 → t = 24h
    exp = compute_expires_at(8.0, base_t, hl)
    diff_h = (exp - base_t).total_seconds() / 3600
    _assert(abs(diff_h - 24.0) < 1/60, f"expected 24h, got {diff_h}h")


def test_compute_expires_at_already_below_threshold_graces_one_hour() -> None:
    """When base_score <= min_threshold, expires = fetched + 1h, not now."""
    base_t = datetime(2026, 7, 7, 12, 0, 0)
    exp = compute_expires_at(0.1, base_t, half_life_h=24.0)
    diff_h = (exp - base_t).total_seconds() / 3600
    _assert(abs(diff_h - 1.0) < 1e-6, f"expected 1h grace, got {diff_h}h")


def test_compute_expires_at_zero_score_expires_immediately() -> None:
    base_t = datetime(2026, 7, 7, 12, 0, 0)
    exp = compute_expires_at(0.0, base_t, half_life_h=24.0)
    _assert(exp == base_t, f"base=0 must expire at fetched, got {exp}")


def test_compute_expires_at_rounded_to_minute() -> None:
    """expires_at values should land on minute boundaries for readability."""
    base_t = datetime(2026, 7, 7, 12, 0, 0)
    exp = compute_expires_at(7.3, base_t, half_life_h=6.0)
    _assert(exp.second == 0, f"seconds must be 0, got {exp.second}")
    _assert(exp.microsecond == 0, f"microseconds must be 0, got {exp.microsecond}")


def test_score_item_full_pipeline() -> None:
    """End-to-end: priority + category → ScoringResult with sensible values."""
    fetched = datetime(2026, 7, 7, 9, 0, 0)
    now = datetime(2026, 7, 7, 12, 0, 0)
    res = score_item(priority=8.0, category="tech",
                     fetched_at=fetched, now=now)
    _assert(isinstance(res, ScoringResult), "must return ScoringResult")
    _assert(res.base_score == 8.0, f"base: {res.base_score}")
    _assert(res.category == "tech", f"cat: {res.category}")
    _assert(res.half_life_h == 24.0, f"hl: {res.half_life_h}")
    _assert(res.weight == 8.0, f"weight at t=0 must equal base, got {res.weight}")
    _assert(res.scored_at == now, f"scored_at: {res.scored_at}")
    _assert(res.expires_at > fetched,
            f"expires must be after fetched, got {res.expires_at}")
    # tech / 8.0 / hl=24:
    #   lifetime_h = 24 * log2(0.5/8) = 24 * 4 = 96h = 4 days exactly.
    diff_h = (res.expires_at - fetched).total_seconds() / 3600
    _assert(95 < diff_h < 97, f"tech 8.0 should live ~4 days, got {diff_h}h")


def test_score_item_priority_clamped() -> None:
    """priority > 10 or < 0 must be clamped, not raise."""
    r_hi = score_item(priority=15.0, category="tech",
                      fetched_at=datetime(2026, 7, 7), now=datetime(2026, 7, 7))
    _assert(r_hi.base_score == 10.0, f"hi clamp: {r_hi.base_score}")
    r_lo = score_item(priority=-3.0, category="tech",
                      fetched_at=datetime(2026, 7, 7), now=datetime(2026, 7, 7))
    _assert(r_lo.base_score == 0.0, f"lo clamp: {r_lo.base_score}")


def test_score_item_priority_string_coerced() -> None:
    """LLM sometimes returns strings. Must not crash."""
    r = score_item(priority="7.5", category="tech",
                   fetched_at=datetime(2026, 7, 7), now=datetime(2026, 7, 7))
    _assert(r.base_score == 7.5, f"string coerce: {r.base_score}")


def test_score_item_priority_garbage_returns_zero() -> None:
    r = score_item(priority="not-a-number", category="tech",
                   fetched_at=datetime(2026, 7, 7), now=datetime(2026, 7, 7))
    _assert(r.base_score == 0.0, f"garbage must be 0.0, got {r.base_score}")


def test_score_item_unknown_category_falls_back_to_default_hl() -> None:
    """Sprint 6.7: unknown categories coerce to DEFAULT_CATEGORY, not preserve.

    Pre-Sprint-6.7 this test asserted the LLM-invented lowercase string was
    preserved as-is (for future whitelist extension). Sprint 6.7 closes the
    set explicitly: anything outside WHITELIST becomes ``"other"``. A valid
    rewrite must not fail because of a one-off label — so we coerce, not raise.
    """
    r = score_item(priority=5.0, category="AnimeSuperGenre",
                   fetched_at=datetime(2026, 7, 7), now=datetime(2026, 7, 7))
    _assert(r.category == DEFAULT_CATEGORY,
            f"unknown cat must coerce to default, got: {r.category}")
    _assert(r.half_life_h == DEFAULT_HALF_LIFE_H,
            f"hl must be default, got {r.half_life_h}")


def test_as_db_dict_keys_match_columns() -> None:
    """Catch a typo in the migration column list."""
    r = score_item(priority=5.0, category="tech",
                   fetched_at=datetime(2026, 7, 7, 12, 0, 0),
                   now=datetime(2026, 7, 7, 12, 0, 0))
    d = r.as_db_dict()
    expected = {"base_score", "category", "half_life_h", "weight", "expires_at", "scored_at"}
    _assert(set(d.keys()) == expected,
            f"keys {set(d.keys())} != expected {expected}")


# ---------------------------------------------------------------------------
# Sprint 6.7: closed-set category whitelist
# ---------------------------------------------------------------------------
def test_whitelist_matches_half_life_keys() -> None:
    """WHITELIST must mirror CATEGORY_HALF_LIFE_H keys exactly.

    Drift between the two would mean scoring.py and the persistence layer
    disagree on what's a valid category — silent data corruption. Catch
    it here so anyone adding a half-life row also adds it to the whitelist
    (which they don't, because WHITELIST is derived from CATEGORY_HALF_LIFE_H
    at import time).
    """
    _assert(WHITELIST == frozenset(CATEGORY_HALF_LIFE_H.keys()),
            f"WHITELIST drift: {set(WHITELIST) ^ set(CATEGORY_HALF_LIFE_H.keys())}")


def test_whitelist_has_twelve_entries() -> None:
    """Sanity: this is the design from Sprint 6.7 (12 categories)."""
    _assert(len(WHITELIST) == 12, f"expected 12, got {len(WHITELIST)}")


def test_normalize_category_lowercases_and_strips() -> None:
    _assert(normalize_category(" Tech ") == "tech", "must lowercase + strip")
    _assert(normalize_category("tech") == "tech", "already-clean stays clean")
    _assert(normalize_category(None) == "", "None → empty string")
    _assert(normalize_category("") == "", "empty → empty")


def test_validate_category_passes_through_whitelist() -> None:
    for cat in WHITELIST:
        _assert(validate_category(cat) == cat, f"{cat} should pass")
    # Case-insensitive on input (LLM sometimes emits uppercase).
    _assert(validate_category("TECH") == "tech", "uppercase normalized")
    _assert(validate_category("  Tech  ") == "tech", "padded normalized")


def test_validate_category_coerces_drift_to_default() -> None:
    """Drift → 'other', not None, not the original string.

    A valid rewrite with a one-off category is still a valid rewrite.
    Failing the row would be a regression.
    """
    _assert(validate_category("sport") == DEFAULT_CATEGORY,
            "misspelling must coerce to default")
    _assert(validate_category("anime") == DEFAULT_CATEGORY,
            "removed-from-whitelist label coerces to default")
    _assert(validate_category("gaming") == DEFAULT_CATEGORY,
            "creative label coerces to default")
    _assert(validate_category(None) == DEFAULT_CATEGORY,
            "None coerces to default")
    _assert(validate_category("") == DEFAULT_CATEGORY,
            "empty coerces to default")


def test_category_was_drift_detects_only_invalid_labels() -> None:
    """None / empty are not 'drift' — they're the no-info path."""
    _assert(not category_was_drift(None), "None is not drift")
    _assert(not category_was_drift(""), "empty is not drift")
    _assert(not category_was_drift("tech"), "valid is not drift")
    _assert(not category_was_drift("TECH"), "normalized-valid is not drift")
    _assert(category_was_drift("sport"), "misspelling is drift")
    _assert(category_was_drift("anime"), "removed label is drift")
    _assert(category_was_drift("gaming"), "creative label is drift")


def test_score_item_drift_coerces_to_other() -> None:
    """End-to-end: LLM returns 'sport' (wrong), we persist 'other'.

    We assert two things:
      1. result.category is the WHITELIST key, NOT the LLM string.
      2. half_life_h corresponds to 'other' (36h), not 'sports' (12h).
    """
    r = score_item(priority=5.0, category="sport",
                   fetched_at=datetime(2026, 7, 7), now=datetime(2026, 7, 7))
    _assert(r.category == DEFAULT_CATEGORY,
            f"drift must coerce, got {r.category!r}")
    _assert(r.half_life_h == CATEGORY_HALF_LIFE_H[DEFAULT_CATEGORY],
            f"hl must follow coerced cat, got {r.half_life_h}")
    _assert(r.half_life_h == 36.0, "other → 36h")


def test_score_item_normalizes_known_category() -> None:
    """LLM sometimes capitalizes. validate_category must lowercase."""
    r = score_item(priority=5.0, category="TECH",
                   fetched_at=datetime(2026, 7, 7), now=datetime(2026, 7, 7))
    _assert(r.category == "tech", f"uppercase must normalize, got {r.category!r}")
    _assert(r.half_life_h == 24.0, "tech → 24h")


def test_relative_weighting_realistic_scenario() -> None:
    """Sanity: a 9.0 politics item outranks a 5.0 science item at age=2h."""
    # Both 2 hours old.
    # politics 9.0, hl=6  → 9 * 0.5^(2/6)  = 9 * 0.79 = 7.11
    # science 5.0, hl=72 → 5 * 0.5^(2/72) = 5 * 0.98 = 4.90
    w_pol = decay(9.0, 2.0, CATEGORY_HALF_LIFE_H["politics"])
    w_sci = decay(5.0, 2.0, CATEGORY_HALF_LIFE_H["science"])
    _assert(w_pol > w_sci, f"politics {w_pol} should outrank science {w_sci}")


def test_relative_weighting_opposite_scenario() -> None:
    """Same candidates, 5 days later: science outlives politics."""
    # age = 5 days = 120h
    # politics 9.0, hl=6   → 9 * 0.5^20 = 8.6e-6 ≈ 0
    # science 5.0, hl=72   → 5 * 0.5^(120/72) = 5 * 0.314 = 1.57
    w_pol = decay(9.0, 120.0, CATEGORY_HALF_LIFE_H["politics"])
    w_sci = decay(5.0, 120.0, CATEGORY_HALF_LIFE_H["science"])
    _assert(w_sci > w_pol, f"after 5d science {w_sci} should beat politics {w_pol}")


# ---------------------------------------------------------------------------

def main() -> None:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures: list[tuple[str, BaseException]] = []
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except BaseException as exc:  # noqa: BLE001 — tests should bubble any error
            failures.append((fn.__name__, exc))
            print(f"  FAIL  {fn.__name__}: {exc}")

    total = len(tests)
    failed = len(failures)
    print(f"\n{'-' * 60}")
    print(f"  {total - failed}/{total} passed")
    if failures:
        for name, exc in failures:
            print(f"  - {name}: {type(exc).__name__}: {exc}")
        sys.exit(1)
    print("  ALL GREEN ✓")


if __name__ == "__main__":
    main()
