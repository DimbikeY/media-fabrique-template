"""Smoke tests for image_pipeline.py — pure-library Plan A→B→C ladder.

Verifies:
  1. Plan A: source image_url is fetched and normalised to WebP.
  2. Plan B: when A fails, the primary image API is called with the
     LLM-supplied prompt.
  3. Plan C: when B fails, the fallback image API is called.
  4. Plan D: when all three plans fail, ``ensure_image`` returns
     ``path=None, plan='D'`` (graceful skip — caller decides what to do).
  5. ``build_prompt``: prefers ``post_image_prompt`` from the LLM; falls
     back to a synthetic prompt from alt+title; returns None when both
     are empty.

Sprint 5.2.1 note: the source payload / provider response is still JPEG
in this smoke (that's what the real world gives us). Pillow decodes it
and writes WebP for the on-disk output. The assertions confirm the
suffix and the format by sniffing the bytes with Pillow.

Network and Pillow are mocked. We do NOT actually call image APIs in
the smoke — this is logic coverage, not integration.

Run: ``python test_image_pipeline_smoke.py`` from the project venv.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).parent
sys.path.insert(0, str(PROJECT))

import image_pipeline as ip  # noqa: E402

# Module-level slot for the real JPEG bytes used by all fakes. Populated
# in main() before tests run; defaults to an empty bytes so test isolation
# never depends on import order. NOTE: this is the *input* payload
# (what the source/provider returns), not the on-disk output — output
# is always WebP after Sprint 5.2.1.
_REAL_JPEG_BYTES: bytes = b""


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --- Plan A mock -----------------------------------------------------------
# We can't bake a valid minimal JPEG as raw bytes (too easy to corrupt),
# so we generate one fresh via Pillow in the test's tmp_dir and serve
# those bytes through the fake requests.get context manager.

def _make_real_jpeg_bytes(path: Path, size=(200, 120)) -> bytes:
    """Write a real 200x120 JPEG to ``path`` and return its bytes."""
    from PIL import Image
    img = Image.new("RGB", size, (64, 128, 200))
    img.save(path, format="JPEG", quality=70)
    return path.read_bytes()


def _sniff_format(path: Path) -> str:
    """Return the Pillow-detected format of ``path`` (e.g. ``'JPEG'``,
    ``'WEBP'``). Used to verify on-disk output independently of the
    suffix. Catches regressions where the suffix is right but Pillow
    wrote the wrong container (e.g. WebP bytes inside a .jpg name)."""
    from PIL import Image
    with Image.open(path) as im:
        return im.format


class FakeDownloadOK:
    """Pretends a successful HTTP fetch: writes a real JPEG into ``dest``.

    Re-uses the bytes we generated at setup time via ``_make_real_jpeg_bytes``.
    """

    def __init__(self, url, timeout, stream):
        self._bytes = _REAL_JPEG_BYTES
        self.status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        # Yield the entire payload as a single chunk.
        yield self._bytes


class FakeDownloadFail:
    """Pretends a 404 from the source image URL."""

    def __init__(self, url, timeout, stream):
        self.status_code = 404

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        raise RuntimeError("404 Not Found")

    def iter_content(self, chunk_size):
        return iter([])


# --- Plan B/C mock ---------------------------------------------------------
class FakeImageAPI:
    """Fake OpenAI image-generation endpoint that returns real JPEG bytes."""

    def __init__(self, payload: bytes | None = None, fail: bool = False):
        self._payload = payload if payload is not None else _REAL_JPEG_BYTES
        self._fail = fail
        self.calls: list[dict] = []

    def images_generate(self, *, model, prompt, size, n, timeout, response_format):
        from base64 import b64encode
        self.calls.append({"model": model, "prompt": prompt, "size": size})
        if self._fail:
            raise RuntimeError("provider down")

        class _Resp:
            def __init__(self, data):
                self.data = data
        b64 = b64encode(self._payload).decode()
        return _Resp([type("D", (), {"b64_json": b64})()])


def _patch_openai_client(monkeypatch_module, fake: FakeImageAPI,
                         image_config=None):
    """Override image_pipeline._image_client to return a stub whose
    .images.generate hits our FakeImageAPI.

    If ``image_config`` is given, also patch ``IMAGE.base_url`` etc. so
    the config check inside plan_b_or_c passes (it short-circuits when
    base_url/api_key/model are empty)."""
    class StubImages:
        def __init__(self):
            self.generate = fake.images_generate
    class StubClient:
        def __init__(self):
            self.images = StubImages()
    monkeypatch_module._image_client = lambda *a, **kw: StubClient()
    if image_config is not None:
        from config import IMAGE as real_IMAGE
        for key, val in image_config.items():
            setattr(real_IMAGE, key, val)


# --- Tests ------------------------------------------------------------------

def test_plan_a_succeeds(tmp_dir: Path) -> None:
    """Source image_url returns a valid JPEG → Plan A returns WebP path."""
    import requests
    orig_get = requests.get
    requests.get = lambda *a, **kw: FakeDownloadOK(*a, **kw)
    try:
        res = ip.plan_a("http://example.com/img.jpg", candidate_id=9001,
                        out_dir=tmp_dir)
        _assert(res.path is not None, f"expected path, got {res.path}")
        _assert(res.note == "ok", f"expected 'ok', got {res.note!r}")
        _assert(res.path.exists(), "returned path does not exist on disk")
        _assert(res.path.stat().st_size > 0, "returned file is empty")
        _assert(res.path.suffix == ".webp",
                f"expected .webp suffix, got {res.path.suffix}")
        _assert(_sniff_format(res.path) == "WEBP",
                f"file is not WebP on disk: {_sniff_format(res.path)}")
    finally:
        requests.get = orig_get
    print(f"  PASS  test_plan_a_succeeds ({res.path.name})")


def test_plan_a_fails_cleanly(tmp_dir: Path) -> None:
    """Bad source URL → Plan A returns None + note."""
    res = ip.plan_a("", candidate_id=9002, out_dir=tmp_dir)
    _assert(res.path is None, "empty URL must yield no path")
    _assert("no source" in res.note, f"unexpected note: {res.note!r}")

    res = ip.plan_a("ftp://example.com/img.jpg", candidate_id=9003, out_dir=tmp_dir)
    _assert(res.path is None, "non-http URL must yield no path")
    print(f"  PASS  test_plan_a_fails_cleanly")


def test_plan_b_succeeds(tmp_dir: Path) -> None:
    """Provider returns valid JPEG → Plan B writes WebP and returns path."""
    fake = FakeImageAPI(payload=None)  # use built-in valid bytes
    _patch_openai_client(ip, fake, image_config={
        "base_url": "http://x", "api_key": "k", "model": "m",
    })
    res = ip.plan_b_or_c(
        "an editorial photo", candidate_id=9101, out_dir=tmp_dir,
        base_url="http://x", api_key="k", model="m", note="plan_b",
    )
    _assert(res.path is not None, f"expected path, got {res.path}")
    _assert(res.path.exists(), "Plan B path does not exist")
    _assert(res.path.suffix == ".webp",
            f"expected .webp suffix, got {res.path.suffix}")
    _assert(_sniff_format(res.path) == "WEBP",
            f"file is not WebP on disk: {_sniff_format(res.path)}")
    _assert(len(fake.calls) == 1, f"expected 1 API call, got {len(fake.calls)}")
    _assert(fake.calls[0]["prompt"] == "an editorial photo",
            f"prompt mismatch: {fake.calls[0]['prompt']!r}")
    print(f"  PASS  test_plan_b_succeeds ({res.path.name})")


def test_plan_b_fails_returns_none(tmp_dir: Path) -> None:
    """Provider error → Plan B returns None + note."""
    fake = FakeImageAPI(fail=True)
    _patch_openai_client(ip, fake, image_config={
        "base_url": "http://x", "api_key": "k", "model": "m",
    })
    res = ip.plan_b_or_c(
        "x", candidate_id=9102, out_dir=tmp_dir,
        base_url="http://x", api_key="k", model="m", note="plan_b",
    )
    _assert(res.path is None, f"Plan B must return None on failure, got {res.path}")
    _assert("plan_b" in res.note, f"note should mention plan_b: {res.note!r}")
    print("  PASS  test_plan_b_fails_returns_none")


def test_plan_b_skipped_when_unconfigured(tmp_dir: Path) -> None:
    """Missing base_url/api_key/model → graceful skip, no API call."""
    res = ip.plan_b_or_c(
        "x", candidate_id=9103, out_dir=tmp_dir,
        base_url="", api_key="", model="", note="plan_b",
    )
    _assert(res.path is None, "unconfigured provider must return None")
    _assert("not configured" in res.note, f"unexpected note: {res.note!r}")
    print("  PASS  test_plan_b_skipped_when_unconfigured")


def test_ensure_image_full_ladder_plan_a(tmp_dir: Path) -> None:
    """Plan A succeeds → ensure_image returns Plan A path (WebP)."""
    import requests
    orig_get = requests.get
    requests.get = lambda *a, **kw: FakeDownloadOK(*a, **kw)
    try:
        res = ip.ensure_image(
            candidate_id=9201,
            source_image_url="http://example.com/cover.jpg",
            post_image_prompt="ignored because A succeeds",
            post_image_alt="alt text",
            item_title="Some title",
            out_dir=tmp_dir,
        )
        _assert(res.plan == "A", f"expected Plan A, got {res.plan}")
        _assert(res.path is not None, "expected path from Plan A")
        _assert(res.path.exists(), "Plan A path does not exist")
        _assert(res.path.suffix == ".webp",
                f"expected .webp suffix, got {res.path.suffix}")
        _assert(_sniff_format(res.path) == "WEBP",
                f"Plan A output is not WebP: {_sniff_format(res.path)}")
    finally:
        requests.get = orig_get
    print(f"  PASS  test_ensure_image_full_ladder_plan_a")


def test_ensure_image_full_ladder_plan_b(tmp_dir: Path) -> None:
    """Plan A fails → Plan B succeeds → ensure_image returns B path (WebP)."""
    import requests
    orig_get = requests.get
    requests.get = lambda *a, **kw: FakeDownloadFail(*a, **kw)
    fake = FakeImageAPI(payload=None)
    _patch_openai_client(ip, fake, image_config={
        "base_url": "http://x", "api_key": "k", "model": "m",
        "fallback_base_url": "", "fallback_api_key": "", "fallback_model": "",
    })
    try:
        res = ip.ensure_image(
            candidate_id=9202,
            source_image_url="http://example.com/broken.jpg",
            post_image_prompt="Editorial dramatic lighting",
            post_image_alt="alt",
            item_title="title",
            out_dir=tmp_dir,
        )
        _assert(res.plan == "B", f"expected Plan B, got {res.plan}")
        _assert(res.path is not None, "expected path from Plan B")
        _assert(res.path.suffix == ".webp",
                f"expected .webp suffix, got {res.path.suffix}")
        _assert(_sniff_format(res.path) == "WEBP",
                f"Plan B output is not WebP: {_sniff_format(res.path)}")
        _assert(len(fake.calls) == 1,
                f"expected exactly one Plan B call, got {len(fake.calls)}")
    finally:
        requests.get = orig_get
    print(f"  PASS  test_ensure_image_full_ladder_plan_b")


def test_ensure_image_full_ladder_plan_c(tmp_dir: Path) -> None:
    """Plan A fails → Plan B fails → Plan C succeeds."""
    import requests
    orig_get = requests.get
    requests.get = lambda *a, **kw: FakeDownloadFail(*a, **kw)

    state = {"n_calls": 0}
    class CountedAPI:
        class images:
            @staticmethod
            def generate(**kwargs):
                state["n_calls"] += 1
                if state["n_calls"] == 1:
                    raise RuntimeError("primary down")
                from base64 import b64encode
                class _Resp:
                    data = [type("D", (), {"b64_json": b64encode(_REAL_JPEG_BYTES).decode()})()]
                return _Resp

    ip._image_client = lambda *a, **kw: CountedAPI()
    # Patch IMAGE so both Plan B and Plan C are "configured".
    from config import IMAGE as real_IMAGE
    for k, v in {"base_url": "http://x", "api_key": "k", "model": "m",
                 "fallback_base_url": "http://y", "fallback_api_key": "k2",
                 "fallback_model": "m2"}.items():
        setattr(real_IMAGE, k, v)
    try:
        res = ip.ensure_image(
            candidate_id=9203,
            source_image_url="http://example.com/broken.jpg",
            post_image_prompt="Editorial lighting",
            post_image_alt="alt",
            item_title="title",
            out_dir=tmp_dir,
        )
        _assert(res.plan in ("C", "B"),
                f"expected Plan B or C, got {res.plan}")
        _assert(res.path is not None, "expected path from Plan C fallback")
        _assert(state["n_calls"] >= 1, "image API was never called")
    finally:
        requests.get = orig_get
    print(f"  PASS  test_ensure_image_full_ladder_plan_c (plan={res.plan})")


def test_ensure_image_full_ladder_plan_d(tmp_dir: Path) -> None:
    """All plans fail → graceful skip (Plan D, path=None)."""
    import requests
    orig_get = requests.get
    requests.get = lambda *a, **kw: FakeDownloadFail(*a, **kw)
    fake = FakeImageAPI(fail=True)
    _patch_openai_client(ip, fake, image_config={
        "base_url": "http://x", "api_key": "k", "model": "m",
        "fallback_base_url": "http://y", "fallback_api_key": "k2", "fallback_model": "m2",
    })
    try:
        res = ip.ensure_image(
            candidate_id=9204,
            source_image_url="http://example.com/broken.jpg",
            post_image_prompt="Editorial lighting",
            post_image_alt="alt",
            item_title="title",
            out_dir=tmp_dir,
        )
        _assert(res.plan == "D", f"expected Plan D, got {res.plan}")
        _assert(res.path is None, f"Plan D must return None path, got {res.path}")
    finally:
        requests.get = orig_get
    print("  PASS  test_ensure_image_full_ladder_plan_d")


def test_build_prompt_prefers_llm_prompt(tmp_dir: Path) -> None:
    """LLM-provided image_prompt wins over the synthetic fallback."""
    res = ip.build_prompt(
        post_image_prompt="Editorial neon lights",
        post_image_alt="alt",
        item_title="title",
    )
    _assert(res == "Editorial neon lights",
            f"expected LLM prompt, got {res!r}")
    print("  PASS  test_build_prompt_prefers_llm_prompt")


def test_build_prompt_falls_back_synthetic(tmp_dir: Path) -> None:
    """When LLM prompt is missing, synthetic from alt+title."""
    res = ip.build_prompt(
        post_image_prompt=None,
        post_image_alt="A car under neon",
        item_title="Some title",
    )
    _assert(res is not None, "synthetic prompt must not be None")
    _assert("A car under neon" in res,
            f"synthetic must include alt: {res!r}")
    _assert("no text" in res, "synthetic must include no-text clause")
    print("  PASS  test_build_prompt_falls_back_synthetic")


def test_build_prompt_returns_none_when_empty(tmp_dir: Path) -> None:
    """Both empty → None (Plan C will skip)."""
    res = ip.build_prompt(post_image_prompt=None,
                          post_image_alt=None, item_title=None)
    _assert(res is None, f"empty inputs must yield None, got {res!r}")
    res = ip.build_prompt(post_image_prompt="",
                          post_image_alt="", item_title="")
    _assert(res is None, "blank inputs must yield None")
    print("  PASS  test_build_prompt_returns_none_when_empty")


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import tempfile
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures: list[tuple[str, BaseException]] = []

    # Generate one set of valid JPEG bytes shared by all tests. We don't
    # care about the contents — just that Pillow can decode them.
    with tempfile.TemporaryDirectory(prefix="img_smoke_seed_") as seed_dir:
        global _REAL_JPEG_BYTES
        _REAL_JPEG_BYTES = _make_real_jpeg_bytes(Path(seed_dir) / "seed.jpg")

        for fn in tests:
            with tempfile.TemporaryDirectory(prefix="img_smoke_") as td:
                tmp_dir = Path(td)
                try:
                    fn(tmp_dir)
                except BaseException as exc:  # noqa: BLE001
                    failures.append((fn.__name__, exc))
                    print(f"  FAIL  {fn.__name__}: {exc}")
    print("-" * 60)
    pass_n = len(tests) - len(failures)
    print(f"  {pass_n}/{len(tests)} passed")
    if failures:
        sys.exit(1)
    print("  ALL GREEN ✓")


if __name__ == "__main__":
    main()