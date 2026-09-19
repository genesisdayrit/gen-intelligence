"""Scheduled reconcile: replay deferred Obsidian writes + light source since-check.

Job id: ``reconcile_deferred_obsidian_writes``. Every 15 minutes (and via
``POST /scheduler/jobs/reconcile_deferred_obsidian_writes/run``):

1. Stamp Redis ``obsidian:last_reconcile_check_at``.
2. Drain the deferred-write queue (cap per run) and re-apply source writers.
3. Readwise since-check: export highlights + Reader documents updated since
   the last successful Readwise watermark (else last reconcile, else 15m).
4. Granola incremental ``sync_granola_notes`` (uses its own Redis cursor;
   idempotent on ``<!-- granola:not_… -->``). Granola journal writes still
   use overwrite until they adopt the rev-safe helper + enqueue hook.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from services.granola.sync import sync_granola_notes
from services.obsidian.add_readwise_buffet import (
    append_readwise_buffet,
    append_wikilink_to_journal_buffet,
    is_document_event,
    is_highlight_event,
    write_highlights_by_journal,
)
from services.obsidian.utils.deferred_writes import (
    DEFAULT_DRAIN_LIMIT,
    KIND_GRANOLA_NOTE,
    KIND_JOURNAL_DOCUMENT,
    KIND_JOURNAL_WIKILINK,
    LAST_READWISE_RECONCILE_KEY,
    LAST_RECONCILE_KEY,
    SOURCE_GRANOLA,
    SOURCE_READWISE,
    drain_deferred_queue,
    format_utc_iso,
    get_watermark,
    set_watermark,
)
from services.readwise.export import get_highlight, iter_export_highlights
from services.readwise.reader import get_document, iter_reader_documents

logger = logging.getLogger(__name__)

SEED_LOOKBACK_MINUTES = 15


def utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def seed_updated_after(now: datetime | None = None) -> str:
    """ISO8601 UTC seed used when Redis has no reconcile watermark."""
    return format_utc_iso(utc_now(now) - timedelta(minutes=SEED_LOOKBACK_MINUTES))


def resolve_readwise_updated_after(
    *,
    now: datetime | None = None,
    previous_reconcile: str | None = None,
    last_readwise: str | None = None,
    client=None,
) -> str:
    """Prefer last successful Readwise watermark, else last reconcile, else 15m."""
    if last_readwise is None:
        last_readwise = get_watermark(LAST_READWISE_RECONCILE_KEY, client=client)
    if last_readwise:
        return last_readwise
    if previous_reconcile:
        return previous_reconcile
    return seed_updated_after(now)


def _as_webhook_highlight(payload: dict) -> dict:
    if is_highlight_event(payload):
        return payload
    return {**payload, "event_type": "readwise.highlight.created"}


def _as_webhook_document(payload: dict) -> dict:
    if is_document_event(payload) or is_highlight_event(payload):
        return payload
    return {**payload, "event_type": "reader.any_document.created"}


def _action_is_failure(action: object) -> bool:
    return action in {"deferred", "error", "kh_error"}


def fetch_readwise_payload(kind: str, payload_ref: str) -> dict | None:
    """Re-fetch a highlight or Reader document for queue replay."""
    if kind == KIND_JOURNAL_DOCUMENT:
        document = get_document(payload_ref)
        if document is None:
            return None
        return _as_webhook_document(document)
    highlight = get_highlight(payload_ref)
    if highlight is None:
        return None
    return _as_webhook_highlight(highlight)


def replay_deferred_item(item: dict) -> bool:
    """Re-apply the webhook/sync writer for one queued item.

    Returns True when the write succeeded, was already present, or the
    source payload is gone (drop the item). False keeps it for another try.
    """
    source = str(item.get("source") or "")
    kind = str(item.get("kind") or "")
    ref = str(item.get("payload_ref") or "").strip()
    if not source or not ref:
        logger.warning("Deferred write drop (missing source/ref): %s", item)
        return True

    if source == SOURCE_READWISE:
        if kind == KIND_JOURNAL_WIKILINK:
            journal_date = item.get("journal_date")
            if not journal_date:
                journal_date = _journal_date_from_target(item.get("target_path"))
            if not journal_date:
                logger.warning(
                    "Deferred wikilink missing journal_date ref=%s", ref
                )
                return False
            result = append_wikilink_to_journal_buffet(ref, journal_date)
            return not _action_is_failure(result.get("action"))

        payload = fetch_readwise_payload(kind, ref)
        if payload is None:
            logger.info(
                "Deferred Readwise payload gone; dropping source=%s kind=%s ref=%s",
                source,
                kind,
                ref,
            )
            return True
        result = append_readwise_buffet(payload)
        return not _action_is_failure(result.get("action"))

    if source == SOURCE_GRANOLA or kind == KIND_GRANOLA_NOTE:
        from services.granola.client import GranolaNoteNotFound, get_note
        from services.granola.sync import write_notes_by_journal

        try:
            note = get_note(ref)
        except GranolaNoteNotFound:
            logger.info("Deferred Granola note %s 404; dropping", ref)
            return True
        except Exception:
            logger.exception("Deferred Granola note fetch failed ref=%s", ref)
            return False
        if not isinstance(note, dict):
            return False
        result = write_notes_by_journal([note], raise_errors=False)
        return not result.get("errors")

    logger.warning(
        "Deferred write unknown source=%s kind=%s ref=%s", source, kind, ref
    )
    return False


def _journal_date_from_target(path: object) -> str | None:
    text = str(path or "").strip()
    if not text:
        return None
    name = text.rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name


def reconcile_readwise_since(
    updated_after: str,
    *,
    now: datetime | None = None,
) -> dict:
    """Idempotent highlight + Reader document writers since ``updated_after``."""
    summary = {
        "updated_after": updated_after,
        "highlights": {
            "selected": 0,
            "inserted": 0,
            "replaced": 0,
            "skipped": 0,
            "skipped_missing_journal": 0,
            "files_written": 0,
            "deferred": 0,
            "errors": [],
        },
        "documents": {"selected": 0, "ok": 0, "skipped": 0, "errors": []},
        "errors": [],
    }

    try:
        highlights = list(iter_export_highlights(updated_after=updated_after))
    except Exception as exc:
        logger.exception("Readwise reconcile export failed")
        summary["errors"].append(str(exc))
        return summary

    highlight_result = write_highlights_by_journal(
        highlights, now=now, raise_errors=False
    )
    for key in (
        "selected",
        "inserted",
        "replaced",
        "skipped",
        "skipped_missing_journal",
        "files_written",
        "deferred",
        "errors",
    ):
        if key in highlight_result:
            summary["highlights"][key] = highlight_result[key]
    summary["errors"].extend(highlight_result.get("errors") or [])

    try:
        documents = list(iter_reader_documents(updated_after=updated_after))
    except Exception as exc:
        logger.exception("Readwise reconcile Reader list failed")
        summary["errors"].append(str(exc))
        return summary

    for document in documents:
        summary["documents"]["selected"] += 1
        payload = _as_webhook_document(document)
        try:
            result = append_readwise_buffet(payload, now=now)
        except Exception as exc:
            logger.exception(
                "Readwise reconcile document write failed id=%s",
                document.get("id"),
            )
            summary["documents"]["errors"].append(str(exc))
            summary["errors"].append(str(exc))
            continue
        action = result.get("action")
        if _action_is_failure(action):
            summary["documents"]["errors"].append(
                f"{document.get('id')}: {action}"
            )
            if result.get("error"):
                summary["errors"].append(str(result["error"]))
        elif action in {"skipped", "ignored", "skipped_missing_journal"}:
            summary["documents"]["skipped"] += 1
        else:
            summary["documents"]["ok"] += 1
    return summary


def reconcile_granola_since(
    updated_after: str | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """Call incremental Granola sync when an API key (and cursor) exist.

    ``sync_granola_notes`` is idempotent: existing ``<!-- granola:not_… -->``
    blocks are skipped. The parked year-2099 poll stays as a manual trigger;
    this job is the scheduled safety net for webhook misses.
    """
    if not os.getenv("GRANOLA_API_KEY"):
        logger.info("Granola reconcile skipped (GRANOLA_API_KEY not set)")
        return {"skipped": True, "reason": "GRANOLA_API_KEY not set"}
    return sync_granola_notes(updated_after=updated_after, now=now)


def reconcile_deferred_obsidian_writes(
    now: datetime | None = None,
    *,
    drain_limit: int = DEFAULT_DRAIN_LIMIT,
    client=None,
) -> dict:
    """Run one reconcile pass: watermark, drain queue, Readwise, Granola."""
    run_started_dt = utc_now(now)
    run_started = format_utc_iso(run_started_dt)
    previous = get_watermark(LAST_RECONCILE_KEY, client=client)
    set_watermark(run_started, LAST_RECONCILE_KEY, client=client)

    logger.info(
        "Reconcile deferred Obsidian writes starting previous=%s now=%s",
        previous,
        run_started,
    )

    queue = drain_deferred_queue(
        replay_deferred_item,
        limit=drain_limit,
        client=client,
    )

    readwise_after = resolve_readwise_updated_after(
        now=run_started_dt,
        previous_reconcile=previous,
        client=client,
    )
    readwise = reconcile_readwise_since(readwise_after, now=now)
    if not readwise.get("errors"):
        set_watermark(run_started, LAST_READWISE_RECONCILE_KEY, client=client)

    granola = reconcile_granola_since(now=now)

    summary = {
        "checked_at": run_started,
        "previous_check_at": previous,
        "queue": queue,
        "readwise": readwise,
        "granola": granola,
    }
    logger.info(
        "Reconcile deferred Obsidian writes finished checked_at=%s "
        "queue_processed=%s queue_succeeded=%s readwise_highlights=%s "
        "readwise_documents=%s granola_selected=%s",
        run_started,
        queue.get("processed"),
        queue.get("succeeded"),
        (readwise.get("highlights") or {}).get("selected"),
        (readwise.get("documents") or {}).get("selected"),
        granola.get("selected") if isinstance(granola, dict) else None,
    )
    return summary
