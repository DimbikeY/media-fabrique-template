"""LLM client: thin OpenAI-compatible wrapper with retries.

We deliberately do NOT touch the database here. rewrite_and_score.py is responsible
for state transitions (new → rewriting → ready / skipped / failed).
This module only does: build prompt → call provider → return parsed JSON
(or raise).
"""
from __future__ import annotations

from collections import namedtuple
from typing import Any, Dict

from loguru import logger
from openai import OpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import LLM, PIPE
from config import _env as _cfg_env

# Re-export under a local name to avoid shadowing builtins.
_env = _cfg_env
from prompts import build_user_prompt, build_tg_user_payload, get_system_prompt, get_tg_system_prompt, parse_llm_json
from models import TGChannelOutput, BlockedOutput

# Errors worth retrying. Auth/quota failures are NOT retried.
from openai import APIConnectionError, APITimeoutError, RateLimitError

_RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError)


RewriteResult = namedtuple("RewriteResult", ["data", "usage", "metrics"])


class LLMClient:
    """Stateless wrapper around an OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.base_url = base_url or LLM.base_url
        self.api_key = api_key or LLM.api_key
        self.model = model or LLM.model
        if not self.base_url or not self.api_key or not self.model:
            raise RuntimeError(
                "LLM client is not configured: set LLM_BASE_URL, LLM_API_KEY, "
                "LLM_MODEL in .env"
            )
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(PIPE.http_retries),
        wait=wait_exponential(
            multiplier=PIPE.http_retry_backoff_seconds,
            min=PIPE.http_retry_backoff_seconds,
            max=10,
        ),
        reraise=True,
    )
    def rewrite(self, article: Dict[str, Any]) -> "RewriteResult":
        """Send one article to the LLM, return parsed JSON dict + usage.

        Raises:
            RuntimeError: on non-retryable provider errors or invalid JSON.
        """
        logger.debug("LLM request: model={} source={}", self.model, article.get("source_name"))
        # Build kwargs, omitting max_tokens entirely when configured to 0
        # so the provider can emit as much reasoning + JSON as it needs.
        # For MiniMax-M3 we also pass reasoning hints via extra_body so the
        # API knows we want thinking enabled but want the final content
        # (not the chain-of-thought) returned in message.content.
        kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": get_system_prompt()},
                {"role": "user", "content": build_user_prompt(article)},
            ],
            "temperature": LLM.temperature,
            "timeout": PIPE.http_timeout_seconds,
            # Some providers ignore this, but those that support it
            # (OpenAI, OpenRouter) will produce cleaner JSON.
            "response_format": {"type": "json_object"},
        }
        if LLM.max_tokens and LLM.max_tokens > 0:
            kwargs["max_tokens"] = LLM.max_tokens
        # MiniMax-M3 (and similar providers) support reasoning parameters
        # via extra_body. Per the MiniMax docs, 'reasoning_split' controls
        # *how* thinking content is returned: when True, CoT comes back in
        # a separate 'reasoning_content' field and 'content' holds only the
        # final answer. 'thinking.type=enabled' turns the feature on.
        # We default to enabled for MiniMax so the model still reasons
        # internally (better text quality per user preference), but the
        # pipeline only ever reads message.content.
        reasoning_hint = _env("LLM_REASONING_HINT", "").strip().lower()
        if reasoning_hint == "minimax":
            kwargs["extra_body"] = {
                "thinking": {"type": "adaptive"},
                "reasoning_split": True,
            }
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except _RETRYABLE as e:
            logger.warning("LLM transient error (will retry): {}", e)
            raise
        except Exception as e:
            # Non-retryable: bad key, quota, schema rejection, etc.
            raise RuntimeError(f"LLM request failed: {e}") from e

        content = (resp.choices[0].message.content or "").strip()
        # Debug: log whether reasoning_content was returned separately. Most
        # providers don't include this; MiniMax-M3 does. We always take the
        # final answer from content, never from reasoning_content.
        reasoning = getattr(resp.choices[0].message, "reasoning_content", None)
        if reasoning:
            logger.debug(
                "LLM returned reasoning_content ({} chars) + content ({} chars)",
                len(reasoning), len(content),
            )
        if not content:
            raise RuntimeError("LLM returned empty content")

        try:
            data = parse_llm_json(content)
        except Exception as e:
            # Log the raw text once so we can debug provider quirks later.
            logger.error("LLM JSON parse failed. Raw (first 500 chars): {}", content[:500])
            raise RuntimeError(f"LLM returned invalid JSON: {e}") from e
        usage = extract_usage(resp)
        # _local metrics: prompt_chars we compute ourselves (provider-
        # independent). response_chars / reasoning_chars come from the
        # provider response shape via extract_usage().
        prompt_chars_local = (
            len(get_system_prompt())
            + len(build_user_prompt(article))
        )
        metrics = {
            **usage,
            "prompt_chars_local": prompt_chars_local,
        }
        return RewriteResult(data=data, usage=usage, metrics=metrics)

    def tg_rewrite(self, post: Dict[str, Any]) -> "RewriteResult":
        """Sprint 6 (channel-prompt): regenerate a TG-channel draft.

        Different from ``rewrite()`` and ``edit_post()``:
          - Uses master_prompt_tg.md system prompt (not master_prompt.md)
          - Different JSON contract: {tg_title, tg_teaser, tg_hashtags} OR blocked
          - Smaller token budget (TG posts are short, ~600 max is plenty)
          - Validates with TGChannelOutput / BlockedOutput (not RewriteOutput)

        Args:
            post: dict-like with keys used by build_tg_user_payload:
                title, content (HTML ok), excerpt, category, tags, priority,
                source_name, source_url, wp_url, note (optional).

        Returns:
            RewriteResult with ``data`` being a parsed TGChannelOutput-like dict
            (or ``{"blocked": True, "reason": ..., ...}`` on block).

        Raises:
            RuntimeError: on non-retryable provider errors or invalid JSON.
        """
        user_payload = build_tg_user_payload(post)
        logger.debug(
            "tg_rewrite: model={} post_title={!r} note={}",
            self.model, (post.get("title") or "")[:80], bool(post.get("note")),
        )
        kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": get_tg_system_prompt()},
                {"role": "user", "content": user_payload},
            ],
            "temperature": LLM.temperature,
            "timeout": PIPE.http_timeout_seconds,
            "response_format": {"type": "json_object"},
        }
        # TG output is small (~100-200 tokens), but MiniMax-M3 with
        # reasoning enabled spends most of the budget on chain-of-thought.
        # We don't cap aggressively because a 4000-token thinking trace
        # is normal for this provider — capping at 600 would cut off
        # reasoning and we'd never see the JSON. Default LLM.max_tokens
        # (often 0 = unlimited) is the right setting; respect it.
        if LLM.max_tokens and LLM.max_tokens > 0:
            kwargs["max_tokens"] = LLM.max_tokens
        # Sprint 6 step 6: skip reasoning hint for short TG-posts.
        # rewrite() (master_prompt.md, longer output) keeps reasoning —
        # it benefits there. Only tg_rewrite() skips it because MiniMax-M3
        # with reasoning_split=True spends the whole token budget on CoT
        # for 3-field outputs and never emits the JSON. Verified via
        # VPS-B round-trip (reasoning_content=1985 chars, content=empty).
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except _RETRYABLE as e:
            logger.warning("tg_rewrite transient error (will retry): {}", e)
            raise
        except Exception as e:
            raise RuntimeError(f"tg_rewrite LLM request failed: {e}") from e

        content = (resp.choices[0].message.content or "").strip()
        # MiniMax-M3 (reasoning_split=True) puts the final answer in
        # reasoning_content and leaves content empty. Fall back to it
        # before declaring failure. We log both lengths for diagnostics.
        reasoning = getattr(resp.choices[0].message, "reasoning_content", None)
        if not content and reasoning:
            logger.debug(
                "tg_rewrite: content empty, falling back to reasoning_content ({} chars)",
                len(reasoning),
            )
            content = reasoning.strip()
        if not content:
            raise RuntimeError("tg_rewrite: LLM returned empty content")

        try:
            data = parse_llm_json(content)
        except Exception as e:
            logger.error(
                "tg_rewrite JSON parse failed. Raw (first 500 chars): {}",
                content[:500],
            )
            raise RuntimeError(f"tg_rewrite: LLM returned invalid JSON: {e}") from e

        # Validate the contract. We do this here (rather than only in the
        # caller) so a malformed payload is caught at the boundary instead of
        # silently writing garbage to tg_dispatch.
        try:
            if data.get("blocked") is True:
                BlockedOutput.model_validate(data)
            else:
                TGChannelOutput.model_validate(data)
        except Exception as e:
            logger.error(
                "tg_rewrite schema validation failed: {}. Data: {}",
                e, data,
            )
            raise RuntimeError(f"tg_rewrite: schema validation failed: {e}") from e

        usage = extract_usage(resp)
        prompt_chars_local = len(get_tg_system_prompt()) + len(user_payload)
        metrics = {**usage, "prompt_chars_local": prompt_chars_local}
        return RewriteResult(data=data, usage=usage, metrics=metrics)

    def edit_post(self, current_post: Dict[str, Any], feedback: str) -> "RewriteResult":
        """Sprint 6m.2: regenerate a draft_post with DD's feedback applied.

        Different from ``rewrite()``: we are NOT rewriting source material.
        The model is given the already-generated post (title, content, meta,
        image prompts) and a free-form feedback note, and must return a new
        version that incorporates the feedback while preserving the overall
        style and JSON contract.

        Uses the same SYSTEM_PROMPT and parse_llm_json() as rewrite() — the
        JSON contract is identical. The user prompt differs: instead of
        ``source_url + article_text`` we pass ``current_post + feedback``.
        The model must keep the canonical structure (Источник, Видео rules)
        intact unless the feedback explicitly asks to change them.
        """
        import json as _json
        if not feedback or not feedback.strip():
            raise ValueError("feedback is required for edit_post()")
        feedback = feedback.strip()
        user_payload = {
            "mode": "edit",
            "current_title": current_post.get("title", ""),
            "current_content": current_post.get("content_html")
                or current_post.get("content", ""),
            "current_excerpt": current_post.get("excerpt", ""),
            "current_slug": current_post.get("slug", ""),
            "current_meta_title": current_post.get("meta_title", ""),
            "current_meta_description": current_post.get("meta_description", ""),
            "current_telegram_teaser": current_post.get("telegram_teaser", ""),
            "current_image_alt": current_post.get("image_alt", ""),
            "current_image_prompt": current_post.get("image_prompt", ""),
            "current_categories_json": current_post.get("categories_json", "[]"),
            "current_tags_json": current_post.get("tags_json", "[]"),
            "feedback": feedback,
        }
        user_text = (
            "Примени правки к существующему посту. Сохрани стиль, структуру и JSON-контракт. "
            "Если правка затрагивает факты — сверяйся с исходником, не выдумывай. "
            "Если фидбек несовместим с темой/правилами — верни текущую версию без изменений "
            "(всё равно валидный JSON по контракту).\n\n"
            f"{_json.dumps(user_payload, ensure_ascii=False)}"
        )
        kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": get_system_prompt()},
                {"role": "user", "content": user_text},
            ],
            "temperature": LLM.temperature,
            "timeout": PIPE.http_timeout_seconds,
            "response_format": {"type": "json_object"},
        }
        if LLM.max_tokens and LLM.max_tokens > 0:
            kwargs["max_tokens"] = LLM.max_tokens
        reasoning_hint = _env("LLM_REASONING_HINT", "").strip().lower()
        if reasoning_hint == "minimax":
            kwargs["extra_body"] = {
                "thinking": {"type": "adaptive"},
                "reasoning_split": True,
            }
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except _RETRYABLE as e:
            logger.warning("LLM transient error on edit (will retry): {}", e)
            raise
        except Exception as e:
            raise RuntimeError(f"LLM edit request failed: {e}") from e

        content = (resp.choices[0].message.content or "").strip()
        if not content:
            raise RuntimeError("LLM returned empty content on edit")
        try:
            data = parse_llm_json(content)
        except Exception as e:
            logger.error("LLM edit JSON parse failed. Raw (first 500): {}", content[:500])
            raise RuntimeError(f"LLM edit returned invalid JSON: {e}") from e
        usage = extract_usage(resp)
        prompt_chars_local = len(get_system_prompt()) + len(user_text)
        metrics = {**usage, "prompt_chars_local": prompt_chars_local}
        return RewriteResult(data=data, usage=usage, metrics=metrics)


def extract_usage(resp) -> dict:
    """Pull token / character accounting out of an OpenAI-compatible response.

    Returns a dict with explicit _provider / _local suffixes so callers
    can tell which numbers came from the provider (tokenizer-dependent)
    and which we computed ourselves (provider-independent).

      prompt_tokens_provider      — provider-reported input tokens
      completion_tokens_provider  — provider-reported output tokens (visible text)
      thinking_tokens_provider    — provider-reported CoT tokens (when split)
      response_chars_local        — len(message.content), our own count
      reasoning_chars_local       — len(message.reasoning_content), our own count
    """
    usage = getattr(resp, "usage", None)

    def _get(obj, key):
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    response_chars_local = None
    reasoning_chars_local = None
    try:
        msg = resp.choices[0].message
        if msg and msg.content:
            response_chars_local = len(msg.content)
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            reasoning_chars_local = len(reasoning)
    except Exception:
        pass

    thinking = (
        _get(usage, "thinking_tokens")
        or _get(usage, "reasoning_tokens")
        or _get(usage, "thoughts_tokens")
    ) if usage else None

    return {
        "prompt_tokens_provider": _get(usage, "prompt_tokens") if usage else None,
        "completion_tokens_provider": _get(usage, "completion_tokens") if usage else None,
        "thinking_tokens_provider": thinking,
        "response_chars_local": response_chars_local,
        "reasoning_chars_local": reasoning_chars_local,
    }