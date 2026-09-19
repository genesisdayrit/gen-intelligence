"""Shared watermark + batched provider runner for missed Obsidian hub writes."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from config import redis_client
from services.obsidian.reconcile.providers import (
    ReconcileContext,
    ReconcileProvider,
    default_providers,
)
from services.obsidian.reconcile.queue import format_utc_iso

logger = logging.getLogger(__name__)

# Shared since-last-check cursor. Not a rolling 1h window and not a
# calendar-day gate. Subsequent runs use whatever gap actually elapsed
# (1h, 3h, or longer if the job was down).
WATERMARK_KEY = "obsidian_reconcile:last_reconcile_check_at"
BATCH_SIZE_PER_PROVIDER = 50
TOTAL_BATCH_CAP = 100
# First run only: empty Redis seeds ``updated_at > now−1h`` so we do not
# dump source history. After that, ``resolve_since`` is purely the stored
# watermark (or a manual ``?since=`` override).
BOOTSTRAP_LOOKBACK = timedelta(hours=1)


def utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def get_watermark() -> str | None:
    """Return ``last_reconcile_check_at`` (UTC ISO of last successful run start)."""
    try:
        value = redis_client.get(WATERMARK_KEY)
    except Exception:
        logger.exception("Reconcile failed to read watermark %s", WATERMARK_KEY)
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def set_watermark(value: str) -> bool:
    """Persist ``last_reconcile_check_at`` after a successful run."""
    try:
        redis_client.set(WATERMARK_KEY, value)
        return True
    except Exception:
        logger.exception("Reconcile failed to persist watermark %s", WATERMARK_KEY)
        return False


def resolve_since(
    since: str | None = None,
    *,
    now: datetime | None = None,
    stored: str | None = None,
) -> str:
    """Resolve the shared since-last-check cursor.

    Precedence: explicit ``since`` (manual ``?since=``) → stored
    ``last_reconcile_check_at`` → first-run bootstrap ``now−1h``.

    The 1h lookback is **not** the primary window. A stored watermark
    from 3h ago (job down) is used as-is. There is no today-only or
    same-calendar-day gate.
    """
    if since is not None and str(since).strip():
        return str(since).strip()
    if stored and str(stored).strip():
        return str(stored).strip()
    return format_utc_iso(utc_now(now) - BOOTSTRAP_LOOKBACK)


def _is_hard_error(result: dict) -> bool:
    return result.get("status") == "error"


def reconcile_missed_obsidian_writes(
    since: str | None = None,
    now: datetime | None = None,
    *,
    providers: Iterable[ReconcileProvider] | None = None,
    batch_size: int = BATCH_SIZE_PER_PROVIDER,
    total_cap: int = TOTAL_BATCH_CAP,
) -> dict:
    """Run every registered provider, then advance ``last_reconcile_check_at``.

    Providers since-check ``updated_at > last_reconcile_check_at`` (or the
    equivalent API cursor). ``since`` overrides that watermark for one
    pass (manual ``?since=``). The watermark is set to this run's start
    only when no required provider reported a hard error, so a down
    export API does not skip a window.

    Deferred-queue drain is independent of this cursor and of calendar
    day — oldest ``enqueued_at`` / attempts, not journal date.
    """
    started = utc_now(now)
    run_started = format_utc_iso(started)
    stored = get_watermark()
    resolved_since = resolve_since(since, now=started, stored=stored)
    registry = list(providers) if providers is not None else default_providers()

    summary: dict = {
        "since": resolved_since,
        "run_started": run_started,
        "watermark": stored,
        "watermark_advanced": False,
        "processed": 0,
        "providers": [],
        "errors": [],
    }

    remaining = max(0, int(total_cap))
    hard_error = False

    logger.info(
        "Obsidian reconcile starting since=%s run_started=%s providers=%s",
        resolved_since,
        run_started,
        [getattr(p, "name", type(p).__name__) for p in registry],
    )

    for provider in registry:
        name = getattr(provider, "name", type(provider).__name__)
        if remaining <= 0:
            skipped = {
                "provider": name,
                "status": "skipped_budget",
                "processed": 0,
                "since": resolved_since,
            }
            summary["providers"].append(skipped)
            continue
        cap = min(max(1, int(batch_size)), remaining)
        ctx = ReconcileContext(
            since=resolved_since,
            run_started=run_started,
            batch_size=cap,
            now=started,
        )
        try:
            result = provider.reconcile(ctx)
        except Exception as exc:
            logger.exception("Reconcile provider %s crashed", name)
            result = {
                "provider": name,
                "status": "error",
                "processed": 0,
                "since": resolved_since,
                "errors": [str(exc)],
            }
        if "provider" not in result:
            result["provider"] = name
        summary["providers"].append(result)
        processed = int(result.get("processed") or 0)
        summary["processed"] += processed
        remaining = max(0, remaining - processed)
        if result.get("errors"):
            summary["errors"].extend(f"{name}: {err}" for err in result["errors"])
        if _is_hard_error(result):
            hard_error = True

    if not hard_error and set_watermark(run_started):
        summary["watermark"] = run_started
        summary["watermark_advanced"] = True

    logger.info(
        "Obsidian reconcile finished since=%s watermark=%s advanced=%s "
        "processed=%s providers=%s errors=%s",
        summary["since"],
        summary["watermark"],
        summary["watermark_advanced"],
        summary["processed"],
        len(summary["providers"]),
        len(summary["errors"]),
    )
    return summary
