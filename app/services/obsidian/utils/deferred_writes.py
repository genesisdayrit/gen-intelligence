"""Redis queue for Dropbox writes deferred after a rev-safe retry.

Event-driven writers (Readwise, later Granola/share-link) enqueue when
``upload_if_rev_matches`` / ``upload_new_file`` still defer after the
immediate re-download retry. The 15-minute reconcile job drains this
queue and re-applies the same idempotent writers.

Dedup is ``(source, payload_ref, target)`` so a burst of conflicts for
the same highlight/doc/note does not flood Redis.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from config import redis_client

logger = logging.getLogger(__name__)

QUEUE_KEY = "obsidian:deferred_writes"
PENDING_KEY = "obsidian:deferred_writes:pending"
DEAD_LETTER_KEY = "obsidian:deferred_writes:dead"
LAST_RECONCILE_KEY = "obsidian:last_reconcile_check_at"
LAST_READWISE_RECONCILE_KEY = "obsidian:last_readwise_reconcile_at"

SOURCE_READWISE = "readwise"
SOURCE_GRANOLA = "granola"

KIND_JOURNAL_HIGHLIGHT = "journal_highlight"
KIND_JOURNAL_DOCUMENT = "journal_document"
KIND_JOURNAL_WIKILINK = "journal_wikilink"
KIND_KH_TWEET = "kh_tweet"
KIND_KH_BOOK = "kh_book"
KIND_KH_ARTICLE = "kh_article"
KIND_KH_YOUTUBE = "kh_youtube"
KIND_GRANOLA_NOTE = "granola_note"

DEFAULT_DRAIN_LIMIT = 50
DEFAULT_MAX_ATTEMPTS = 5

ReplayFn = Callable[[dict], bool]


@dataclass(frozen=True)
class DeferredWriteContext:
    """Identity of a write that can be retried later from source payload."""

    source: str
    kind: str
    payload_ref: str
    target_path: str | None = None
    journal_date: str | None = None


def format_utc_iso(value: datetime | None = None) -> str:
    """Format a datetime as ISO8601 UTC with a Z suffix."""
    if value is None:
        value = datetime.now(timezone.utc)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def dedup_key(
    source: str,
    payload_ref: str,
    target_path: str | None = None,
    journal_date: str | None = None,
) -> str:
    """Stable Redis set member for ``(source, payload_ref, target)``."""
    target = (target_path or journal_date or "").strip()
    return f"{source}|{payload_ref}|{target}"


def _as_contexts(
    defer: DeferredWriteContext | Sequence[DeferredWriteContext] | None,
) -> list[DeferredWriteContext]:
    if defer is None:
        return []
    if isinstance(defer, DeferredWriteContext):
        return [defer]
    return [item for item in defer if item is not None]


def enqueue_deferred_write(
    *,
    source: str,
    kind: str,
    payload_ref: str,
    target_path: str | None = None,
    journal_date: str | None = None,
    enqueued_at: str | None = None,
    attempts: int = 0,
    client=None,
) -> bool:
    """Push one deferred write. Returns True when newly queued.

    Dedupes on ``(source, payload_ref, target)``. Redis errors fail open
    so a brief outage cannot fail the webhook/write path.
    """
    ref = str(payload_ref or "").strip()
    src = str(source or "").strip()
    if not ref or not src:
        logger.info(
            "Deferred write skip enqueue (missing source or payload_ref) "
            "source=%s kind=%s ref=%s",
            source,
            kind,
            payload_ref,
        )
        return False

    item = {
        "source": src,
        "kind": str(kind or "").strip(),
        "payload_ref": ref,
        "target_path": (target_path or "").strip() or None,
        "journal_date": (journal_date or "").strip() or None,
        "enqueued_at": enqueued_at or format_utc_iso(),
        "attempts": int(attempts),
    }
    key = dedup_key(src, ref, item["target_path"], item["journal_date"])
    redis = client if client is not None else redis_client
    try:
        added = redis.sadd(PENDING_KEY, key)
        if not added:
            logger.info(
                "Deferred write already queued source=%s kind=%s ref=%s target=%s",
                src,
                item["kind"],
                ref,
                item["target_path"] or item["journal_date"],
            )
            return False
        redis.rpush(QUEUE_KEY, json.dumps(item))
    except Exception:
        logger.exception(
            "Deferred write enqueue failed source=%s kind=%s ref=%s",
            src,
            item["kind"],
            ref,
        )
        return False
    logger.info(
        "Deferred write enqueued source=%s kind=%s ref=%s target=%s",
        src,
        item["kind"],
        ref,
        item["target_path"] or item["journal_date"],
    )
    return True


def enqueue_deferred_contexts(
    defer: DeferredWriteContext | Sequence[DeferredWriteContext] | None,
    *,
    client=None,
) -> int:
    """Enqueue one or more contexts. Returns how many were newly queued."""
    queued = 0
    for ctx in _as_contexts(defer):
        if enqueue_deferred_write(
            source=ctx.source,
            kind=ctx.kind,
            payload_ref=ctx.payload_ref,
            target_path=ctx.target_path,
            journal_date=ctx.journal_date,
            client=client,
        ):
            queued += 1
    return queued


def get_watermark(key: str = LAST_RECONCILE_KEY, *, client=None) -> str | None:
    """Return a persisted ISO8601 watermark, or None if missing/unreadable."""
    redis = client if client is not None else redis_client
    try:
        value = redis.get(key)
    except Exception:
        logger.exception("Failed to read watermark %s", key)
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def set_watermark(value: str, key: str = LAST_RECONCILE_KEY, *, client=None) -> bool:
    """Persist an ISO8601 watermark. Returns True on success."""
    redis = client if client is not None else redis_client
    try:
        redis.set(key, value)
        return True
    except Exception:
        logger.exception("Failed to persist watermark %s", key)
        return False


def drain_deferred_queue(
    replay_fn: ReplayFn,
    *,
    limit: int = DEFAULT_DRAIN_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    client=None,
) -> dict:
    """Pop up to ``limit`` items, replay each, requeue or dead-letter.

    ``replay_fn`` returns True when the write landed or is safely skippable
    (already present / source gone). False bumps ``attempts``. After
    ``max_attempts`` failures the item is logged and moved to the dead-letter
    list.
    """
    redis = client if client is not None else redis_client
    summary = {
        "processed": 0,
        "succeeded": 0,
        "requeued": 0,
        "dead_lettered": 0,
        "errors": [],
    }
    for _ in range(max(0, int(limit))):
        try:
            raw = redis.lpop(QUEUE_KEY)
        except Exception as exc:
            logger.exception("Deferred write drain failed to pop")
            summary["errors"].append(str(exc))
            break
        if raw is None:
            break

        summary["processed"] += 1
        try:
            item = json.loads(raw) if not isinstance(raw, dict) else raw
        except Exception as exc:
            logger.exception("Deferred write drain skipped unreadable item")
            summary["errors"].append(str(exc))
            continue

        key = dedup_key(
            str(item.get("source") or ""),
            str(item.get("payload_ref") or ""),
            item.get("target_path"),
            item.get("journal_date"),
        )
        try:
            redis.srem(PENDING_KEY, key)
        except Exception:
            logger.exception("Deferred write drain failed to clear pending %s", key)

        try:
            ok = bool(replay_fn(item))
        except Exception as exc:
            logger.exception(
                "Deferred write replay raised source=%s kind=%s ref=%s",
                item.get("source"),
                item.get("kind"),
                item.get("payload_ref"),
            )
            summary["errors"].append(str(exc))
            ok = False

        if ok:
            summary["succeeded"] += 1
            continue

        attempts = int(item.get("attempts") or 0) + 1
        item["attempts"] = attempts
        if attempts >= max_attempts:
            logger.error(
                "Deferred write dead-lettered after %s attempts "
                "source=%s kind=%s ref=%s target=%s",
                attempts,
                item.get("source"),
                item.get("kind"),
                item.get("payload_ref"),
                item.get("target_path") or item.get("journal_date"),
            )
            try:
                redis.rpush(DEAD_LETTER_KEY, json.dumps(item))
            except Exception:
                logger.exception("Deferred write failed to write dead-letter")
            summary["dead_lettered"] += 1
            continue

        if enqueue_deferred_write(
            source=str(item.get("source") or ""),
            kind=str(item.get("kind") or ""),
            payload_ref=str(item.get("payload_ref") or ""),
            target_path=item.get("target_path"),
            journal_date=item.get("journal_date"),
            enqueued_at=item.get("enqueued_at"),
            attempts=attempts,
            client=redis,
        ):
            summary["requeued"] += 1
        else:
            summary["errors"].append(
                f"requeue failed for {item.get('source')}:{item.get('payload_ref')}"
            )
    return summary


def context_from_item(item: dict) -> DeferredWriteContext:
    """Rebuild a context dataclass from a queued JSON item."""
    return DeferredWriteContext(
        source=str(item.get("source") or ""),
        kind=str(item.get("kind") or ""),
        payload_ref=str(item.get("payload_ref") or ""),
        target_path=item.get("target_path"),
        journal_date=item.get("journal_date"),
    )


def item_as_dict(ctx: DeferredWriteContext, **extra) -> dict:
    data = asdict(ctx)
    data.update(extra)
    return data
