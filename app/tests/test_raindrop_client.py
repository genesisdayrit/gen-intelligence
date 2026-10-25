"""Raindrop bookmark URL normalize, Redis NX race, and search fallback."""

from __future__ import annotations

import os
import sys
import threading
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("RAINDROP_IO_TEST_TOKEN", "test-raindrop-token")

from services.raindrop.client import (  # noqa: E402
    BOOKMARK_DEDUP_TTL_SECONDS,
    bookmark_dedup_key,
    create_bookmark,
    normalize_bookmark_url,
)


class FakeRedis:
    """Minimal thread-safe Redis for SET NX / GET / DEL."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple] = []
        self._lock = threading.Lock()

    def set(self, key, value, nx=False, ex=None, px=None):
        self.set_calls.append((key, value, nx, ex))
        with self._lock:
            if nx and key in self.store:
                return False
            self.store[key] = value
            return True

    def get(self, key):
        with self._lock:
            return self.store.get(key)

    def delete(self, *keys):
        deleted = 0
        with self._lock:
            for key in keys:
                if key in self.store:
                    del self.store[key]
                    deleted += 1
        return deleted


def _http_client(*, search_items=None, post_id="rd-new"):
    search_items = list(search_items or [])
    posts: list[dict] = []
    gets: list[dict] = []

    class _Client:
        def __init__(self, *args, **kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, headers=None, params=None):
            gets.append({"url": url, "params": params})
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {"items": search_items}
            return response

        def post(self, url, headers=None, json=None):
            posts.append({"url": url, "json": json})
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {"item": {"_id": post_id}}
            return response

    return _Client, posts, gets


def test_normalize_bookmark_url_youtube_variants_and_tracking():
    watch = "https://www.youtube.com/watch?v=abcdefghijk"
    assert normalize_bookmark_url("https://youtu.be/abcdefghijk?si=share") == watch
    assert (
        normalize_bookmark_url("https://www.youtube.com/shorts/abcdefghijk?is=1") == watch
    )
    assert (
        normalize_bookmark_url("https://m.youtube.com/watch?v=abcdefghijk&utm_source=app")
        == watch
    )
    assert (
        normalize_bookmark_url("https://www.youtube.com/watch?v=abcdefghijk&si=x&utm_medium=cpc")
        == watch
    )


def test_normalize_bookmark_url_strips_utm_slash_and_fragment():
    assert (
        normalize_bookmark_url(
            "https://www.Example.com/path/?utm_source=news&utm_campaign=fall#top"
        )
        == "https://www.example.com/path"
    )
    assert normalize_bookmark_url("https://Example.com/a/") == "https://example.com/a"
    assert normalize_bookmark_url("") == ""


def test_create_bookmark_nx_race_one_post():
    """Two near-simultaneous claims: only the NX winner POSTs."""
    fake = FakeRedis()
    url = "https://www.theverge.com/black-friday"
    started = threading.Barrier(3)
    client_cls, posts, _gets = _http_client()

    def _run():
        started.wait()
        create_bookmark(url, "Title", redis_conn=fake)

    with patch("services.raindrop.client.httpx.Client", client_cls), patch(
        "services.raindrop.client._get_access_token", return_value="test-raindrop-token"
    ):
        workers = [threading.Thread(target=_run) for _ in range(2)]
        for worker in workers:
            worker.start()
        started.wait()
        for worker in workers:
            worker.join()

    assert len(posts) == 1
    key = bookmark_dedup_key(normalize_bookmark_url(url))
    assert key in fake.store
    nx_claims = [call for call in fake.set_calls if call[2] is True]
    assert len(nx_claims) == 2
    assert BOOKMARK_DEDUP_TTL_SECONDS == 90 * 24 * 60 * 60


def test_create_bookmark_search_fallback_skips_post():
    """Redis miss + Raindrop already has the link → return it, no POST."""
    fake = FakeRedis()
    url = "https://youtu.be/abcdefghijk?si=old"
    existing = {
        "_id": 4242,
        "link": "https://www.youtube.com/watch?v=abcdefghijk",
    }
    client_cls, posts, gets = _http_client(search_items=[existing])

    with patch("services.raindrop.client.httpx.Client", client_cls), patch(
        "services.raindrop.client._get_access_token", return_value="test-raindrop-token"
    ):
        result = create_bookmark(url, "Vai", redis_conn=fake)

    assert result["success"] is True
    assert result["bookmark_id"] == "4242"
    assert posts == []
    assert gets
    assert fake.store[bookmark_dedup_key(normalize_bookmark_url(url))] == "4242"


def test_create_bookmark_posts_when_search_empty():
    fake = FakeRedis()
    client_cls, posts, _gets = _http_client(search_items=[], post_id="rd-9")

    with patch("services.raindrop.client.httpx.Client", client_cls), patch(
        "services.raindrop.client._get_access_token", return_value="test-raindrop-token"
    ):
        result = create_bookmark(
            "https://www.theverge.com/black-friday",
            "Sale",
            redis_conn=fake,
        )

    assert result["success"] is True
    assert result["bookmark_id"] == "rd-9"
    assert len(posts) == 1
    assert posts[0]["json"]["link"] == "https://www.theverge.com/black-friday"
