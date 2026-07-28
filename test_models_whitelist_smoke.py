"""Sprint 6.7 smoke: Pydantic validators on RewriteOutput enforce the
closed-set category whitelist at the JSON-parsing boundary.

Why this is separate from test_rewrite_and_score_smoke.py:
  - test_rewrite_and_score_smoke.py is end-to-end: it goes through the
    full state machine with a MockLLMClient. Adding a fourth scenario
    there would mix orchestration tests with schema-validation tests.
  - This file is a pure-Pydantic test: we feed a dict directly into
    RewriteOutput.model_validate() and assert the validators behave.

Run from project root with the venv:
    .venv/bin/python test_models_whitelist_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).parent
sys.path.insert(0, str(PROJECT))

from models import RewriteOutput  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from scoring import WHITELIST  # noqa: E402


# --- Tiny assertion helper, zero deps -----------------------------------
def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --- Base valid payload (all required fields filled) ---------------------
_BASE_PAYLOAD = {
    "title": "OPTIC Texas three-peat",
    "slug": "optic-texas-three-peat",
    "excerpt": "Short excerpt.",
    "content": "<p>Body.</p>",
    "image_alt": "Team on stage",
    "image_prompt": "Esports stage with neon lights, no text",
    "meta_title": "OPTIC Texas",
    "meta_description": "OPTIC Texas wins third championship.",
    "telegram_teaser": "OPTIC wins again. #esports",
    "priority": 7.5,
    "category": "tech",
    "categories": ["tech", "sports"],
    "tags": ["esports", "cod"],
}


def _payload(**overrides):
    """Return a fresh copy of _BASE_PAYLOAD with overrides applied."""
    p = dict(_BASE_PAYLOAD)
    p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# category (single) — Sprint 6.7 contract: coerce to whitelist
# ---------------------------------------------------------------------------
def test_category_passes_through_whitelist() -> None:
    for cat in WHITELIST:
        out = RewriteOutput.model_validate(_payload(category=cat))
        _assert(out.category == cat, f"{cat} should pass, got {out.category!r}")


def test_category_coerces_uppercase_to_lowercase() -> None:
    out = RewriteOutput.model_validate(_payload(category="TECH"))
    _assert(out.category == "tech", f"uppercase must lowercase, got {out.category!r}")


def test_category_coerces_padded_to_clean() -> None:
    out = RewriteOutput.model_validate(_payload(category="  Tech  "))
    _assert(out.category == "tech", f"padded must trim, got {out.category!r}")


def test_category_coerces_drift_to_other() -> None:
    """Single-value drift: 'sport' (misspelled), 'gaming' (creative)
    coerce to 'other' rather than failing the rewrite."""
    for drift in ("sport", "gaming", "anime", "Economics", "FOOD"):
        out = RewriteOutput.model_validate(_payload(category=drift))
        _assert(out.category == "other",
                f"{drift!r} must coerce to 'other', got {out.category!r}")


def test_category_none_passes_through_as_none() -> None:
    """Optional field: None means "model didn't fill it", not "drift".
    score_item has its own default for None."""
    out = RewriteOutput.model_validate(_payload(category=None))
    _assert(out.category is None, f"None must pass, got {out.category!r}")


# ---------------------------------------------------------------------------
# categories (list) — Sprint 6.7 contract: filter to whitelist + dedup + cap
# ---------------------------------------------------------------------------
def test_categories_filters_non_whitelist() -> None:
    """Non-whitelist entries are dropped silently, not coerced.
    The LLM is told to use ONLY these; a third-party label is silently
    removed rather than coerced to 'other' (which would be wrong — the
    list field is for WP slugs, and inventing a WP term would 404)."""
    out = RewriteOutput.model_validate(
        _payload(categories=["tech", "gaming", "anime"])
    )
    _assert(out.categories == ["tech"],
            f"non-whitelist must be dropped, got {out.categories!r}")


def test_categories_lowercases_and_trims() -> None:
    out = RewriteOutput.model_validate(
        _payload(categories=["  Tech ", "SPORTS"])
    )
    _assert(out.categories == ["tech", "sports"],
            f"must lowercase+trim, got {out.categories!r}")


def test_categories_dedups_preserving_order() -> None:
    """LLM sometimes repeats labels (especially after a long CoT).
    Dedup by insertion order — the first occurrence wins."""
    out = RewriteOutput.model_validate(
        _payload(categories=["tech", "Tech", "TECH", "sports", "tech"])
    )
    _assert(out.categories == ["tech", "sports"],
            f"must dedup by first-occurrence, got {out.categories!r}")


def test_categories_caps_at_two() -> None:
    """master_prompt rule #7: pick 1–2 categories. If the LLM returns 5,
    we silently keep the first two (its strongest picks)."""
    out = RewriteOutput.model_validate(
        _payload(categories=["tech", "sports", "ai", "games", "other"])
    )
    _assert(out.categories == ["tech", "sports"],
            f"must cap at 2, got {out.categories!r}")


def test_categories_falls_back_to_other_on_empty() -> None:
    """A post with zero valid categories would publish without any WP
    term — visible bug. We fall back to ['other'] so the lookup has at
    least one slug to try."""
    out = RewriteOutput.model_validate(_payload(categories=[]))
    _assert(out.categories == ["other"],
            f"empty list must fall back to ['other'], got {out.categories!r}")


def test_categories_falls_back_to_other_when_all_dropped() -> None:
    """All labels non-whitelist → all dropped → fall back to ['other']."""
    out = RewriteOutput.model_validate(
        _payload(categories=["gaming", "anime", "food"])
    )
    _assert(out.categories == ["other"],
            f"all-dropped must fall back, got {out.categories!r}")


def test_categories_ignores_non_string_entries() -> None:
    """Defensive: LLM might slip a number or null into the list."""
    out = RewriteOutput.model_validate(
        _payload(categories=["tech", 42, None, "sports"])
    )
    _assert(out.categories == ["tech", "sports"],
            f"non-string must be skipped, got {out.categories!r}")


# ---------------------------------------------------------------------------
# Combined: contract shape for the rewrite path
# ---------------------------------------------------------------------------
def test_round_trip_dict_through_rewrite_output() -> None:
    """Realistic LLM payload: a bit messy, with drift + duplicates.
    RewriteOutput must produce a clean, scorable, publishable row.

    Notes on the expected outcome:
      - category="Sport" is drift (whitelist has "sports", not "sport"),
        so it coerces to "other".
      - categories list: "Tech" normalizes to "tech" (whitelist),
        "sport" is dropped (drift), "tech" dedup-skipped, "AI" passes
        whitelist. Result: ["tech", "ai"] — both valid WP slugs, neither
        invented by drift coercion.
    """
    raw = _payload(
        category="Sport",                              # drift + uppercase
        categories=["  Tech ", "sport", "tech", "AI"], # mix valid + drift + dup
    )
    out = RewriteOutput.model_validate(raw)
    _assert(out.category == "other",
            f"single-value drift must coerce, got {out.category!r}")
    _assert(out.categories == ["tech", "ai"],
            f"list must filter+dedup+cap, got {out.categories!r}")
    # The post is publishable: a valid category for scoring, a valid
    # list for WP lookup. No Pydantic errors, no exceptions.


# ---------------------------------------------------------------------------
def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except (AssertionError, ValidationError) as exc:
            failures.append((fn.__name__, exc))
            print(f"  FAIL  {fn.__name__}: {type(exc).__name__}: {exc}")

    total = len(tests)
    failed = len(failures)
    print(f"\n{'-' * 60}")
    print(f"  {total - failed}/{total} passed")
    if failures:
        for name, exc in failures:
            print(f"  - {name}: {type(exc).__name__}: {exc}")
        sys.exit(1)
    print("  ALL GREEN ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())