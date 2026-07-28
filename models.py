"""Pydantic models for the LLM JSON contract.

Two shapes:
  - BlockedOutput:   when the source is on the stop-list (SVO, politics, ...)
  - RewriteOutput:   the normal rewrite response

Use ``parse_llm_payload(dict) -> Union[RewriteOutput, BlockedOutput]`` to
dispatch on the ``blocked`` flag.
"""
from __future__ import annotations

from typing import List, Literal, Union

from pydantic import BaseModel, Field, field_validator

from scoring import WHITELIST, normalize_category, validate_category


# --- Blocked (stop-list) ---------------------------------------------------
class BlockedOutput(BaseModel):
    blocked: Literal[True]
    reason: str = Field(min_length=1, max_length=64)
    title: str = ""
    content: str = ""


# --- Sprint 6: TG-channel "<your_channel>" output ------------------------------
class TGChannelOutput(BaseModel):
    """Successful TG-channel regeneration output (master_prompt_tg.md).

    Separate from RewriteOutput because the TG-channel prompt has a different
    JSON contract (3 fields, not 15+) and different length constraints.
    """
    blocked: Literal[False] = False
    tg_title: str = Field(min_length=1, max_length=80)
    tg_teaser: str = Field(min_length=1, max_length=500)
    tg_hashtags: List[str] = Field(default_factory=list, max_length=10)

    @field_validator("tg_hashtags")
    @classmethod
    def _hashtags_no_hash(cls, v: List[str]) -> List[str]:
        """master_prompt_tg.md rule #7: hashtags WITHOUT leading '#'.
        The wrapper adds '#' before each when assembling the final post.
        Defensive: if model emits '#tag', strip it.
        """
        import re
        out: list[str] = []
        for h in v:
            if not h:
                continue
            t = h.strip()
            if t.startswith("#"):
                t = t[1:]
            # Lowercase + collapse whitespace (defensive against 'Open AI' -> 'openai' style drifts).
            t = re.sub(r"\s+", "", t.lower())
            if t:
                out.append(t)
        return out


# --- Successful rewrite ----------------------------------------------------
class RewriteOutput(BaseModel):
    blocked: Literal[False] = False
    title: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=80)
    excerpt: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    image_alt: str = Field(min_length=1, max_length=200)
    image_prompt: str = Field(min_length=1, max_length=1000)
    categories: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    meta_title: str = Field(min_length=1, max_length=80)
    meta_description: str = Field(min_length=1, max_length=200)
    telegram_teaser: str = Field(min_length=1, max_length=1000)

    # Sprint 5.1: scoring metadata. The LLM emits these in the same
    # JSON as the rewrite — we do a separate scorer LLM call. This keeps
    # cost down and keeps scoring consistent with the rewrite context.
    #
    # Both are optional in the contract so an older / smaller model
    # that can't fill them still produces a usable rewrite; we fall
    # back to defaults (priority=0, category=None) in rewrite_and_score. Newer
    # models should always fill them per master_prompt rules #10/#11.
    priority: float | None = Field(default=None, ge=0.0, le=10.0)
    category: str | None = Field(default=None, max_length=64)

    @field_validator("slug")
    @classmethod
    def _slug_kebab(cls, v: str) -> str:
        # The model is told kebab-case, but enforce ASCII + dashes only.
        import re

        cleaned = re.sub(r"[^a-z0-9-]+", "-", v.strip().lower()).strip("-")
        if not cleaned:
            raise ValueError("slug must contain ASCII letters or digits")
        return cleaned[:60]

    @field_validator("tags")
    @classmethod
    def _strip_blanks(cls, v: List[str]) -> List[str]:
        return [x.strip() for x in v if x and x.strip()]

    @field_validator("category")
    @classmethod
    def _coerce_category(cls, v: "str | None") -> "str | None":
        """Sprint 6.7: closed-set whitelist for the single ``category`` label.

        The LLM sometimes invents new labels (``"sport"`` vs ``"sports"``,
        ``"ai-news"``, ``"gaming"``) or slips in a non-whitelist value. We
        DO NOT raise — we coerce to ``"other"`` so the rewrite still scores.
        Returns the normalized (lowercase, stripped) whitelist key.
        """
        if v is None:
            return None
        return validate_category(v)

    @field_validator("categories", mode="before")
    @classmethod
    def _normalize_categories(cls, v):
        """Sprint 6.7: closed-set whitelist for the ``categories`` list.

        Normalize (lowercase + trim), drop empties, **dedup** while
        preserving order, and filter against ``scoring.WHITELIST``. Items
        not in the whitelist are **dropped** (not coerced) — the LLM is
        told to pick 1–2 from the whitelist, so a third party label
        silently gone is safer than inventing a fake WP term.

        The dedup happens AFTER the whitelist filter so we don't end up
        with ``["tech", "Tech"]`` if the model was sloppy.
        """
        if not isinstance(v, list):
            return v
        seen: set[str] = set()
        out: List[str] = []
        for raw in v:
            if not isinstance(raw, str):
                continue
            key = normalize_category(raw)
            if not key:
                continue
            if key not in WHITELIST:
                # Drop non-whitelist entries silently — the prompt told
                # the model to use ONLY these, so an extra label here
                # is the model being creative. We log a soft warning
                # below.
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        if not out:
            # Never publish a post with zero categories — fall back to
            # "other" so the WP lookup has at least one slug to try.
            return ["other"]
        # Sprint 6.7 sanity: cap at 2 categories per master_prompt rule #7.
        # LLM is told "1–2 наиболее точных"; if it returns 5, take the first
        # two (they're the model's strongest picks).
        return out[:2]


LLMResult = Union[RewriteOutput, BlockedOutput]


def parse_llm_payload(data: dict) -> LLMResult:
    """Dispatch on the ``blocked`` flag and return the right model."""
    if data.get("blocked") is True:
        return BlockedOutput.model_validate(data)
    return RewriteOutput.model_validate(data)