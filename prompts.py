"""Master LLM prompt and JSON contract.

Single source of truth = master_prompt.md (the long v2.1 file in the project
root). We load it at import time so the system prompt exactly matches what is
documented. If a custom override is supplied at runtime, we wrap it as the
"base instruction" inside the user prompt — the JSON contract rules in the
system prompt still apply.
"""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, Optional

# Where the canonical prompt lives. Resolved relative to this file so it
# works from any cwd.
_PROMPT_FILE = Path(__file__).parent / "master_prompt.md"
# Sprint 6 (channel-prompt): separate TG-channel prompt.
_TG_PROMPT_FILE = Path(__file__).parent / "master_prompt_tg.md"

# Strip the surrounding ``` fence from master_prompt.md (it is written as a
# fenced code block for human readability) so the model sees raw prose.
_SYSTEM_PROMPT_RAW = _PROMPT_FILE.read_text(encoding="utf-8") if _PROMPT_FILE.exists() else ""
# TG-channel prompt is shipped as raw prose (no outer fence), so no stripping needed.
# If someone wraps it in ``` later, _strip_code_fence() still works.
_TG_SYSTEM_PROMPT_RAW = _TG_PROMPT_FILE.read_text(encoding="utf-8") if _TG_PROMPT_FILE.exists() else ""

def _strip_code_fence(s: str) -> str:
    """Extract the inner content of a fenced code block if present.

    master_prompt.md is human-readable prose around a ``` fenced block. We
    want only what's inside the fence — not the surrounding "how to use"
    commentary.
    """
    s = s.strip()
    # Find the first opening fence (```lang or just ```) and the next closing one.
    open_idx = s.find("```")
    if open_idx == -1:
        return s
    # Skip past the opening fence line
    after_open = s.find("\n", open_idx)
    if after_open == -1:
        return s
    body_start = after_open + 1
    close_idx = s.rfind("```")
    if close_idx == -1 or close_idx < body_start:
        return s[body_start:]
    return s[body_start:close_idx].strip()


SYSTEM_PROMPT: str = _strip_code_fence(_SYSTEM_PROMPT_RAW)
# Sprint 6: TG-channel system prompt. Computed AFTER _strip_code_fence so
# the function is in scope. The TG prompt file is shipped as raw prose
# (no outer code fence) but we still strip defensively.
TG_SYSTEM_PROMPT: str = _strip_code_fence(_TG_SYSTEM_PROMPT_RAW) if _TG_SYSTEM_PROMPT_RAW else ""


# --- JSON contract: stable field names the rest of the pipeline depends on ---
REQUIRED_FIELDS: list[str] = [
    "title",
    "content",
    "excerpt",
    "slug",
    "image_alt",
    "image_prompt",
    "categories",
    "tags",
    "meta_title",
    "meta_description",
    "telegram_teaser",
]


def build_user_prompt(article: Dict[str, Any], master_override: Optional[str] = None) -> str:
    """Build the user message sent to the LLM.

    Only includes fields the model actually uses per the master_prompt:
      - source_name : for attribution of rumors / insider quotes
      - source_url  : appended as '<p>Источник: ...</p>' at the end
      - video_embed_url : rendered as '<p>Видео: ...</p>' when present
      - article_text : the raw body to rewrite (REQUIRED)

    Fields deliberately dropped: published_at, image_url, original_title,
    summary. They were never referenced by the master prompt and just
    padded the token count.
    """
    base = master_override or (
        "Перескажи статью для нашей аудитории. Сохрани факты. "
        "Добавь ссылку на первоисточник в конце."
    )
    body = (article.get("body") or article.get("summary") or "").strip()
    payload = {
        "source_name": article.get("source_name", ""),
        "source_url": article.get("source_url") or article.get("url"),
        "video_embed_url": article.get("video_embed_url", ""),
        "article_text": body,
    }
    return f"{base}\n\n{json.dumps(payload, ensure_ascii=False)}"


def get_system_prompt() -> str:
    """Return the canonical system prompt (loaded from master_prompt.md)."""
    if not SYSTEM_PROMPT:
        # Fallback if the file is missing — better than crashing.
        return dedent(
            """
            Ты — редактор, пересказывающий новости. Сохраняй только факты из исходника.
            Ответ — строго JSON без ```. Заполни поля: title, slug, excerpt, content,
            image_alt, image_prompt, categories, tags, meta_title, meta_description,
            telegram_teaser. Если тема в стоп-листе (СВО/политика/иноагенты/VPN) —
            верни {"blocked": true, "reason": "violent", "title": "", "content": ""}.
            """
        ).strip()
    return SYSTEM_PROMPT


# --- Sprint 6: TG-channel "<your_channel>" --------------------------------------
# Versioned constant used in tg_dispatch.prompt_version so we can re-batch
# older posts with a new prompt later. Keep this in lockstep with the
# master_prompt_tg.md file header (search for "v1.0" there).
TG_PROMPT_VERSION = "master_prompt_tg.md@v1.0"


def get_tg_system_prompt() -> str:
    """Return the TG-channel system prompt (loaded from master_prompt_tg.md).

    If the file is missing, fall back to an inline minimal prompt so callers
    don't crash. The fallback mirrors the master prompt's contract: only
    {tg_title, tg_teaser, tg_hashtags} or {blocked: true}.
    """
    if not TG_SYSTEM_PROMPT:
        return dedent(
            """
            Ты — редактор Telegram-канала "<your_channel>" (горячие новости).
            На вход — WP-пост (title, content, category, tags, priority) и опциональный note.
            Ответ — строго JSON без ```. Заполни {tg_title, tg_teaser, tg_hashtags}
            (без '#' в начале тегов). Если тема в стоп-листе (СВО/политика/иноагенты) —
            верни {"blocked": true, "reason": "violent", "tg_title": "", "tg_teaser": "", "tg_hashtags": []}.
            """
        ).strip()
    return TG_SYSTEM_PROMPT


def build_tg_user_payload(post: Dict[str, Any]) -> str:
    """Build the user message for tg_rewrite().

    Mirrors master_prompt_tg.md "ВХОДНЫЕ ДАННЫЕ" section. Note `note` may be
    None — we omit the field rather than send an empty string, so the
    model knows there's no editorial hint.
    """
    payload = {
        "wp_title": post.get("title", ""),
        "wp_content": strip_html(post.get("content", "")),
        "wp_excerpt": post.get("excerpt", ""),
        "wp_category": post.get("category", ""),
        "wp_tags": post.get("tags", []) or [],
        "wp_priority": post.get("priority"),
        "source_name": post.get("source_name", ""),
        "source_url": post.get("source_url", ""),
        "wp_url": post.get("wp_url", ""),
    }
    note = post.get("note")
    if note:
        payload["note"] = note
    return json.dumps(payload, ensure_ascii=False)


def strip_html(html: str) -> str:
    """Lightweight HTML-to-text for LLM input. We only feed plain text into
    master_prompt_tg.md — the model is told "WP-контент (300-500 слов,
    plain text)" and HTML tags would inflate token count without adding
    signal.
    """
    import re
    if not html:
        return ""
    # Drop script/style blocks entirely (rare in our WP posts, defensive).
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Replace block-level closers with newlines so paragraphs survive.
    s = re.sub(r"</(p|div|li|h[1-6]|br|tr)\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    # Strip remaining tags.
    s = re.sub(r"<[^>]+>", "", s)
    # Decode common entities.
    s = (
        s.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    # Collapse runs of whitespace per line, keep paragraph breaks.
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    # Drop empty lines but keep at most one blank between paragraphs.
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln:
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(ln)
        blank = False
    return "\n".join(out).strip()


def parse_llm_json(raw: str) -> Dict[str, Any]:
    """Defensive JSON extraction.

    Tries strategies in order:
      0. Strip a leading <think>...</think> block if present.
      1. Strip ``` code fences, then json.loads the whole thing.
      2. Find the first {...} block in the response (greedy on outer braces)
         and parse it. This catches responses where the model wrote prose
         around the JSON.
      3. If both fail, raise — the caller treats it as a non-retryable error.
    """
    if not raw or not raw.strip():
        raise ValueError("LLM returned empty content")
    # Strategy 0: strip chain-of-thought blocks. Some providers (MiniMax-M3
    # in particular) emit reasoning in <think>...</think> before the actual
    # answer. We strip both well-formed blocks AND unclosed ones (model ran
    # out of tokens mid-thought). In the truncated case there is no JSON
    # to recover — Strategy 2 will fail and we'll surface a clean error.
    import re
    s = raw.strip()
    # Closed blocks: drop entirely.
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL)
    # Unclosed block (no closing tag): if a '{' appears within the first
    # ~200 chars after the <think> tag, treat the prose as
    # reasoning and keep the JSON that follows. Otherwise (no JSON yet)
    # drop the <think> and everything that came before it.
    m = re.search(r"<think>", s)
    if m:
        after = s[m.end():]
        if "{" in after[:200]:
            s = after.strip()
        else:
            s = s[:m.start()].strip()
    s = s.strip()
    # Strategy 1: clean fence, whole-string parse.
    s = _strip_code_fence(s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Strategy 2: locate the outermost JSON object inside any wrapper text.
    # Walk every '{' until raw_decode() succeeds — it respects string
    # boundaries, so '{' / '}' inside JSON strings (e.g. CSS like
    # `<style>.foo { margin: 10px; }</style>` or JS in `<pre>` blocks)
    # do not fool the parser. This is strictly better than the previous
    # hand-rolled depth counter, which broke when content_html contained
    # nested brace patterns.
    decoder = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch == "{":
            try:
                obj, _end = decoder.raw_decode(s, i)
                return obj
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON object found in LLM response: {raw[:200]!r}")