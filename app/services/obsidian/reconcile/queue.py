"""Redis deferred-write queue for rev-safe Obsidian hub uploads.

When ``upload_if_rev_matches`` still returns ``deferred`` after the
immediate re-download retry, callers enqueue a compact item. The hourly
reconcile job drains a batch: success removes the item, failure bumps
``attempts``, and ~5 failures move the item aside as a dead letter.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from config import redis_client

logger = logging.getLogger(__name__)

DEFERRED_HASH_KEY = "obsidian_reconcile:deferred"
DEAD_HASH_KEY = "obsidian_reconcile:deferred:dead"
MAX_ATTEMPTS = 5

ReplayFn = Callable[[dict[str, Any]], bool]


def format_utc_iso(value: datetime | None = None) -> str:
    """UTC instant as ``YYYY-MM-DDTHH:MM:SSZ``."""
    if value is None:
        value = datetime.now(timezone.utc)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def dedup_key(source: str, kind: str, payload_ref: str, target: str) -> str:
    """Stable Redis hash field for one deferred write."""
    return f"{source}:{kind}:{payload_ref}:{target}"


def _as_item(raw: object) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        logger.warning("Deferred queue item is not JSON: %r", raw)
        return None
    return data if isinstance(data, dict) else None


def enqueue_deferred(
    *,
    source: str,
    kind: str,
    payload_ref: str,
    target: str,
    payload: dict[str, Any] | None = None,
    enqueued_at: str | None = None,
    attempts: int = 0,
) -> dict[str, Any]:
    """Push a deferred write. Same dedup key is a no-op (keeps attempts)."""
    key = dedup_key(source, kind, payload_ref, target)
    existing_raw = redis_client.hget(DEFERRED_HASH_KEY, key)
    existing = _as_item(existing_raw)
    if existing is not None:
        logger.info("Deferred queue dedup hit key=%s attempts=%s", key, existing.get("attempts"))
        return existing

    item = {
        "source": source,
        "kind": kind,
        "payload_ref": payload_ref,
        "target": target,
        "enqueued_at": enqueued_at or format_utc_iso(),
        "attempts": int(attempts),
    }
    if payload is not None:
        item["payload"] = payload
    redis_client.hset(DEFERRED_HASH_KEY, key, json.dumps(item))
    logger.info(
        "Deferred queue enqueued source=%s kind=%s ref=%s target=%s",
        source,
        kind,
        payload_ref,
        target,
    )
    return item


def list_deferred(*, include_dead: bool = False) -> list[dict[str, Any]]:
    """Return queued items, oldest ``enqueued_at`` first.

    No calendar-day or journal-date filter. Dead-lettered items
    (``attempts >= MAX_ATTEMPTS``) are omitted unless ``include_dead``.
    """
    raw_items = redis_client.hgetall(DEFERRED_HASH_KEY) or {}
    items: list[dict[str, Any]] = []
    for field, raw in raw_items.items():
        item = _as_item(raw)
        if item is None:
            continue
        item["_key"] = field
        items.append(item)
    items.sort(key=lambda row: (str(row.get("enqueued_at") or ""), str(row.get("_key") or "")))
    if include_dead:
        return items
    return [item for item in items if int(item.get("attempts") or 0) < MAX_ATTEMPTS]


def _store_item(hash_key: str, item: dict[str, Any]) -> None:
    field = item.get("_key") or dedup_key(
        str(item.get("source") or ""),
        str(item.get("kind") or ""),
        str(item.get("payload_ref") or ""),
        str(item.get("target") or ""),
    )
    stored = {k: v for k, v in item.items() if k != "_key"}
    redis_client.hset(hash_key, field, json.dumps(stored))


def _move_to_dead(item: dict[str, Any]) -> None:
    field = item.get("_key") or dedup_key(
        str(item.get("source") or ""),
        str(item.get("kind") or ""),
        str(item.get("payload_ref") or ""),
        str(item.get("target") or ""),
    )
    stored = {k: v for k, v in item.items() if k != "_key"}
    redis_client.hset(DEAD_HASH_KEY, field, json.dumps(stored))
    redis_client.hdel(DEFERRED_HASH_KEY, field)
    logger.error(
        "Deferred queue dead-lettered source=%s kind=%s ref=%s target=%s attempts=%s",
        item.get("source"),
        item.get("kind"),
        item.get("payload_ref"),
        item.get("target"),
        item.get("attempts"),
    )


def _default_replay(item: dict[str, Any]) -> bool:
    """Replay using the same idempotent writers the live paths use."""
    source = str(item.get("source") or "")
    kind = str(item.get("kind") or "")
    payload = item.get("payload")
    target = str(item.get("target") or "")

    if source == "folder_journal" or kind == "journal_yaml":
        from scripts.obsidian.workflows.file_updates.update_modified_files_today import (
            STATUS_DEFERRED,
            STATUS_ERROR,
            _get_dropbox_client,
            _update_journal_property,
        )

        dbx = _get_dropbox_client()
        status = _update_journal_property(dbx, target)
        return status not in {STATUS_DEFERRED, STATUS_ERROR}

    if kind == "journal_wikilink" and isinstance(payload, dict):
        from services.obsidian.add_readwise_buffet import append_wikilink_to_journal_buffet

        result = append_wikilink_to_journal_buffet(
            str(payload.get("note_title") or item.get("payload_ref") or ""),
            str(payload.get("journal_date") or ""),
        )
        return result.get("action") not in {"deferred", "error"}

    if source == "granola" or kind == "journal_note":
        from services.granola.sync import write_notes_by_journal

        if not isinstance(payload, dict):
            return False
        result = write_notes_by_journal([payload], raise_errors=False)
        return not result.get("errors") and int(result.get("deferred") or 0) == 0

    if source == "share_link":
        from services.obsidian.add_shared_link import add_shared_link

        payload = payload if isinstance(payload, dict) else {}
        result = add_shared_link(
            str(payload.get("url") or item.get("payload_ref") or ""),
            payload.get("title"),
            journal_date=payload.get("journal_date"),
            extra_frontmatter=payload.get("extra_frontmatter"),
        )
        return bool(result.get("success")) and result.get("action") not in {"deferred", "error"}

    if source == "youtube":
        from services.obsidian.add_youtube_link import (
            add_youtube_link,
            apply_youtube_extra_frontmatter,
        )

        payload = payload if isinstance(payload, dict) else {}
        if kind == "kh_extra":
            result = apply_youtube_extra_frontmatter(
                str(payload.get("file_path") or target),
                payload.get("extra_frontmatter"),
            )
        else:
            result = add_youtube_link(
                str(payload.get("url") or item.get("payload_ref") or ""),
                journal_date=payload.get("journal_date"),
                extra_frontmatter=payload.get("extra_frontmatter"),
                note_title=payload.get("note_title"),
                note_author=payload.get("note_author"),
            )
        return bool(result.get("success")) and result.get("action") not in {"deferred", "error"}

    if source == "journal_properties" or kind == "journal_properties":
        from scripts.obsidian.workflows.file_updates.update_daily_journal_properties import (
            update_daily_journal_properties,
        )

        use_today = True
        if isinstance(payload, dict):
            use_today = bool(payload.get("use_today", True))
        return bool(update_daily_journal_properties(use_today=use_today))

    if source == "todoist":
        payload = payload if isinstance(payload, dict) else {}
        task_content = str(payload.get("task_content") or item.get("payload_ref") or "")
        if kind == "uncompleted":
            from services.obsidian.remove_todoist_completed import remove_todoist_completed

            remove_todoist_completed(task_content)
            return True
        from services.obsidian.add_todoist_completed import append_todoist_completed

        append_todoist_completed(task_content)
        return True

    if source == "telegram":
        payload = payload if isinstance(payload, dict) else {}
        if kind == "log_update":
            from services.obsidian.update_telegram_log import update_telegram_log

            return bool(
                update_telegram_log(
                    int(payload.get("message_id") or item.get("payload_ref") or 0),
                    str(payload.get("new_text") or ""),
                )
            )
        from services.obsidian.add_telegram_log import append_telegram_log

        append_telegram_log(
            str(payload.get("message_text") or item.get("payload_ref") or ""),
            payload.get("message_id"),
        )
        return True

    if source == "manus":
        from services.obsidian.add_manus_task import upsert_manus_task

        payload = payload if isinstance(payload, dict) else {}
        result = upsert_manus_task(
            str(payload.get("task_id") or item.get("payload_ref") or ""),
            str(payload.get("task_title") or ""),
            str(payload.get("task_url") or ""),
        )
        if kind == "weekly_cycle":
            return (
                bool(result.get("weekly_cycle_success"))
                and result.get("weekly_cycle_action") not in {"deferred"}
            )
        return (
            bool(result.get("daily_action_success"))
            and result.get("daily_action_action") not in {"deferred"}
        )

    if source == "daily_action":
        payload = payload if isinstance(payload, dict) else {}
        if kind == "review_section":
            from scripts.obsidian.workflows.file_updates.add_daily_review_section import (
                add_daily_review_section,
            )

            return bool(add_daily_review_section())
        if kind == "issues_touched":
            from services.obsidian.add_daily_action_issues_touched import (
                upsert_daily_action_issue_touched,
            )

            result = upsert_daily_action_issue_touched(
                issue_identifier=str(payload.get("issue_identifier") or item.get("payload_ref") or ""),
                project_name=str(payload.get("project_name") or ""),
                issue_title=str(payload.get("issue_title") or ""),
                status_name=str(payload.get("status_name") or ""),
                issue_url=str(payload.get("issue_url") or ""),
                status_changed=bool(payload.get("status_changed", True)),
            )
            return bool(result.get("success")) and result.get("action") not in {"deferred"}
        from services.obsidian.add_daily_action_updates import upsert_daily_action_update

        result = upsert_daily_action_update(
            str(payload.get("section_type") or "initiative"),
            str(payload.get("url") or item.get("payload_ref") or ""),
            str(payload.get("parent_name") or ""),
            str(payload.get("content") or ""),
        )
        return bool(result.get("success")) and result.get("action") not in {"deferred"}

    if isinstance(payload, dict) and (source == "readwise" or payload.get("text") or payload.get("title")):
        from services.obsidian.add_readwise_buffet import append_readwise_buffet

        result = append_readwise_buffet(payload)
        return result.get("action") not in {"deferred", "error", "kh_error"}

    logger.warning(
        "Deferred queue has no replay handler source=%s kind=%s ref=%s",
        source,
        kind,
        item.get("payload_ref"),
    )
    return False


def drain_deferred_batch(
    limit: int = 50,
    *,
    replay_fn: ReplayFn | None = None,
) -> dict[str, Any]:
    """Process up to ``limit`` queued items. Dead-letter after ``MAX_ATTEMPTS``.

    Selection is enqueue-time order, not journal date.
    """
    replay = replay_fn or _default_replay
    summary = {
        "selected": 0,
        "succeeded": 0,
        "failed": 0,
        "dead_lettered": 0,
        "processed": 0,
        "errors": [],
    }
    pending = list_deferred()
    batch = pending[: max(0, int(limit))]
    summary["selected"] = len(batch)

    for item in batch:
        field = item.get("_key")
        try:
            ok = bool(replay(item))
        except Exception as exc:
            logger.exception(
                "Deferred queue replay failed source=%s kind=%s ref=%s",
                item.get("source"),
                item.get("kind"),
                item.get("payload_ref"),
            )
            ok = False
            summary["errors"].append(f"{item.get('payload_ref')}: {exc}")

        if ok:
            if field:
                redis_client.hdel(DEFERRED_HASH_KEY, field)
            summary["succeeded"] += 1
            summary["processed"] += 1
            continue

        attempts = int(item.get("attempts") or 0) + 1
        item["attempts"] = attempts
        if attempts >= MAX_ATTEMPTS:
            _move_to_dead(item)
            summary["dead_lettered"] += 1
        else:
            _store_item(DEFERRED_HASH_KEY, item)
            logger.warning(
                "Deferred queue retry scheduled source=%s kind=%s ref=%s attempts=%s",
                item.get("source"),
                item.get("kind"),
                item.get("payload_ref"),
                attempts,
            )
        summary["failed"] += 1
        summary["processed"] += 1

    return summary
