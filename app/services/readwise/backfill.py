"""Repeatable Readwise highlight backfill into Obsidian daily journals."""

import logging
import os
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

from config import redis_client
from services.obsidian.add_readwise_buffet import (
    highlight_local_datetime,
    write_highlights_by_journal,
)
from services.obsidian.utils.date_helpers import get_effective_date
from services.readwise.export import iter_export_highlights

load_dotenv()

logger = logging.getLogger(__name__)

# First day of the unbroken daily journal streak.
DEFAULT_SINCE = "2024-08-13"

# Seeded after the 2026-08-23 live today-only export+write (~9:00pm PT).
# Empty Redis must not fall back to a full export from DEFAULT_SINCE.
FIRST_CURSOR = "2026-08-24T04:00:00Z"
CURSOR_REDIS_KEY = "readwise:backfill:cursor"


def parse_since_date(value: str | date) -> date:
    """Parse an ISO date (YYYY-MM-DD) or ISO8601 datetime into a local cutoff date."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1]
    if "T" in text:
        text = text.split("T", 1)[0]
    return date.fromisoformat(text)


def format_utc_iso(value: datetime) -> str:
    """Format a datetime as ISO8601 UTC with a Z suffix."""
    if value.tzinfo is None:
        utc = value.replace(tzinfo=timezone.utc)
    else:
        utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now(now: datetime | None = None) -> datetime:
    """UTC instant for lookback / cursor. ``now`` is converted when provided."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def get_stored_cursor() -> str | None:
    """Return the persisted last-run cursor, or None if missing/unreadable."""
    try:
        value = redis_client.get(CURSOR_REDIS_KEY)
    except Exception:
        logger.exception("Readwise backfill failed to read cursor from Redis")
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def set_stored_cursor(value: str) -> bool:
    """Persist the last-run cursor. Returns True on success."""
    try:
        redis_client.set(CURSOR_REDIS_KEY, value)
        return True
    except Exception:
        logger.exception("Readwise backfill failed to persist cursor to Redis")
        return False


def _parse_lookback_days(value) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def resolve_backfill_params(
    since: str | None = None,
    updated_after: str | None = None,
    lookback_days: int | str | None = None,
    now: datetime | None = None,
    stored_cursor: str | None = None,
) -> tuple[str, str | None, int | None]:
    """Resolve since / updated_after / lookback from args, env, cursor, then seed.

    Precedence for ``updated_after``:
    1. Explicit ``updated_after`` kwarg (empty string clears the filter)
    2. ``READWISE_BACKFILL_UPDATED_AFTER``
    3. ``lookback_days`` kwarg or ``READWISE_BACKFILL_LOOKBACK_DAYS``
    4. Stored Redis cursor
    5. ``FIRST_CURSOR`` seed (never a full export from ``DEFAULT_SINCE``)
    """
    resolved_since = since or os.getenv("READWISE_BACKFILL_SINCE") or DEFAULT_SINCE

    if lookback_days is None:
        env_lookback = os.getenv("READWISE_BACKFILL_LOOKBACK_DAYS")
        lookback_days = env_lookback.strip() if env_lookback else None
    try:
        resolved_lookback = _parse_lookback_days(lookback_days)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid lookback_days: {lookback_days}") from exc

    if updated_after is not None:
        resolved_updated = updated_after.strip() if updated_after else None
        return resolved_since, resolved_updated, resolved_lookback

    env_updated = os.getenv("READWISE_BACKFILL_UPDATED_AFTER")
    if env_updated and env_updated.strip():
        return resolved_since, env_updated.strip(), resolved_lookback

    if resolved_lookback is not None:
        if resolved_lookback < 0:
            raise ValueError(f"lookback_days must be >= 0, got {resolved_lookback}")
        lookback_after = format_utc_iso(utc_now(now) - timedelta(days=resolved_lookback))
        return resolved_since, lookback_after, resolved_lookback

    resolved_updated = (stored_cursor or "").strip() or FIRST_CURSOR
    return resolved_since, resolved_updated, None


def highlight_effective_date(payload: dict, now: datetime | None = None) -> date:
    """Journal calendar date for a highlight (SYSTEM_TIMEZONE + 3am rollover)."""
    local = highlight_local_datetime(payload, now=now)
    return get_effective_date(local).date()


def select_highlights(
    payloads: list[dict],
    since: str | date,
    now: datetime | None = None,
) -> list[dict]:
    """Keep highlights whose local journal date is on or after ``since``."""
    since_date = parse_since_date(since)
    selected = []
    for payload in payloads:
        if highlight_effective_date(payload, now=now) < since_date:
            continue
        selected.append(payload)
    return selected


def backfill_readwise_highlights(
    since: str | None = None,
    updated_after: str | None = None,
    lookback_days: int | str | None = None,
    now: datetime | None = None,
) -> dict:
    """Pull highlights from the export API and append them to daily journals.

    Parameters
    ----------
    since:
        ISO date. Highlights whose local (3am-rollover) date is before this
        are skipped. Default ``2024-08-13`` or ``READWISE_BACKFILL_SINCE``.
    updated_after:
        Optional ISO8601 timestamp passed through to the export API.
        Overrides the stored cursor / seed / lookback for this run
        (``READWISE_BACKFILL_UPDATED_AFTER``).
    lookback_days:
        Optional integer. When ``updated_after`` is not explicit, set
        ``updated_after`` to now minus this many days (UTC). Also
        ``READWISE_BACKFILL_LOOKBACK_DAYS``. Wins over the stored cursor.
    """
    run_started = format_utc_iso(utc_now(now))
    stored_cursor = get_stored_cursor()
    effective_cursor = stored_cursor or FIRST_CURSOR

    summary = {
        "selected": 0,
        "inserted": 0,
        "replaced": 0,
        "skipped": 0,
        "skipped_missing_journal": 0,
        "files_written": 0,
        "errors": [],
        "since": since,
        "updated_after": updated_after,
        "cursor": effective_cursor,
        "lookback_days": None,
    }

    try:
        since, updated_after, resolved_lookback = resolve_backfill_params(
            since,
            updated_after,
            lookback_days=lookback_days,
            now=now,
            stored_cursor=stored_cursor,
        )
    except ValueError as exc:
        logger.error("Readwise backfill invalid params: %s", exc)
        summary["errors"].append(str(exc))
        return summary

    summary["since"] = since
    summary["updated_after"] = updated_after
    summary["lookback_days"] = resolved_lookback

    try:
        since_date = parse_since_date(since)
    except ValueError:
        logger.error("Readwise backfill invalid since=%s", since)
        summary["errors"].append(f"Invalid since date: {since}")
        return summary

    logger.info(
        "Readwise backfill starting since=%s updated_after=%s cursor=%s lookback_days=%s",
        since_date.isoformat(),
        updated_after,
        effective_cursor,
        resolved_lookback,
    )

    try:
        payloads = list(iter_export_highlights(updated_after=updated_after))
    except Exception as exc:
        logger.exception("Readwise export failed")
        summary["errors"].append(str(exc))
        return summary

    selected = select_highlights(payloads, since_date, now=now)
    result = write_highlights_by_journal(selected, now=now, raise_errors=False)
    summary.update(
        {
            "selected": result["selected"],
            "inserted": result["inserted"],
            "replaced": result["replaced"],
            "skipped": result["skipped"],
            "skipped_missing_journal": result["skipped_missing_journal"],
            "files_written": result["files_written"],
            "errors": result["errors"],
        }
    )

    if summary["errors"]:
        logger.error("Readwise backfill errors: %s", summary["errors"])
        logger.info(
            "Readwise backfill finished without advancing cursor=%s "
            "updated_after=%s selected=%s inserted=%s replaced=%s "
            "skipped=%s skipped_missing_journal=%s files_written=%s errors=%s",
            summary["cursor"],
            summary["updated_after"],
            summary["selected"],
            summary["inserted"],
            summary["replaced"],
            summary["skipped"],
            summary["skipped_missing_journal"],
            summary["files_written"],
            len(summary["errors"]),
        )
        return summary

    if set_stored_cursor(run_started):
        summary["cursor"] = run_started
    logger.info(
        "Readwise backfill finished selected=%s inserted=%s replaced=%s "
        "skipped=%s skipped_missing_journal=%s files_written=%s errors=%s "
        "updated_after=%s cursor=%s",
        summary["selected"],
        summary["inserted"],
        summary["replaced"],
        summary["skipped"],
        summary["skipped_missing_journal"],
        summary["files_written"],
        len(summary["errors"]),
        summary["updated_after"],
        summary["cursor"],
    )
    return summary
