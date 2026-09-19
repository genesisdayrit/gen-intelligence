"""Hourly Obsidian reconcile: registry, deferred queue, watermark, Readwise."""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ.setdefault("READWISE_TOKEN", "test-readwise-token")
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"

from services.obsidian.reconcile.providers import (
    DeferredDropboxProvider,
    GranolaSinceCheckProvider,
    ReadwiseSinceCheckProvider,
    ReconcileContext,
    StubProvider,
    default_providers,
)
from services.obsidian.reconcile.queue import (
    DEAD_HASH_KEY,
    DEFERRED_HASH_KEY,
    MAX_ATTEMPTS,
    dedup_key,
    drain_deferred_batch,
    enqueue_deferred,
)
from services.obsidian.reconcile.runner import (
    WATERMARK_KEY,
    reconcile_missed_obsidian_writes,
    resolve_since,
)
from services.obsidian.utils.dropbox_rev_safe import record_deferred_write


class FakeRedis:
    """Minimal hash/string Redis for reconcile tests."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value):
        self.kv[key] = value
        return True

    def hset(self, name, key, value):
        self.hashes.setdefault(name, {})[key] = value
        return 1

    def hget(self, name, key):
        return self.hashes.get(name, {}).get(key)

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def hdel(self, name, *keys):
        bucket = self.hashes.setdefault(name, {})
        removed = 0
        for key in keys:
            if key in bucket:
                del bucket[key]
                removed += 1
        return removed


def _ctx(
    *,
    since: str | None = "2026-09-19T17:00:00Z",
    batch_size: int = 50,
    now: datetime | None = None,
) -> ReconcileContext:
    started = now or datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc)
    return ReconcileContext(
        since=since,
        run_started="2026-09-19T18:00:00Z",
        batch_size=batch_size,
        now=started,
    )


def _item(**overrides):
    item = {
        "source": "readwise",
        "kind": "journal_highlight",
        "payload_ref": "111",
        "target": "/vault/Sep 19, 2026.md",
        "payload": {"id": 111, "text": "quote", "book_id": 7},
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


def test_default_registry_includes_required_and_stub_providers():
    names = [provider.name for provider in default_providers()]
    assert names[0] == "deferred_dropbox"
    assert "readwise" in names
    assert "granola" in names
    for stub in ("share_link", "daily_action", "todoist", "manus", "telegram"):
        assert stub in names


def test_registry_invokes_stubs():
    stubs = [p for p in default_providers() if isinstance(p, StubProvider)]
    assert {p.name for p in stubs} == {
        "share_link",
        "daily_action",
        "todoist",
        "manus",
        "telegram",
    }
    ctx = _ctx()
    for provider in stubs:
        result = provider.reconcile(ctx)
        assert result["provider"] == provider.name
        assert result["status"] == "stub"
        assert result["processed"] == 0
        assert "Not yet wired" in result["note"]


def test_runner_invokes_every_registered_provider_including_stubs():
    fake = FakeRedis()
    called: list[str] = []

    class Tracking(StubProvider):
        def reconcile(self, ctx):
            called.append(self.name)
            return super().reconcile(ctx)

    providers = [
        Tracking("deferred_dropbox"),
        Tracking("readwise"),
        Tracking("share_link"),
        Tracking("telegram"),
    ]
    with patch("services.obsidian.reconcile.runner.redis_client", fake):
        result = reconcile_missed_obsidian_writes(
            since="2026-09-19T17:00:00Z",
            now=datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc),
            providers=providers,
        )
    assert called == ["deferred_dropbox", "readwise", "share_link", "telegram"]
    assert [row["status"] for row in result["providers"]] == ["stub"] * 4


# ---------------------------------------------------------------------------
# Queue drain + dedup
# ---------------------------------------------------------------------------


def test_enqueue_dedups_same_key_and_keeps_attempts():
    fake = FakeRedis()
    with patch("services.obsidian.reconcile.queue.redis_client", fake):
        first = enqueue_deferred(**_item(), enqueued_at="2026-09-19T17:01:00Z")
        first["attempts"] = 2
        key = dedup_key("readwise", "journal_highlight", "111", "/vault/Sep 19, 2026.md")
        fake.hset(DEFERRED_HASH_KEY, key, __import__("json").dumps(first))
        second = enqueue_deferred(**_item(), enqueued_at="2026-09-19T17:05:00Z")
    assert second["attempts"] == 2
    assert second["enqueued_at"] == "2026-09-19T17:01:00Z"
    assert len(fake.hgetall(DEFERRED_HASH_KEY)) == 1


def test_drain_batch_removes_success_bumps_failure_and_dead_letters():
    fake = FakeRedis()
    outcomes = {"a": True, "b": False, "c": False}

    def replay(item):
        return outcomes[item["payload_ref"]]

    with patch("services.obsidian.reconcile.queue.redis_client", fake):
        enqueue_deferred(**_item(payload_ref="a", payload={"id": "a"}))
        enqueue_deferred(**_item(payload_ref="b", payload={"id": "b"}))
        enqueue_deferred(**_item(payload_ref="c", payload={"id": "c"}, attempts=MAX_ATTEMPTS - 1))
        first = drain_deferred_batch(limit=2, replay_fn=replay)
        assert first["selected"] == 2
        assert first["succeeded"] == 1
        assert first["failed"] == 1
        assert first["dead_lettered"] == 0

        remaining = fake.hgetall(DEFERRED_HASH_KEY)
        assert "readwise:journal_highlight:a:/vault/Sep 19, 2026.md" not in remaining
        b_key = "readwise:journal_highlight:b:/vault/Sep 19, 2026.md"
        assert b_key in remaining
        assert '"attempts": 1' in remaining[b_key]

        second = drain_deferred_batch(limit=10, replay_fn=replay)
        assert second["succeeded"] == 0
        assert second["failed"] == 2
        assert second["dead_lettered"] == 1
        assert "readwise:journal_highlight:c:/vault/Sep 19, 2026.md" not in fake.hgetall(
            DEFERRED_HASH_KEY
        )
        assert fake.hgetall(DEAD_HASH_KEY)


def test_deferred_drain_does_not_date_gate_journal_day():
    """A deferred item from last week is still drained — no today-only filter."""
    fake = FakeRedis()
    seen: list[dict] = []

    def replay(item):
        seen.append(item)
        return True

    with patch("services.obsidian.reconcile.queue.redis_client", fake):
        enqueue_deferred(
            source="readwise",
            kind="journal_highlight",
            payload_ref="old",
            target="/vault/Sep 1, 2026.md",
            payload={"journal_date": "Sep 1, 2026", "text": "old highlight"},
            enqueued_at="2026-09-01T10:00:00Z",
        )
        enqueue_deferred(
            source="granola",
            kind="journal_note",
            payload_ref="not_old",
            target="/vault/Sep 12, 2026.md",
            payload={"id": "not_old"},
            enqueued_at="2026-09-12T08:00:00Z",
        )
        result = drain_deferred_batch(limit=10, replay_fn=replay)

    assert result["succeeded"] == 2
    assert [row["payload_ref"] for row in seen] == ["old", "not_old"]
    assert seen[0]["payload"]["journal_date"] == "Sep 1, 2026"


def test_record_deferred_write_uses_shared_queue():
    fake = FakeRedis()
    with patch("services.obsidian.reconcile.queue.redis_client", fake):
        record_deferred_write(
            source="folder_journal",
            kind="journal_yaml",
            payload_ref="/vault/Note.md",
            target="/vault/Note.md",
        )
        record_deferred_write(
            source="folder_journal",
            kind="journal_yaml",
            payload_ref="/vault/Note.md",
            target="/vault/Note.md",
        )
    assert len(fake.hgetall(DEFERRED_HASH_KEY)) == 1


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------


def test_watermark_key_is_last_reconcile_check_at():
    assert WATERMARK_KEY == "obsidian_reconcile:last_reconcile_check_at"


def test_resolve_since_prefers_override_then_watermark_then_bootstrap():
    now = datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc)
    assert resolve_since("2026-09-01T00:00:00Z", now=now, stored="2026-09-19T17:00:00Z") == (
        "2026-09-01T00:00:00Z"
    )
    assert resolve_since(None, now=now, stored="2026-09-19T17:00:00Z") == "2026-09-19T17:00:00Z"
    # First run only: empty watermark seeds now−1h. Not the primary window.
    assert resolve_since(None, now=now, stored=None) == "2026-09-19T17:00:00Z"
    # Job was down 3h — use the stored cursor as-is, do not clamp to 1h.
    assert resolve_since(None, now=now, stored="2026-09-19T15:00:00Z") == "2026-09-19T15:00:00Z"


def test_first_run_bootstraps_one_hour_then_since_last_check_is_unclamped():
    """Empty Redis seeds now−1h once; a later gap uses the watermark, not now−1h."""
    fake = FakeRedis()
    now_first = datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc)
    with patch("services.obsidian.reconcile.runner.redis_client", fake):
        first = reconcile_missed_obsidian_writes(
            now=now_first,
            providers=[StubProvider("share_link")],
        )
    assert first["since"] == "2026-09-19T17:00:00Z"
    assert first["watermark_advanced"] is True
    assert fake.get(WATERMARK_KEY) == "2026-09-19T18:00:00Z"

    now_later = datetime(2026, 9, 19, 21, 0, tzinfo=timezone.utc)
    with patch("services.obsidian.reconcile.runner.redis_client", fake):
        second = reconcile_missed_obsidian_writes(
            now=now_later,
            providers=[StubProvider("share_link")],
        )
    assert second["since"] == "2026-09-19T18:00:00Z"
    assert second["since"] != "2026-09-19T20:00:00Z"
    assert fake.get(WATERMARK_KEY) == "2026-09-19T21:00:00Z"


def test_watermark_advances_to_run_start_after_successful_providers():
    fake = FakeRedis()
    fake.set(WATERMARK_KEY, "2026-09-19T17:00:00Z")
    now = datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc)
    with patch("services.obsidian.reconcile.runner.redis_client", fake):
        result = reconcile_missed_obsidian_writes(
            now=now,
            providers=[StubProvider("share_link")],
        )
    assert result["since"] == "2026-09-19T17:00:00Z"
    assert result["run_started"] == "2026-09-19T18:00:00Z"
    assert result["watermark_advanced"] is True
    assert fake.get(WATERMARK_KEY) == "2026-09-19T18:00:00Z"


def test_watermark_does_not_advance_on_hard_provider_error():
    fake = FakeRedis()
    fake.set(WATERMARK_KEY, "2026-09-19T17:00:00Z")

    class Boom(StubProvider):
        def reconcile(self, ctx):
            return {"provider": self.name, "status": "error", "processed": 0, "errors": ["down"]}

    now = datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc)
    with patch("services.obsidian.reconcile.runner.redis_client", fake):
        result = reconcile_missed_obsidian_writes(now=now, providers=[Boom("readwise")])
    assert result["watermark_advanced"] is False
    assert fake.get(WATERMARK_KEY) == "2026-09-19T17:00:00Z"


def test_manual_since_override_is_used_and_still_advances_watermark():
    fake = FakeRedis()
    now = datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc)
    with patch("services.obsidian.reconcile.runner.redis_client", fake):
        result = reconcile_missed_obsidian_writes(
            since="2026-09-19T12:00:00Z",
            now=now,
            providers=[StubProvider("share_link")],
        )
    assert result["since"] == "2026-09-19T12:00:00Z"
    assert fake.get(WATERMARK_KEY) == "2026-09-19T18:00:00Z"


# ---------------------------------------------------------------------------
# Readwise since-check (idempotent, mocked)
# ---------------------------------------------------------------------------


def test_readwise_since_check_calls_webhook_writer_and_is_idempotent():
    highlight = {"id": 954480, "text": "Most Amazing Highlight Ever", "book_id": 8237}
    calls: list[dict] = []

    def fake_append(payload, now=None):
        calls.append(payload)
        action = "skipped" if len(calls) > 1 else "inserted"
        return {"success": True, "action": action, "error": None}

    ctx = _ctx()
    with (
        patch(
            "services.readwise.export.iter_export_highlights",
            return_value=[highlight],
        ) as export,
        patch(
            "services.readwise.reader.iter_reader_documents",
            return_value=[],
        ) as reader,
        patch(
            "services.obsidian.add_readwise_buffet.append_readwise_buffet",
            side_effect=fake_append,
        ),
    ):
        provider = ReadwiseSinceCheckProvider()
        first = provider.reconcile(ctx)
        second = provider.reconcile(ctx)

    export.assert_called_with(updated_after=ctx.since)
    reader.assert_called_with(updated_after=ctx.since)
    assert first["inserted"] == 1
    assert first["skipped"] == 0
    assert second["inserted"] == 0
    assert second["skipped"] == 1
    assert calls[0]["id"] == calls[1]["id"] == 954480
    assert calls[0]["event_type"] == "readwise.highlight.created"
    assert calls[0]["text"] == calls[1]["text"]


def test_readwise_since_check_stamps_documents_and_respects_batch_cap():
    highlights = [
        {"id": 1, "text": "one", "book_id": 1},
        {"id": 2, "text": "two", "book_id": 1},
    ]
    documents = [{"id": "doc-1", "title": "Saved", "url": "https://example.com"}]
    seen: list[str] = []

    def fake_append(payload, now=None):
        seen.append(str(payload["id"]))
        return {"success": True, "action": "inserted", "error": None}

    ctx = _ctx(batch_size=2)
    with (
        patch("services.readwise.export.iter_export_highlights", return_value=highlights),
        patch("services.readwise.reader.iter_reader_documents", return_value=documents),
        patch(
            "services.obsidian.add_readwise_buffet.append_readwise_buffet",
            side_effect=fake_append,
        ),
    ):
        result = ReadwiseSinceCheckProvider().reconcile(ctx)

    assert result["selected"] == 2
    assert result["processed"] == 2
    assert seen == ["1", "2"]


def test_granola_provider_calls_existing_incremental_sync():
    ctx = _ctx()
    with patch(
        "services.granola.sync.sync_granola_notes",
        return_value={"selected": 3, "inserted": 1, "skipped": 2, "errors": []},
    ) as mock_sync:
        result = GranolaSinceCheckProvider().reconcile(ctx)
    mock_sync.assert_called_once_with(updated_after=ctx.since, now=ctx.now)
    assert result["status"] == "ok"
    assert result["processed"] == 3


def test_granola_since_check_is_idempotent_on_second_pass():
    ctx = _ctx()
    responses = [
        {"selected": 1, "inserted": 1, "skipped": 0, "errors": []},
        {"selected": 1, "inserted": 0, "skipped": 1, "errors": []},
    ]
    with patch(
        "services.granola.sync.sync_granola_notes",
        side_effect=responses,
    ) as mock_sync:
        first = GranolaSinceCheckProvider().reconcile(ctx)
        second = GranolaSinceCheckProvider().reconcile(ctx)
    assert first["inserted"] == 1
    assert second["skipped"] == 1
    assert mock_sync.call_count == 2
    assert mock_sync.call_args_list[0].kwargs["updated_after"] == ctx.since
    assert mock_sync.call_args_list[1].kwargs["updated_after"] == ctx.since


def test_runner_drains_deferred_queue_then_since_checks():
    """One hourly pass: drain enqueue-time queue, then Readwise + Granola since-check."""
    fake = FakeRedis()
    now = datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc)

    with (
        patch("services.obsidian.reconcile.queue.redis_client", fake),
        patch("services.obsidian.reconcile.runner.redis_client", fake),
        patch("services.obsidian.reconcile.queue._default_replay", return_value=True),
        patch(
            "services.readwise.export.iter_export_highlights",
            return_value=[{"id": 1, "text": "hi", "book_id": 2}],
        ) as export,
        patch("services.readwise.reader.iter_reader_documents", return_value=[]),
        patch(
            "services.obsidian.add_readwise_buffet.append_readwise_buffet",
            return_value={"success": True, "action": "skipped"},
        ) as writer,
        patch(
            "services.granola.sync.sync_granola_notes",
            return_value={"selected": 0, "inserted": 0, "skipped": 0, "errors": []},
        ) as granola,
    ):
        enqueue_deferred(
            source="todoist",
            kind="completed",
            payload_ref="Task",
            target="/vault/DA 2026-09-01.md",
            payload={"task_content": "Task", "journal_date": "Sep 1, 2026"},
            enqueued_at="2026-09-01T10:00:00Z",
        )
        result = reconcile_missed_obsidian_writes(
            now=now,
            providers=[
                DeferredDropboxProvider(),
                ReadwiseSinceCheckProvider(),
                GranolaSinceCheckProvider(),
            ],
        )

    names = [row["provider"] for row in result["providers"]]
    assert names == ["deferred_dropbox", "readwise", "granola"]
    assert result["providers"][0]["succeeded"] == 1
    assert fake.hgetall(DEFERRED_HASH_KEY) == {}
    export.assert_called_with(updated_after=result["since"])
    writer.assert_called()
    granola.assert_called_once_with(updated_after=result["since"], now=now)
    assert result["watermark_advanced"] is True
    assert fake.get(WATERMARK_KEY) == "2026-09-19T18:00:00Z"


def test_default_replay_dispatches_conflict_guard_sources():
    from services.obsidian.reconcile.queue import _default_replay

    with (
        patch(
            "services.granola.sync.write_notes_by_journal",
            return_value={"errors": [], "deferred": 0},
        ) as granola,
        patch(
            "services.obsidian.add_shared_link.add_shared_link",
            return_value={"success": True, "action": "updated"},
        ) as share,
        patch(
            "services.obsidian.add_todoist_completed.append_todoist_completed",
        ) as todoist,
        patch(
            "services.obsidian.add_telegram_log.append_telegram_log",
        ) as telegram,
        patch(
            "services.obsidian.add_manus_task.upsert_manus_task",
            return_value={"daily_action_success": True, "daily_action_action": "inserted"},
        ) as manus,
        patch(
            "services.obsidian.add_youtube_link.add_youtube_link",
            return_value={"success": True, "action": "updated"},
        ) as youtube,
        patch(
            "scripts.obsidian.workflows.file_updates.update_daily_journal_properties.update_daily_journal_properties",
            return_value=True,
        ) as journal_props,
        patch(
            "services.obsidian.add_daily_action_issues_touched.upsert_daily_action_issue_touched",
            return_value={"success": True, "action": "inserted"},
        ) as issues,
        patch(
            "scripts.obsidian.workflows.file_updates.add_daily_review_section.add_daily_review_section",
            return_value=True,
        ) as review,
        patch(
            "services.obsidian.update_telegram_log.update_telegram_log",
            return_value=True,
        ) as telegram_update,
    ):
        assert _default_replay(
            {"source": "granola", "kind": "journal_note", "payload": {"id": "not_1"}}
        )
        assert _default_replay(
            {
                "source": "share_link",
                "kind": "kh_update",
                "payload_ref": "https://example.com",
                "target": "/kh/a.md",
                "payload": {"url": "https://example.com"},
            }
        )
        assert _default_replay(
            {
                "source": "todoist",
                "kind": "completed",
                "payload_ref": "Task",
                "payload": {"task_content": "Task"},
            }
        )
        assert _default_replay(
            {
                "source": "telegram",
                "kind": "log",
                "payload": {"message_text": "[01:00 PM] hi", "message_id": 9},
            }
        )
        assert _default_replay(
            {
                "source": "manus",
                "kind": "daily_action",
                "payload": {
                    "task_id": "abc",
                    "task_title": "T",
                    "task_url": "https://manus.im/app/abc",
                },
            }
        )
        assert _default_replay(
            {
                "source": "youtube",
                "kind": "kh_update",
                "payload_ref": "https://youtu.be/abcdefghijk",
                "payload": {"url": "https://youtu.be/abcdefghijk"},
            }
        )
        assert _default_replay(
            {"source": "journal_properties", "kind": "journal_properties", "payload": {"use_today": True}}
        )
        assert _default_replay(
            {
                "source": "daily_action",
                "kind": "issues_touched",
                "payload": {
                    "issue_identifier": "GD-1",
                    "project_name": "P",
                    "issue_title": "T",
                    "status_name": "Todo",
                    "issue_url": "https://linear.app/x/issue/gd-1",
                },
            }
        )
        assert _default_replay({"source": "daily_action", "kind": "review_section", "payload": {}})
        assert _default_replay(
            {
                "source": "telegram",
                "kind": "log_update",
                "payload": {"message_id": 9, "new_text": "edited"},
            }
        )

    granola.assert_called_once()
    share.assert_called_once()
    todoist.assert_called_once_with("Task")
    telegram.assert_called_once()
    manus.assert_called_once()
    youtube.assert_called_once()
    journal_props.assert_called_once_with(use_today=True)
    issues.assert_called_once()
    review.assert_called_once()
    telegram_update.assert_called_once_with(9, "edited")


def test_deferred_dropbox_provider_drains_queue():
    ctx = _ctx(batch_size=10)
    with patch(
        "services.obsidian.reconcile.providers.drain_deferred_batch",
        return_value={"selected": 2, "succeeded": 2, "failed": 0, "processed": 2, "errors": []},
    ) as mock_drain:
        result = DeferredDropboxProvider().reconcile(ctx)
    mock_drain.assert_called_once_with(limit=10)
    assert result["status"] == "ok"
    assert result["processed"] == 2
