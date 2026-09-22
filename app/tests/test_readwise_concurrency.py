"""Redis per-target lock + Reader document single-flight."""

from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("SYSTEM_TIMEZONE", "America/Los_Angeles")

from services.obsidian.add_readwise_buffet import (
    append_readwise_buffet,
    readwise_target_identity,
)
from services.obsidian.utils.readwise_concurrency import (
    READER_SINGLEFLIGHT_TTL_SECONDS,
    claim_reader_document_singleflight,
    lock_readwise_target,
    normalize_knowledge_hub_url,
    reader_singleflight_key,
    readwise_target_lock_key,
    release_reader_document_singleflight,
)
from services.obsidian.utils.redis_lock import (
    DEFAULT_LOCK_TTL_SECONDS,
    redis_lock,
)

pytestmark = pytest.mark.readwise_concurrency


class FakeRedis:
    """Minimal thread-safe Redis for SET NX / GET / DEL / EVAL release."""

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

    def eval(self, _script, _numkeys, key, token):
        with self._lock:
            if self.store.get(key) == token:
                del self.store[key]
                return 1
            return 0


def _reader_payload(**overrides):
    data = {
        "id": "01kb5cap1wy21zp37bc2rjj",
        "url": "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj",
        "title": "Our Black Friday sale ends soon",
        "author": "The Verge",
        "source_url": "https://www.theverge.com/black-friday",
        "category": "article",
        "parent_id": None,
        "summary": "A sale.",
        "event_type": "reader.any_document.created",
    }
    data.update(overrides)
    return data


def _highlight_payload(**overrides):
    data = {
        "id": 954480,
        "text": "Most Amazing Highlight Ever",
        "book_id": 8237,
        "title": "Exclusive | The Tech Elite Is Funding an Alternative to College",
        "author": "WSJ",
        "category": "articles",
        "source_url": "https://www.wsj.com/tech/elite-college?si=abc",
        "event_type": "readwise.highlight.created",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# URL identity
# ---------------------------------------------------------------------------


def test_normalize_url_strips_tracking_slash_and_fragment():
    assert (
        normalize_knowledge_hub_url(
            "https://www.WSJ.com/tech/elite-college/?si=share&is=1#comments"
        )
        == "https://www.wsj.com/tech/elite-college"
    )


def test_normalize_youtube_collapses_to_watch_v():
    assert (
        normalize_knowledge_hub_url("https://youtu.be/dQw4w9wgWcQ?si=share")
        == "https://www.youtube.com/watch?v=dQw4w9wgWcQ"
    )


def test_highlight_and_document_share_normalized_url_identity():
    highlight = _highlight_payload()
    document = _reader_payload(
        source_url="https://www.wsj.com/tech/elite-college?is=1",
        title="Exclusive | The Tech Elite Is Funding an Alternative to College",
        author="WSJ",
    )
    assert readwise_target_identity(highlight) == readwise_target_identity(document)
    assert readwise_target_identity(highlight) == "https://www.wsj.com/tech/elite-college"


def test_identity_falls_back_to_dropbox_path_when_no_url():
    payload = {
        "id": 1,
        "text": "quote",
        "book_id": 99,
        "event_type": "readwise.highlight.created",
    }
    path = "/obsidian/personal/02_Knowledge-Hub/Deep Work.md"
    with patch(
        "services.obsidian.add_readwise_buffet._resolve_highlight_book",
        return_value={"title": "Deep Work"},
    ):
        assert readwise_target_identity(payload, dropbox_target=path) == path


# ---------------------------------------------------------------------------
# Lock: serialize same key, isolate different keys, always release
# ---------------------------------------------------------------------------


def test_lock_serializes_same_key_writers():
    fake = FakeRedis()
    order: list[str] = []
    first_holding = threading.Event()
    release_first = threading.Event()

    def first():
        with redis_lock("same-note", client=fake, wait_seconds=2, poll_interval=0.01):
            order.append("a-in")
            first_holding.set()
            assert release_first.wait(timeout=2)
            order.append("a-out")

    def second():
        assert first_holding.wait(timeout=2)
        with redis_lock("same-note", client=fake, wait_seconds=2, poll_interval=0.01):
            order.append("b-in")
            order.append("b-out")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    assert first_holding.wait(timeout=2)
    time.sleep(0.05)
    assert order == ["a-in"]
    release_first.set()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert order == ["a-in", "a-out", "b-in", "b-out"]


def test_lock_different_keys_do_not_block_each_other():
    fake = FakeRedis()
    entered: list[str] = []
    both_in = threading.Barrier(2)
    release = threading.Event()

    def worker(key: str):
        with redis_lock(key, client=fake, wait_seconds=2, poll_interval=0.01):
            entered.append(key)
            both_in.wait(timeout=2)
            assert release.wait(timeout=2)

    t1 = threading.Thread(target=worker, args=("note-a",))
    t2 = threading.Thread(target=worker, args=("note-b",))
    t1.start()
    t2.start()
    deadline = time.monotonic() + 2
    while len(entered) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert set(entered) == {"note-a", "note-b"}
    release.set()
    t1.join(timeout=2)
    t2.join(timeout=2)


def test_lock_releases_on_success():
    fake = FakeRedis()
    key = "kh-note"
    with redis_lock(key, client=fake, wait_seconds=1, poll_interval=0.01) as acquired:
        assert acquired is True
        assert fake.get(key) is not None
    assert fake.get(key) is None


def test_lock_releases_on_error():
    fake = FakeRedis()
    key = "kh-note"
    with pytest.raises(RuntimeError, match="boom"):
        with redis_lock(key, client=fake, wait_seconds=1, poll_interval=0.01) as acquired:
            assert acquired is True
            assert fake.get(key) is not None
            raise RuntimeError("boom")
    assert fake.get(key) is None


def test_lock_acquire_uses_set_nx_and_ttl():
    fake = FakeRedis()
    with redis_lock("note", client=fake, ttl_seconds=60, wait_seconds=0.1):
        pass
    assert fake.set_calls
    _key, _value, nx, ex = fake.set_calls[0]
    assert nx is True
    assert ex == 60


def test_lock_readwise_target_prefixes_identity():
    fake = FakeRedis()
    identity = "https://www.wsj.com/tech/elite-college"
    with lock_readwise_target(identity, client=fake, wait_seconds=0.1):
        assert fake.get(readwise_target_lock_key(identity)) is not None
    assert fake.get(readwise_target_lock_key(identity)) is None


# ---------------------------------------------------------------------------
# Reader single-flight / debounce
# ---------------------------------------------------------------------------


def test_reader_singleflight_first_wins_same_url():
    fake = FakeRedis()
    url = "https://www.wsj.com/tech/elite-college"
    assert claim_reader_document_singleflight(url, client=fake) is True
    assert claim_reader_document_singleflight(url, client=fake) is False
    assert claim_reader_document_singleflight(
        "https://www.theverge.com/other", client=fake
    ) is True
    _key, _value, nx, ex = fake.set_calls[0]
    assert _key == reader_singleflight_key(url)
    assert nx is True
    assert ex == READER_SINGLEFLIGHT_TTL_SECONDS


def test_reader_singleflight_release_allows_retry():
    fake = FakeRedis()
    url = "https://www.wsj.com/tech/elite-college"
    assert claim_reader_document_singleflight(url, client=fake) is True
    release_reader_document_singleflight(url, client=fake)
    assert claim_reader_document_singleflight(url, client=fake) is True


def test_reader_singleflight_redis_error_fails_open():
    class Boom:
        def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

    assert claim_reader_document_singleflight("https://x.test", client=Boom()) is True


def test_append_document_singleflight_suppresses_duplicate_write_and_raindrop():
    fake = FakeRedis()
    payload = _reader_payload()
    share_result = {
        "success": True,
        "action": "created",
        "error": None,
        "file_path": "_Knowledge-Hub/Our Black Friday sale ends soon.md",
    }
    with patch(
        "services.obsidian.utils.readwise_concurrency.redis_client", fake
    ), patch(
        "services.obsidian.utils.redis_lock.redis_client", fake
    ), patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value=share_result,
    ) as mock_share, patch(
        "services.obsidian.add_readwise_buffet.create_bookmark",
        return_value={"success": True, "bookmark_id": "1", "error": None},
    ) as mock_bookmark:
        first = append_readwise_buffet(payload)
        second = append_readwise_buffet(payload)

    assert first["action"] == "created"
    assert second["action"] == "skipped_singleflight"
    mock_share.assert_called_once()
    mock_bookmark.assert_called_once()


def test_append_document_releases_singleflight_on_kh_error():
    fake = FakeRedis()
    payload = _reader_payload()
    with patch(
        "services.obsidian.utils.readwise_concurrency.redis_client", fake
    ), patch(
        "services.obsidian.utils.redis_lock.redis_client", fake
    ), patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value={"success": False, "action": "kh_error", "error": "dropbox down"},
    ), patch(
        "services.obsidian.add_readwise_buffet.create_bookmark"
    ) as mock_bookmark:
        first = append_readwise_buffet(payload)
        second = append_readwise_buffet(
            payload,
        )

    assert first["action"] == "kh_error"
    assert first["success"] is True
    # Claim released so the retry is allowed to run.
    assert second["action"] == "kh_error"
    mock_bookmark.assert_not_called()


def test_lock_ttl_constant_is_sixty_seconds():
    assert DEFAULT_LOCK_TTL_SECONDS == 60
    assert READER_SINGLEFLIGHT_TTL_SECONDS == 45
