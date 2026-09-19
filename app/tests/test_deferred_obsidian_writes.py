"""Deferred Obsidian write queue and scheduled reconcile job tests."""

import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ.setdefault("READWISE_TOKEN", "test-readwise-token")
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")


@pytest.fixture(autouse=True)
def _force_env(monkeypatch):
    monkeypatch.setenv("SYSTEM_TIMEZONE", "America/Los_Angeles")
    monkeypatch.setenv("READWISE_TOKEN", "test-readwise-token")
    monkeypatch.setenv("GRANOLA_API_KEY", "test-granola-key")


from services.obsidian.add_readwise_buffet import (  # noqa: E402
    append_readwise_buffet,
    clear_book_cache,
    insert_content_buffet_bullet,
)
from services.obsidian.reconcile_deferred_writes import (  # noqa: E402
    reconcile_deferred_obsidian_writes,
    reconcile_readwise_since,
    replay_deferred_item,
    resolve_readwise_updated_after,
    seed_updated_after,
)
from services.obsidian.utils.deferred_writes import (  # noqa: E402
    DEAD_LETTER_KEY,
    KIND_JOURNAL_HIGHLIGHT,
    LAST_READWISE_RECONCILE_KEY,
    LAST_RECONCILE_KEY,
    PENDING_KEY,
    QUEUE_KEY,
    SOURCE_READWISE,
    DeferredWriteContext,
    dedup_key,
    drain_deferred_queue,
    enqueue_deferred_write,
    get_watermark,
)
from services.obsidian.utils.dropbox_rev_safe import (  # noqa: E402
    upload_if_rev_matches,
    upload_new_file,
)

LA = pytz.timezone("America/Los_Angeles")
JOURNAL_FOLDER = "/obsidian/personal/01_daily/_journal"
REV_STALE = "fedcba9876543210"
REV_MATCH = "0123456789abcdef"

SAMPLE_JOURNAL = """---
date: 2026-08-22
---

### Content Buffet:
- 

### Content Planning
- plan something
"""

JOURNAL_WITH_HIGHLIGHT = """---
date: 2026-08-22
---

### Content Buffet:
- [[Deep Work by Cal Newport]]: "Most Amazing Highlight Ever" ([Link](https://readwise.io/open/954480))

### Content Planning
- plan something
"""


class FakeRedis:
    """In-memory Redis subset used by the deferred-write queue."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.sets: dict[str, set[str]] = {}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, **_kwargs):
        self.kv[key] = value
        return True

    def sadd(self, key, *values):
        bucket = self.sets.setdefault(key, set())
        added = 0
        for value in values:
            if value not in bucket:
                bucket.add(value)
                added += 1
        return added

    def srem(self, key, *values):
        bucket = self.sets.get(key)
        if not bucket:
            return 0
        removed = 0
        for value in values:
            if value in bucket:
                bucket.remove(value)
                removed += 1
        return removed

    def sismember(self, key, value):
        return value in self.sets.get(key, set())

    def rpush(self, key, *values):
        lst = self.lists.setdefault(key, [])
        lst.extend(values)
        return len(lst)

    def lpop(self, key):
        lst = self.lists.get(key)
        if not lst:
            return None
        return lst.pop(0)

    def llen(self, key):
        return len(self.lists.get(key, []))

    def lrange(self, key, start, end):
        lst = self.lists.get(key, [])
        if end == -1:
            return lst[start:]
        return lst[start : end + 1]


def _rev_conflict_api_error():
    import dropbox

    reason = dropbox.files.WriteError.conflict(dropbox.files.WriteConflictError.file)
    failed = dropbox.files.UploadWriteFailed(reason=reason, upload_session_id="sess")
    error = dropbox.files.UploadError.path(failed)
    return dropbox.exceptions.ApiError("req", error, "", "")


def _download_with_rev(content: str, *, rev: str, path: str):
    metadata = MagicMock()
    metadata.rev = rev
    metadata.path_display = path
    response = MagicMock()
    response.content = content.encode("utf-8")
    return metadata, response


def _highlight_payload(**overrides):
    data = {
        "id": 954480,
        "text": "Most Amazing Highlight Ever",
        "note": "",
        "highlighted_at": "2025-11-27T18:55:56.719036Z",
        "url": None,
        "updated": "2025-11-27T18:55:56.867572Z",
        "book_id": 8237,
        "title": "Deep Work",
        "author": "Cal Newport",
        "event_type": "readwise.highlight.created",
        "secret": "test-readwise-secret",
    }
    data.update(overrides)
    return data


def _book(user_book_id=8237, title="Deep Work", author="Cal Newport", highlights=None):
    return {
        "user_book_id": user_book_id,
        "is_deleted": False,
        "title": title,
        "readable_title": title,
        "author": author,
        "highlights": highlights or [],
        "category": "books",
    }


def _hl(highlight_id=954480, text="Most Amazing Highlight Ever", **overrides):
    highlight = {
        "id": highlight_id,
        "is_deleted": False,
        "text": text,
        "note": None,
        "highlighted_at": "2025-11-27T18:55:56.719036Z",
        "created_at": "2025-11-27T18:55:56.719036Z",
        "updated_at": "2025-11-27T18:55:56.719036Z",
        "url": None,
        "book_id": 8237,
        "is_discard": False,
        "readwise_url": f"https://readwise.io/open/{highlight_id}",
    }
    highlight.update(overrides)
    return highlight


def _export_page(results, next_page_cursor=None):
    return {"count": len(results), "nextPageCursor": next_page_cursor, "results": results}


# ---------------------------------------------------------------------------
# Queue: enqueue + dedup
# ---------------------------------------------------------------------------


def test_enqueue_dedupes_by_source_payload_and_target():
    redis = FakeRedis()
    first = enqueue_deferred_write(
        source=SOURCE_READWISE,
        kind=KIND_JOURNAL_HIGHLIGHT,
        payload_ref="954480",
        target_path=f"{JOURNAL_FOLDER}/Aug 22, 2026.md",
        client=redis,
    )
    second = enqueue_deferred_write(
        source=SOURCE_READWISE,
        kind=KIND_JOURNAL_HIGHLIGHT,
        payload_ref="954480",
        target_path=f"{JOURNAL_FOLDER}/Aug 22, 2026.md",
        client=redis,
    )
    assert first is True
    assert second is False
    assert redis.llen(QUEUE_KEY) == 1
    assert redis.sismember(
        PENDING_KEY,
        dedup_key(
            SOURCE_READWISE,
            "954480",
            f"{JOURNAL_FOLDER}/Aug 22, 2026.md",
        ),
    )


def test_enqueue_skips_missing_payload_ref():
    redis = FakeRedis()
    assert (
        enqueue_deferred_write(
            source=SOURCE_READWISE,
            kind=KIND_JOURNAL_HIGHLIGHT,
            payload_ref="",
            client=redis,
        )
        is False
    )
    assert redis.llen(QUEUE_KEY) == 0


def test_upload_if_rev_matches_enqueues_on_defer():
    redis = FakeRedis()
    mock_dbx = MagicMock()
    mock_dbx.files_upload.side_effect = _rev_conflict_api_error()
    ctx = DeferredWriteContext(
        source=SOURCE_READWISE,
        kind=KIND_JOURNAL_HIGHLIGHT,
        payload_ref="954480",
        target_path="/vault/Aug 22, 2026.md",
        journal_date="Aug 22, 2026",
    )
    with patch(
        "services.obsidian.utils.deferred_writes.redis_client",
        redis,
    ):
        result = upload_if_rev_matches(
            mock_dbx, "/vault/Aug 22, 2026.md", b"x", REV_STALE, defer=ctx
        )
    assert result.status == "deferred"
    assert redis.llen(QUEUE_KEY) == 1
    item = json.loads(redis.lrange(QUEUE_KEY, 0, 0)[0])
    assert item["source"] == SOURCE_READWISE
    assert item["payload_ref"] == "954480"
    assert item["kind"] == KIND_JOURNAL_HIGHLIGHT


def test_upload_new_file_enqueues_on_defer():
    redis = FakeRedis()
    mock_dbx = MagicMock()
    mock_dbx.files_upload.side_effect = _rev_conflict_api_error()
    ctx = DeferredWriteContext(
        source=SOURCE_READWISE,
        kind="kh_tweet",
        payload_ref="99",
        target_path="/hub/Tweets from @x.md",
    )
    with patch("services.obsidian.utils.deferred_writes.redis_client", redis):
        result = upload_new_file(mock_dbx, "/hub/Tweets from @x.md", b"x", defer=ctx)
    assert result.status == "deferred"
    assert redis.llen(QUEUE_KEY) == 1


# ---------------------------------------------------------------------------
# Readwise writer enqueues after immediate retry still defers
# ---------------------------------------------------------------------------


def test_readwise_journal_defer_after_retry_enqueues():
    """PR #206 immediate retry still deferred → queue the highlight."""
    clear_book_cache()
    redis = FakeRedis()
    mock_dbx = MagicMock()
    journal_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    mock_dbx.files_download.return_value = _download_with_rev(
        SAMPLE_JOURNAL, rev=REV_STALE, path=journal_path
    )
    mock_dbx.files_upload.side_effect = _rev_conflict_api_error()
    now = LA.localize(datetime(2026, 8, 22, 2, 30))

    with (
        patch(
            "services.obsidian.add_readwise_buffet._get_dropbox_client",
            return_value=mock_dbx,
        ),
        patch(
            "services.obsidian.add_readwise_buffet._find_folder_by_suffix",
            side_effect=["/obsidian/personal/01_daily", JOURNAL_FOLDER],
        ),
        patch("services.obsidian.utils.deferred_writes.redis_client", redis),
    ):
        result = append_readwise_buffet(_highlight_payload(), now=now)

    assert result["action"] == "deferred"
    assert mock_dbx.files_upload.call_count == 2
    assert redis.llen(QUEUE_KEY) == 1
    item = json.loads(redis.lrange(QUEUE_KEY, 0, 0)[0])
    assert item["source"] == SOURCE_READWISE
    assert item["kind"] == KIND_JOURNAL_HIGHLIGHT
    assert item["payload_ref"] == "954480"
    assert item["target_path"] == journal_path


def test_readwise_journal_retry_success_does_not_enqueue():
    """Immediate rematch after re-download must not enqueue."""
    clear_book_cache()
    redis = FakeRedis()
    mock_dbx = MagicMock()
    journal_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    mock_dbx.files_download.side_effect = [
        _download_with_rev(SAMPLE_JOURNAL, rev=REV_STALE, path=journal_path),
        _download_with_rev(SAMPLE_JOURNAL, rev=REV_MATCH, path=journal_path),
    ]
    mock_dbx.files_upload.side_effect = [_rev_conflict_api_error(), MagicMock()]
    now = LA.localize(datetime(2026, 8, 22, 2, 30))

    with (
        patch(
            "services.obsidian.add_readwise_buffet._get_dropbox_client",
            return_value=mock_dbx,
        ),
        patch(
            "services.obsidian.add_readwise_buffet._find_folder_by_suffix",
            side_effect=["/obsidian/personal/01_daily", JOURNAL_FOLDER],
        ),
        patch("services.obsidian.utils.deferred_writes.redis_client", redis),
    ):
        result = append_readwise_buffet(_highlight_payload(), now=now)

    assert result["action"] == "replaced"
    assert redis.llen(QUEUE_KEY) == 0


# ---------------------------------------------------------------------------
# Drain / replay
# ---------------------------------------------------------------------------


def test_drain_queue_succeeds_and_removes_item():
    redis = FakeRedis()
    enqueue_deferred_write(
        source=SOURCE_READWISE,
        kind=KIND_JOURNAL_HIGHLIGHT,
        payload_ref="954480",
        target_path=f"{JOURNAL_FOLDER}/Aug 22, 2026.md",
        client=redis,
    )
    replayed = []

    def replay(item):
        replayed.append(item)
        return True

    summary = drain_deferred_queue(replay, client=redis)
    assert summary["processed"] == 1
    assert summary["succeeded"] == 1
    assert summary["requeued"] == 0
    assert redis.llen(QUEUE_KEY) == 0
    assert replayed[0]["payload_ref"] == "954480"
    assert not redis.sismember(
        PENDING_KEY,
        dedup_key(SOURCE_READWISE, "954480", f"{JOURNAL_FOLDER}/Aug 22, 2026.md"),
    )


def test_drain_requeues_then_dead_letters_after_max_attempts():
    redis = FakeRedis()
    enqueue_deferred_write(
        source=SOURCE_READWISE,
        kind=KIND_JOURNAL_HIGHLIGHT,
        payload_ref="1",
        target_path="/j.md",
        client=redis,
    )
    for expected_attempts in range(1, 5):
        summary = drain_deferred_queue(lambda _item: False, limit=1, client=redis)
        assert summary["requeued"] == 1
        remaining = json.loads(redis.lrange(QUEUE_KEY, 0, 0)[0])
        assert remaining["attempts"] == expected_attempts

    summary = drain_deferred_queue(lambda _item: False, limit=1, client=redis)
    assert summary["dead_lettered"] == 1
    assert redis.llen(QUEUE_KEY) == 0
    dead = json.loads(redis.lrange(DEAD_LETTER_KEY, 0, 0)[0])
    assert dead["attempts"] == 5
    assert dead["payload_ref"] == "1"


def test_replay_readwise_item_calls_append_buffet():
    item = {
        "source": SOURCE_READWISE,
        "kind": KIND_JOURNAL_HIGHLIGHT,
        "payload_ref": "954480",
        "target_path": f"{JOURNAL_FOLDER}/Nov 27, 2025.md",
        "attempts": 0,
    }
    payload = _highlight_payload()
    with (
        patch(
            "services.obsidian.reconcile_deferred_writes.get_highlight",
            return_value=payload,
        ),
        patch(
            "services.obsidian.reconcile_deferred_writes.append_readwise_buffet",
            return_value={"success": True, "action": "skipped"},
        ) as mock_append,
    ):
        assert replay_deferred_item(item) is True
    mock_append.assert_called_once()
    sent = mock_append.call_args.args[0]
    assert sent["id"] == 954480


def test_reconcile_drains_queue_and_succeeds():
    redis = FakeRedis()
    enqueue_deferred_write(
        source=SOURCE_READWISE,
        kind=KIND_JOURNAL_HIGHLIGHT,
        payload_ref="954480",
        target_path=f"{JOURNAL_FOLDER}/Nov 27, 2025.md",
        client=redis,
    )
    now = datetime(2026, 9, 19, 21, 0, tzinfo=timezone.utc)

    with (
        patch(
            "services.obsidian.reconcile_deferred_writes.get_highlight",
            return_value=_highlight_payload(),
        ),
        patch(
            "services.obsidian.reconcile_deferred_writes.append_readwise_buffet",
            return_value={"success": True, "action": "inserted"},
        ),
        patch(
            "services.obsidian.reconcile_deferred_writes.reconcile_readwise_since",
            return_value={"errors": [], "highlights": {"selected": 0}, "documents": {}},
        ),
        patch(
            "services.obsidian.reconcile_deferred_writes.reconcile_granola_since",
            return_value={"selected": 0, "skipped": 0},
        ),
    ):
        summary = reconcile_deferred_obsidian_writes(now=now, client=redis)

    assert summary["queue"]["processed"] == 1
    assert summary["queue"]["succeeded"] == 1
    assert redis.llen(QUEUE_KEY) == 0
    assert get_watermark(LAST_RECONCILE_KEY, client=redis) == "2026-09-19T21:00:00Z"
    assert get_watermark(LAST_READWISE_RECONCILE_KEY, client=redis) == "2026-09-19T21:00:00Z"


# ---------------------------------------------------------------------------
# Readwise since-check is idempotent
# ---------------------------------------------------------------------------


def test_readwise_since_check_does_not_duplicate_buffet_line():
    """A highlight already in the journal is skipped, not written twice."""
    clear_book_cache()
    journal_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    store = {journal_path: JOURNAL_WITH_HIGHLIGHT}
    uploaded = []

    mock_dbx = MagicMock()

    def download(path):
        metadata = MagicMock()
        metadata.rev = REV_MATCH
        metadata.path_display = path
        response = MagicMock()
        response.content = store[path].encode("utf-8")
        return metadata, response

    def upload(data, path, mode=None, autorename=None):
        text = data.decode("utf-8")
        store[path] = text
        uploaded.append(text)
        return MagicMock()

    mock_dbx.files_download.side_effect = download
    mock_dbx.files_upload.side_effect = upload

    book = _book(highlights=[_hl()])
    book.pop("category", None)
    pages = [_export_page([book])]
    page_iter = iter(pages)

    def fake_get(url, params=None, headers=None, timeout=None):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        if "export" in (url or ""):
            response.json.return_value = next(page_iter)
        else:
            response.json.return_value = {"results": [], "nextPageCursor": None}
        return response

    with (
        patch("services.readwise.export.requests.get", side_effect=fake_get),
        patch("services.readwise.reader.requests.get", side_effect=fake_get),
        patch(
            "services.obsidian.add_readwise_buffet._get_dropbox_client",
            return_value=mock_dbx,
        ),
        patch(
            "services.obsidian.add_readwise_buffet._find_folder_by_suffix",
            side_effect=["/obsidian/personal/01_daily", JOURNAL_FOLDER],
        ),
        patch(
            "services.obsidian.add_readwise_buffet._resolve_knowledge_hub_folder",
            return_value="/obsidian/personal/01_knowledge-hub",
        ),
    ):
        summary = reconcile_readwise_since("2026-09-19T20:45:00Z")

    assert summary["highlights"]["selected"] == 1
    assert summary["highlights"]["skipped"] == 1
    assert summary["highlights"]["inserted"] == 0
    assert summary["highlights"]["files_written"] == 0
    assert uploaded == []
    assert store[journal_path].count("https://readwise.io/open/954480") == 1
    # insert helper itself is also idempotent on the same keys
    again, action = insert_content_buffet_bullet(
        store[journal_path],
        '- [[Deep Work by Cal Newport]]: "Most Amazing Highlight Ever" '
        "([Link](https://readwise.io/open/954480))",
        ["https://readwise.io/open/954480"],
    )
    assert action == "skipped"
    assert again == store[journal_path]


def test_empty_watermark_seeds_fifteen_minutes():
    now = datetime(2026, 9, 19, 21, 0, tzinfo=timezone.utc)
    assert seed_updated_after(now) == "2026-09-19T20:45:00Z"
    redis = FakeRedis()
    assert resolve_readwise_updated_after(now=now, client=redis) == "2026-09-19T20:45:00Z"
    redis.set(LAST_READWISE_RECONCILE_KEY, "2026-09-19T20:30:00Z")
    assert (
        resolve_readwise_updated_after(now=now, client=redis) == "2026-09-19T20:30:00Z"
    )
