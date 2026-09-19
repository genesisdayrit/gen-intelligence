"""Reconcile provider protocol, registry, and v1 implementations.

Add a source later by implementing ``ReconcileProvider`` (or subclass
``StubProvider`` / ``BaseReconcileProvider``) and appending it to
``default_providers()``. The hourly runner walks that list with a shared
``last_reconcile_check_at`` watermark and a per-run batch budget.
Since-checks use ``ctx.since`` (``updated_at > watermark``), never a
today-only or same-calendar-day gate.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from services.obsidian.reconcile.queue import drain_deferred_batch

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileContext:
    """Shared inputs for one hourly (or manual) reconcile pass."""

    since: str | None
    run_started: str
    batch_size: int
    now: datetime


@runtime_checkable
class ReconcileProvider(Protocol):
    """One source the hourly job can drain or since-check."""

    name: str

    def reconcile(self, ctx: ReconcileContext) -> dict:
        """Process up to ``ctx.batch_size`` items with ``updated_at > ctx.since``."""


@dataclass
class BaseReconcileProvider:
    """Convenience base so new sources are a class + a registry line."""

    name: str

    def reconcile(self, ctx: ReconcileContext) -> dict:
        raise NotImplementedError


@dataclass
class StubProvider(BaseReconcileProvider):
    """Placeholder so adding share-link / DA / Todoist / Manus / Telegram is obvious."""

    reason: str = "Not yet wired; replace this stub with a real ReconcileProvider."

    def reconcile(self, ctx: ReconcileContext) -> dict:
        logger.info(
            "Reconcile stub %s skipped (since=%s): %s",
            self.name,
            ctx.since,
            self.reason,
        )
        return {
            "provider": self.name,
            "status": "stub",
            "processed": 0,
            "since": ctx.since,
            "note": self.reason,
        }


class DeferredDropboxProvider(BaseReconcileProvider):
    """Drain Redis items enqueued after a rev-safe immediate retry still deferred.

    Order is oldest ``enqueued_at`` first, then attempts / dead-letter.
    No journal-date or calendar-day gate — a deferred write from last
    week is still replayed.
    """

    name = "deferred_dropbox"

    def __init__(self) -> None:
        super().__init__(name=self.name)

    def reconcile(self, ctx: ReconcileContext) -> dict:
        summary = drain_deferred_batch(limit=ctx.batch_size)
        status = "ok"
        if summary.get("errors") and summary.get("succeeded", 0) == 0 and summary.get("selected", 0):
            status = "error"
        return {
            "provider": self.name,
            "status": status,
            "processed": summary.get("processed", 0),
            "since": ctx.since,
            **summary,
        }


def _stamp_highlight(payload: dict) -> dict:
    """Export rows already satisfy ``is_highlight_event`` via text+book_id."""
    if payload.get("event_type"):
        return payload
    return {**payload, "event_type": "readwise.highlight.created"}


def _stamp_document(payload: dict) -> dict:
    """List API docs have no webhook ``event_type``; stamp so KH writers run."""
    if payload.get("event_type"):
        return payload
    return {**payload, "event_type": "reader.any_document.created"}


class ReadwiseSinceCheckProvider(BaseReconcileProvider):
    """Pull highlights/docs with ``updated_after=ctx.since`` (shared watermark)."""

    name = "readwise"

    def __init__(self) -> None:
        super().__init__(name=self.name)

    def reconcile(self, ctx: ReconcileContext) -> dict:
        summary: dict = {
            "provider": self.name,
            "status": "ok",
            "processed": 0,
            "selected": 0,
            "inserted": 0,
            "skipped": 0,
            "deferred": 0,
            "errors": [],
            "since": ctx.since,
        }
        if not os.getenv("READWISE_TOKEN"):
            logger.info("Readwise reconcile skipped; READWISE_TOKEN not set")
            summary["status"] = "skipped"
            summary["errors"].append("READWISE_TOKEN not set")
            return summary

        from services.obsidian.add_readwise_buffet import append_readwise_buffet
        from services.readwise.export import iter_export_highlights
        from services.readwise.reader import iter_reader_documents

        try:
            highlights = [_stamp_highlight(p) for p in iter_export_highlights(updated_after=ctx.since)]
            documents = [_stamp_document(p) for p in iter_reader_documents(updated_after=ctx.since)]
        except Exception as exc:
            logger.exception("Readwise since-check list/export failed")
            summary["status"] = "error"
            summary["errors"].append(str(exc))
            return summary

        items = highlights + documents
        batch = items[: ctx.batch_size]
        summary["selected"] = len(batch)

        for payload in batch:
            try:
                result = append_readwise_buffet(payload, now=ctx.now)
            except Exception as exc:
                logger.exception("Readwise since-check write failed id=%s", payload.get("id"))
                summary["errors"].append(f"{payload.get('id')}: {exc}")
                continue
            action = result.get("action")
            if action == "deferred":
                summary["deferred"] += 1
            elif action in {"skipped", "skipped_missing_journal", "ignored"}:
                summary["skipped"] += 1
            elif action in {"inserted", "replaced", "created", "updated"}:
                summary["inserted"] += 1
            elif action in {"error", "kh_error"}:
                summary["errors"].append(f"{payload.get('id')}: {result.get('error') or action}")
            summary["processed"] += 1

        if summary["errors"] and summary["processed"] == 0:
            summary["status"] = "error"
        return summary


class GranolaSinceCheckProvider(BaseReconcileProvider):
    """Reuse incremental sync with the shared watermark as ``updated_after``.

    Passes ``ctx.since`` so this path is since-last-check, not Granola's
    empty-Redis 15m seed and not a journal-date filter.
    """

    name = "granola"

    def __init__(self) -> None:
        super().__init__(name=self.name)

    def reconcile(self, ctx: ReconcileContext) -> dict:
        from services.granola.sync import sync_granola_notes

        result = sync_granola_notes(updated_after=ctx.since, now=ctx.now)
        errors = list(result.get("errors") or [])
        status = "ok"
        if errors == ["GRANOLA_API_KEY not set"]:
            status = "skipped"
        elif errors:
            status = "error"
        processed = int(result.get("selected") or 0)
        return {
            "provider": self.name,
            "status": status,
            "processed": processed,
            "since": ctx.since,
            **result,
        }


def default_providers() -> list[ReconcileProvider]:
    """v1 registry. Append a new provider here (or replace a stub)."""
    return [
        DeferredDropboxProvider(),
        ReadwiseSinceCheckProvider(),
        GranolaSinceCheckProvider(),
        StubProvider("share_link"),
        StubProvider("daily_action"),
        StubProvider("todoist"),
        StubProvider("manus"),
        StubProvider("telegram"),
    ]
