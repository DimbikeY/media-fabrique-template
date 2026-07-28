"""Sprint 4 smoke test for publisher.

Mocks the WP REST layer (via a ``MockWPClient`` subclass that intercepts
``_request`` and returns canned responses) and exercises:

  1. Happy path — first publish, no existing slug.
  2. Slug already exists — we PATCH instead of POST.
  3. WP returns 401 — claim is released, post moved to failed.
  4. WP returns 500 — retries, eventually fails the post.

Each scenario seeds a fresh item + draft post (unique feed_url) and
cleans up afterwards so the smoke is idempotent and safe to re-run.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

PROJECT = Path(__file__).parent
sys.path.insert(0, str(PROJECT))

import publisher as pub  # noqa: E402
from config import PIPE  # noqa: E402


# --- Mock client -------------------------------------------------------------
class _MockResponse:
    """Stand-in for ``requests.Response`` with just enough surface for
    ``WPClient._request`` and the high-level methods."""
    def __init__(self, status_code: int, body: Any = None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class MockWPClient(pub.WPClient):
    """WPClient subclass that bypasses real HTTP and uses canned responses.

    Configure per-scenario via:
        MockWPClient.find_post_response   → list returned by /posts?slug=
        MockWPClient.create_response      → dict returned by POST /posts
        MockWPClient.update_response      → dict returned by POST /posts/{id}
        MockWPClient.upload_response      → dict returned by POST /media
        MockWPClient.categories_response  → list returned by /categories
        MockWPClient.tags_response        → list returned by /tags
        MockWPClient.fail_with            → Exception class to raise instead
    """

    def __init__(self):
        # Skip parent __init__ — no real HTTP needed.
        self.base_url = "https://wp.example"
        self.api_root = "https://wp.example/wp-json/wp/v2"

    # Per-scenario state — set these on the instance before run().
    find_post_response: List[Dict[str, Any]] = []
    create_response: Optional[Dict[str, Any]] = None
    update_response: Optional[Dict[str, Any]] = None
    upload_response: Optional[Dict[str, Any]] = None
    categories_response: List[Dict[str, Any]] = []
    tags_response: List[Dict[str, Any]] = []
    # If set, this exception type is raised on the next call (for testing
    # auth/request failures). Re-armed each call.
    fail_with: Optional[Exception] = None
    # Counters for assertions.
    post_calls: int = 0
    patch_calls: int = 0
    media_calls: int = 0
    # Sprint 5.2.1: capture the last multipart payload so scenario tests
    # can assert the mime sent to WP. Shape: ``{"file": (name, bytes, mime)}``.
    last_media_files: Optional[Dict[str, Any]] = None

    def _request(self, method: str, path: str, **kwargs) -> _MockResponse:
        # Failure injection takes priority.
        if self.fail_with is not None:
            ex = self.fail_with
            self.fail_with = None  # consume once
            raise ex

        # GET /posts?slug=...
        if method == "GET" and path == "/posts":
            return _MockResponse(200, list(self.find_post_response))

        # GET /categories
        if method == "GET" and path == "/categories":
            return _MockResponse(200, list(self.categories_response))

        # GET /tags
        if method == "GET" and path == "/tags":
            return _MockResponse(200, list(self.tags_response))

        # POST /media
        if method == "POST" and path == "/media":
            self.media_calls += 1
            self.last_media_files = kwargs.get("files")
            return _MockResponse(201, self.upload_response or {"id": 999})

        # POST /posts  (create)
        if method == "POST" and path == "/posts":
            self.post_calls += 1
            return _MockResponse(201, self.create_response or {
                "id": 1000 + self.post_calls, "link": "https://wp.example/p/1",
            })

        # POST /posts/{id}  (update)
        if method == "POST" and path.startswith("/posts/"):
            self.patch_calls += 1
            return _MockResponse(200, self.update_response or {
                "id": 2000, "link": "https://wp.example/p/existing",
            })

        raise AssertionError(f"unexpected request: {method} {path}")


# --- Helpers -----------------------------------------------------------------
def _stub_ensure_image(return_path: Optional[Path] = None) -> None:
    """Patch image_pipeline.ensure_image so publisher tests don't hit
    the real image API. By default returns path=None (Plan D — graceful
    skip), which is what most smoke tests assume: we want to test WP
    behaviour, not image generation.

    Tests that need a real image path can pass ``return_path=<Path>``
    and the publisher will go on to upload that file (caller must have
    created a valid WebP at that path — Sprint 5.2.1 switched the
    on-disk format from JPEG to WebP).
    """
    import image_pipeline

    def _fake_ensure_image(**kwargs):
        from image_pipeline import EnsureImageResult
        return EnsureImageResult(path=return_path,
                                 plan="D" if return_path is None else "B",
                                 note="stubbed for smoke test")
    image_pipeline.ensure_image = _fake_ensure_image


def _seed_draft_post(
    conn: sqlite3.Connection,
    *,
    image_path: Optional[Path] = None,
    categories: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
) -> Tuple[int, int]:
    """Insert a fresh ready item + draft post. Return (candidate_id, post_id)."""
    feed_url = f"https://smoke.example/{uuid.uuid4().hex}.xml"
    src_id = conn.execute(
        "INSERT INTO sources(name, feed_url) VALUES (?, ?)",
        ("Smoke Source", feed_url),
    ).lastrowid
    cur = conn.execute(
        """
        INSERT INTO candidates(source_id, guid, url, title, summary, status,
                          safety_status, lang, video_embed_url)
        VALUES (?, ?, ?, ?, ?, 'ready', 'review', 'en', ?)
        """,
        (src_id, f"smoke-{src_id}", "https://smoke.example/article",
         "Smoke article", "Smoke body", None),
    )
    candidate_id = cur.lastrowid
    cur = conn.execute(
        """
        INSERT INTO draft_posts(
            candidate_id, title, slug, excerpt, content_html,
            meta_title, meta_description,
            image_alt, image_prompt,
            featured_image_path, categories_json, tags_json,
            status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')
        """,
        (
            candidate_id, "Smoke title", f"smoke-{src_id}", "smoke excerpt",
            "<p>smoke body</p>", "smoke meta", "smoke meta desc",
            "smoke alt text", "smoke prompt",
            str(image_path) if image_path else None,
            json.dumps(categories or [], ensure_ascii=False),
            json.dumps(tags or [], ensure_ascii=False),
        ),
    )
    conn.commit()
    return candidate_id, cur.lastrowid


def _cleanup(conn: sqlite3.Connection, candidate_id: int) -> None:
    src_row = conn.execute(
        "SELECT source_id FROM candidates WHERE id=?", (candidate_id,),
    ).fetchone()
    conn.execute("DELETE FROM draft_posts WHERE candidate_id=?", (candidate_id,))
    conn.execute("DELETE FROM candidates WHERE id=?", (candidate_id,))
    if src_row:
        conn.execute("DELETE FROM sources WHERE id=?", (src_row[0],))
    conn.commit()


def _post_row(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM draft_posts WHERE id=?", (post_id,),
    ).fetchone()


def _load_post_row(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row:
    """Load the exact row shape that ``process_one`` expects (joined with
    candidates + sources). We select only the post we just seeded so concurrent
    real data in the DB doesn't leak into the smoke. The column list MUST
    match what publisher._fetch_candidates returns, otherwise inline image
    acquisition will raise IndexError on missing columns.
    """
    return conn.execute(
        """
        SELECT p.id AS post_id,
               p.candidate_id, p.title, p.slug, p.excerpt, p.content_html,
               p.meta_title, p.meta_description,
               p.featured_image_path, p.image_alt, p.image_prompt,
               p.categories_json, p.tags_json,
               i.title AS item_title, i.url AS item_url,
               s.name AS source_name, s.feed_url AS source_url,
               s.homepage_url AS source_homepage,
               i.video_embed_url,
               i.image_url AS source_image_url,
               i.weight, i.base_score, i.category
          FROM draft_posts p
          JOIN candidates i ON i.id = p.candidate_id
          JOIN sources s ON s.id = i.source_id
         WHERE p.id = ?
        """,
        (post_id,),
    ).fetchone()


# --- Scenarios ---------------------------------------------------------------
def scenario_happy_path() -> None:
    print("\n[scenario] happy path: create new post + upload media + set terms")
    _stub_ensure_image()
    # Sprint 5.2.1: featured image is now a real WebP (was JPEG).
    from PIL import Image
    real_image = Path("/tmp/smoke_publisher_featured.webp")
    with Image.new("RGB", (300, 200), (60, 80, 180)) as im:
        im.save(real_image, format="WEBP", quality=82, method=6)
    try:
        with sqlite3.connect(PIPE.db_path) as conn:
            conn.row_factory = sqlite3.Row
            candidate_id, post_id = _seed_draft_post(
                conn,
                image_path=real_image,
                categories=["Игры", "Обзоры"],
                tags=["ps5", "solo-leveling"],
            )
            try:
                client = MockWPClient()
                client.find_post_response = []  # no existing slug
                client.upload_response = {"id": 555, "link": "https://wp.example/m/555"}
                client.categories_response = [
                    {"id": 1, "slug": "igry", "name": "Игры"},
                    {"id": 2, "slug": "obzory", "name": "Обзоры"},
                ]
                client.tags_response = [
                    {"id": 10, "slug": "ps5", "name": "PS5"},
                    {"id": 11, "slug": "solo-leveling", "name": "Solo Leveling"},
                ]
                client.create_response = {"id": 4242, "link": "https://wp.example/p/4242"}

                row = _load_post_row(conn, post_id)
                assert row is not None
                assert pub._claim(conn, row["post_id"]), "claim should succeed"
                conn.commit()
                tag = pub.process_one(client, conn, row)

                assert tag == "published", f"expected published, got {tag}"
                r = _post_row(conn, post_id)
                assert r["status"] == "published", r["status"]
                assert r["wp_post_id"] == 4242, r["wp_post_id"]
                assert r["wp_post_url"] == "https://wp.example/p/4242"
                assert client.post_calls == 1
                assert client.patch_calls == 0
                assert client.media_calls == 1
                # Content should include Источник paragraph.
                assert "Источник:" in (r["content_html"] or "")
                # Sprint 5.2.1: media upload must use image/webp mime,
                # derived from the file extension in publisher.upload_media.
                assert client.last_media_files is not None, \
                    "media upload was never attempted"
                file_tuple = client.last_media_files.get("file")
                assert file_tuple is not None, \
                    f"unexpected files shape: {client.last_media_files!r}"
                _name, _bytes, mime = file_tuple
                assert mime == "image/webp", \
                    f"expected image/webp mime, got {mime!r}"
                print(f"  ok -> wp_id=4242, post_status=published, mime={mime}")
            finally:
                _cleanup(conn, candidate_id)
    finally:
        real_image.unlink(missing_ok=True)


def scenario_slug_exists() -> None:
    print("\n[scenario] slug already exists: PATCH instead of POST")
    _stub_ensure_image()
    with sqlite3.connect(PIPE.db_path) as conn:
        conn.row_factory = sqlite3.Row
        candidate_id, post_id = _seed_draft_post(
            conn, image_path=None,
            categories=["Игры"], tags=["ps5"],
        )
        try:
            client = MockWPClient()
            # WP already has this slug.
            client.find_post_response = [{"id": 9999, "link": "https://wp.example/p/9999"}]
            client.categories_response = [{"id": 1, "slug": "igry", "name": "Игры"}]
            client.tags_response = [{"id": 10, "slug": "ps5", "name": "PS5"}]
            client.update_response = {"id": 9999, "link": "https://wp.example/p/9999"}

            row = _load_post_row(conn, post_id)
            assert row is not None
            assert pub._claim(conn, row["post_id"])
            conn.commit()
            tag = pub.process_one(client, conn, row)

            assert tag == "published", f"expected published, got {tag}"
            r = _post_row(conn, post_id)
            assert r["status"] == "published"
            assert r["wp_post_id"] == 9999, r["wp_post_id"]
            assert client.post_calls == 0, "should NOT POST /posts"
            assert client.patch_calls == 1, "should POST /posts/9999"
            assert client.media_calls == 0, "no image → no media upload"
            print(f"  ok -> updated existing wp_id=9999")
        finally:
            _cleanup(conn, candidate_id)


def scenario_auth_failure() -> None:
    print("\n[scenario] WP returns 401: post moved to failed, claim released")
    _stub_ensure_image()
    with sqlite3.connect(PIPE.db_path) as conn:
        conn.row_factory = sqlite3.Row
        candidate_id, post_id = _seed_draft_post(conn, image_path=None)
        try:
            client = MockWPClient()
            # First call (slug check) returns 401.
            client.fail_with = pub.WPAuthError("GET /posts → 401: nope")

            row = _load_post_row(conn, post_id)
            assert row is not None
            assert pub._claim(conn, row["post_id"])
            conn.commit()
            tag = pub.process_one(client, conn, row)

            assert tag == "failed", f"expected failed, got {tag}"
            r = _post_row(conn, post_id)
            assert r["status"] == "failed", r["status"]
            assert r["wp_post_id"] is None
            assert "wp_auth:" in (r["error_reason"] or ""), r["error_reason"]
            print(f"  ok -> failed with reason: {r['error_reason']!r}")
        finally:
            _cleanup(conn, candidate_id)


def scenario_request_failure() -> None:
    print("\n[scenario] WP returns 500 (retryable): post failed with wp_request")
    _stub_ensure_image()
    with sqlite3.connect(PIPE.db_path) as conn:
        conn.row_factory = sqlite3.Row
        candidate_id, post_id = _seed_draft_post(conn, image_path=None)
        try:
            client = MockWPClient()
            # Replace _request with one that always raises WPRequestError
            # (mimics a 5xx after tenacity exhausts its budget).
            client._request = lambda method, path, **kw: (_ for _ in ()).throw(
                pub.WPRequestError(f"{method} {path} → 500: simulated")
            )

            row = _load_post_row(conn, post_id)
            assert row is not None
            assert pub._claim(conn, row["post_id"])
            conn.commit()
            tag = pub.process_one(client, conn, row)

            assert tag == "failed", f"expected failed, got {tag}"
            r = _post_row(conn, post_id)
            assert r["status"] == "failed"
            assert "wp_request:" in (r["error_reason"] or ""), r["error_reason"]
            print(f"  ok -> failed with reason: {r['error_reason']!r}")
        finally:
            _cleanup(conn, candidate_id)


# --- Runner ------------------------------------------------------------------
def main() -> int:
    scenarios = [
        scenario_happy_path,
        scenario_slug_exists,
        scenario_auth_failure,
        scenario_request_failure,
    ]
    for s in scenarios:
        s()
    print(f"\nAll {len(scenarios)} publisher smoke scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())