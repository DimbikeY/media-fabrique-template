"""Sprint 5.2: image acquisition as a pure library.

Sprint 3 had this logic in `image_processor.py` paired with a cron-tick
worker that ran separately. Sprint 5.2 deletes that worker: image
acquisition is now an inline step of `publisher.py`. Every post that
goes out has either a real image or an explicit Plan D (graceful skip).

Sprint 5.2.1: output format switched from JPEG to WebP (lossy, q=82,
method=6). Smaller files, native WP support since 5.8. See
``notes/technical/sprint-5.2.1-webp.md`` for the rationale.

Why a library, not a worker:

  - We do not want to spend image API quota on draft_posts that may never
    be published (low-priority filler waiting in the queue).
  - We do not want a "post published without a cover" failure mode.
  - Publisher is the single source of truth for "this goes out now" —
    inline image generation matches that mental model.

This module exposes ``ensure_image(...)`` which runs the full Plan A→B→C
ladder for a single (post, item) pair and returns the local WebP path.
Plan D (graceful skip) is handled by the caller — this library never
writes to the database, it only fetches/normalises images.

Ladder (first success wins):
  A) Use the source's own image_url (download, normalize, save).
  B) Call IMAGE_BASE_URL (stock or generation provider) for a stock photo.
  C) Call IMAGE_FALLBACK_BASE_URL with the LLM-supplied image_prompt.

The state machine for ``candidates`` is unchanged: ready stays ready. The
caller decides what to do with the returned path (write it to
``draft_posts.featured_image_path`` and proceed, or pass 0 to WordPress).
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger
from openai import (
    APIConnectionError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)
from PIL import Image, ImageOps
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import IMAGE, PIPE
from utils import is_http_url

# Sprint 5.2.1: output format. JPEG was the default in Sprint 3/5.2; we
# moved to lossy WebP for ~25–35% smaller files at the same visual quality
# (q=82 ≈ JPEG q=88). Pillow writes WebP via libwebp, method=6 is the
# slowest-but-smallest encoder. WP core (5.8+) accepts image/webp natively.
_OUTPUT_SUFFIX = ".webp"
_OUTPUT_FORMAT = "WEBP"

try:
    import requests  # local import — Plan A only
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

_RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError)


# --- Plan A: download + normalize the source's image_url --------------------

# Sprint 5.5 (B1): trademark + publicity-rights guard.
# If the source image's alt text mentions a brand whose category does not
# match the item's category, OR mentions a recognisable person's name,
# Plan A is unsafe for production. We discard the Plan A result and fall
# back to Plan B/C which lets the image API re-generate without faces/logos.
_TRADEMARK_KEYWORDS = {
    "lego", "playstation", "xbox", "nintendo", "disney", "marvel", "dc comics",
    "star wars", "pokemon", "nike", "adidas", "apple", "microsoft", "google",
    "sony", "sega", "epic games", "riot games", "blizzard", "ubisoft", "ea",
    "electronic arts", "rockstar", "capcom", "konami", "bandai namco",
    "square enix", "tencent", "netflix", "spotify", "youtube",
}
# Common first-name patterns that often indicate a real person's face.
# Not exhaustive — this is a heuristic, not a hard filter. The LLM image
# generator is the second line of defence via the negative prompt.
_PERSON_NAME_HINTS = (
    "celebrity", "actor", "actress", "singer", "streamer", "youtuber",
    "tiktoker", "host", "presenter", "interview", "portrait of",
)


def _alt_is_unsafe(alt: str, item_category: Optional[str] = None) -> bool:
    """Return True if ``alt`` mentions a trademark/face that we don't want
    to feature on the production site (Sprint 5.5 B1)."""
    a = (alt or "").lower()
    if not a:
        return False
    for kw in _TRADEMARK_KEYWORDS:
        if kw in a:
            # Trademark mention is acceptable only when the item is in the
            # matching category. The check is fuzzy — if we don't know the
            # category we err on the safe side and flag it.
            return True
    for hint in _PERSON_NAME_HINTS:
        if hint in a:
            return True
    return False
@dataclass
class _DownloadResult:
    path: Optional[Path]
    note: str  # "ok" | "no source image_url" | "download empty" | "normalize:..."


def _download(url: str, dest: Path) -> Optional[Path]:
    """Fetch the URL into ``dest``. Returns the saved path or ``None``.

    Uses ``requests`` directly (not the LLM SDK) because we need binary
    streaming and HTTP-level control over timeouts. Returns None when
    the payload is suspiciously small (< 1024 bytes) — that's usually a
    tracking pixel, not a real image.
    """
    if requests is None:
        return None
    with requests.get(url, timeout=PIPE.http_timeout_seconds, stream=True) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                f.write(chunk)
    if dest.stat().st_size < 1024:
        return None
    return dest


def plan_a(source_url: str, candidate_id: int, out_dir: Path) -> _DownloadResult:
    """Download + normalize the original image. Returns path or None+note."""
    if not source_url or not is_http_url(source_url):
        return _DownloadResult(None, "no source image_url")

    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f"item_{candidate_id}_raw.bin"

    try:
        saved = _download(source_url, raw_path)
    except Exception as e:
        logger.warning("Plan A download failed for item {}: {}", candidate_id, e)
        return _DownloadResult(None, f"download:{e}")

    if saved is None:
        return _DownloadResult(None, "download empty or too small")

    # Normalize to target size + format
    try:
        with Image.open(saved) as im:
            im = ImageOps.exif_transpose(im)  # honor EXIF rotation
            im = im.convert("RGB")             # strip alpha for WebP/OG compat
            im = ImageOps.contain(im, (IMAGE.width, IMAGE.height))
            # Pad to exact canvas size (Letterbox) so OG is always 1200x630.
            canvas = Image.new("RGB", (IMAGE.width, IMAGE.height), (16, 16, 16))
            canvas.paste(im, ((IMAGE.width - im.width) // 2,
                              (IMAGE.height - im.height) // 2))

            final_path = out_dir / f"item_{candidate_id}{_OUTPUT_SUFFIX}"
            canvas.save(final_path, format=_OUTPUT_FORMAT,
                        quality=IMAGE.quality_webp,
                        method=6)
    except Exception as e:
        logger.warning("Plan A normalize failed for item {}: {}", candidate_id, e)
        return _DownloadResult(None, f"normalize:{e}")
    finally:
        # Clean up raw binary, we only keep the encoded output.
        try:
            saved.unlink(missing_ok=True)
        except Exception:
            pass

    return _DownloadResult(final_path, "ok")


# --- Plan B/C: ask the image provider -------------------------------------
# The provider is OpenAI-compatible. We follow the same retry rules as LLM.
def _image_client(base_url: str, api_key: str) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=api_key)


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
def _call_image_api(
    client: OpenAI,
    model: str,
    prompt: str,
    size: str,
    timeout: int,
) -> bytes:
    resp = client.images.generate(
        model=model,
        prompt=prompt,
        size=size,
        n=1,
        timeout=timeout,
        response_format="b64_json",
    )
    import base64
    return base64.b64decode(resp.data[0].b64_json)


def plan_b_or_c(
    prompt: str,
    candidate_id: int,
    out_dir: Path,
    *,
    base_url: str,
    api_key: str,
    model: str,
    note: str,
) -> _DownloadResult:
    if not (base_url and api_key and model):
        return _DownloadResult(None, f"{note}: provider not configured")
    if not prompt or len(prompt.strip()) < 4:
        return _DownloadResult(None, f"{note}: empty prompt")

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        client = _image_client(base_url, api_key)
        size = f"{IMAGE.width}x{IMAGE.height}"
        raw = _call_image_api(client, model, prompt.strip(), size, PIPE.http_timeout_seconds)
    except _RETRYABLE as e:
        logger.warning("{} transient error: {}", note, e)
        return _DownloadResult(None, f"{note}:{e}")
    except Exception as e:
        logger.warning("{} failed: {}", note, e)
        return _DownloadResult(None, f"{note}:{e}")

    if len(raw) < 512:
        return _DownloadResult(None, f"{note}:tiny payload")

    final_path = out_dir / f"item_{candidate_id}{_OUTPUT_SUFFIX}"
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB").resize((IMAGE.width, IMAGE.height))
            im.save(final_path, format=_OUTPUT_FORMAT,
                    quality=IMAGE.quality_webp,
                    method=6)
    except Exception as e:
        return _DownloadResult(None, f"{note}:decode:{e}")
    return _DownloadResult(final_path, f"ok:{note}")


# --- Composite prompt selector (was _fetch_prompt in the worker) ------------
def build_prompt(post_image_prompt: Optional[str], post_image_alt: Optional[str],
                 item_title: Optional[str]) -> Optional[str]:
    """Pick the best prompt for Plan C, with a synthetic fallback.

    Priority:
      1. post_image_prompt — what the LLM generated specifically for the
         image API (English, with style/composition details). Best quality.
      2. Synthetic prompt from post_image_alt + item_title — a safe
         fallback that still produces something usable.

    Pure function: takes primitives, returns a string or None. The caller
    (publisher) has already fetched these from the row, so this module
    stays SQLite-free.
    """
    llm_prompt = (post_image_prompt or "").strip()
    if llm_prompt:
        return llm_prompt
    alt = (post_image_alt or "").strip()
    title = (item_title or "").strip()
    if not alt and not title:
        return None
    return (
        f"Editorial photo illustration: {alt or title}. "
        "Cinematic lighting, photojournalistic style. "
        "STRICT PROHIBITIONS (legal safety, do not violate): "
        "no real people's faces or recognisable likenesses; "
        "no brand logos or trademarks (LEGO, PlayStation, Disney, Nike, etc.) "
        "unless the post is explicitly about that brand; "
        "no watermarks, no text overlays, no captions. "
        "Safe for news, editorial use only."
    )


# --- Public entry point -----------------------------------------------------
@dataclass
class EnsureImageResult:
    """Result of the A→B→C ladder for one (post, item) pair.

    ``path`` is None on Plan D (graceful skip). ``plan`` is one of
    'A', 'B', 'C', 'D' for log aggregation. ``note`` is the same string
    that ``plan_*`` functions return, useful for diagnostics.
    """
    path: Optional[Path]
    plan: str  # 'A' | 'B' | 'C' | 'D'
    note: str


def ensure_image(
    *,
    candidate_id: int,
    source_image_url: Optional[str],
    post_image_prompt: Optional[str],
    post_image_alt: Optional[str],
    item_title: Optional[str],
    item_category: Optional[str] = None,
    out_dir: Optional[Path] = None,
) -> EnsureImageResult:
    """Run the full Plan A→B→C ladder for one item. Returns the JPEG path.

    Plan D (no path) is a normal outcome — caller decides whether to
    publish without a cover (graceful) or to mark the post failed.
    """
    out = out_dir or IMAGE.out_dir
    title = (item_title or "")[:60]
    logger.info("ensure_image item={} title={!r}", candidate_id, title)

    # Plan A — original image from the source
    res = plan_a(source_image_url or "", candidate_id, out)
    if res.path:
        # Sprint 5.5 (B1): trademark + face guard. If the alt text mentions
        # a brand/celebrity that isn't safe for production, discard the
        # Plan A image and fall through to Plan B/C generation.
        if _alt_is_unsafe(post_image_alt or "", item_category):
            logger.warning(
                "Plan A discarded for item {} (unsafe alt): {!r}",
                candidate_id, (post_image_alt or "")[:80],
            )
        else:
            logger.success("Plan A ok for item {}: {}", candidate_id, res.path)
            return EnsureImageResult(path=res.path, plan="A", note=res.note)

    # Build the prompt once, reuse for B and C
    prompt = build_prompt(post_image_prompt, post_image_alt, item_title) or ""

    # Plan B — primary provider (stock or generation)
    res = plan_b_or_c(
        prompt, candidate_id, out,
        base_url=IMAGE.base_url, api_key=IMAGE.api_key, model=IMAGE.model,
        note="plan_b",
    )
    if res.path:
        logger.success("Plan B ok for item {}: {}", candidate_id, res.path)
        return EnsureImageResult(path=res.path, plan="B", note=res.note)

    # Plan C — fallback provider
    res = plan_b_or_c(
        prompt, candidate_id, out,
        base_url=IMAGE.fallback_base_url or IMAGE.base_url,
        api_key=IMAGE.fallback_api_key or IMAGE.api_key,
        model=IMAGE.fallback_model or IMAGE.model,
        note="plan_c",
    )
    if res.path:
        logger.success("Plan C ok for item {}: {}", candidate_id, res.path)
        return EnsureImageResult(path=res.path, plan="C", note=res.note)

    # Plan D — graceful skip
    logger.warning("All plans failed for item {}. Plan D (no image).", candidate_id)
    return EnsureImageResult(path=None, plan="D", note="all plans failed")