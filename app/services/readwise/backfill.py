"""Repeatable Readwise highlight backfill into Obsidian daily journals."""

import logging
import os
from datetime import date, datetime

from dotenv import load_dotenv

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


def resolve_backfill_params(
    since: str | None = None,
    updated_after: str | None = None,
) -> tuple[str, str | None]:
    """Resolve since / updated_after from args, then env, then defaults."""
    resolved_since = since or os.getenv("READWISE_BACKFILL_SINCE") or DEFAULT_SINCE
    resolved_updated = updated_after
    if resolved_updated is None:
        env_updated = os.getenv("READWISE_BACKFILL_UPDATED_AFTER")
        resolved_updated = env_updated.strip() if env_updated else None
    elif resolved_updated == "":
        resolved_updated = None
    return resolved_since, resolved_updated


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
    now: datetime | None = None,
) -> dict:
    """Pull highlights from the export API and append them to daily journals.

    Parameters
    ----------
    since:
        ISO date. Highlights whose local (3am-rollover) date is before this
        are skipped. Default ``2024-08-13`` or ``READWISE_BACKFILL_SINCE``.
    updated_after:
        Optional ISO8601 timestamp passed through to the export API for
        incremental reruns (``READWISE_BACKFILL_UPDATED_AFTER``).
    """
    since, updated_after = resolve_backfill_params(since, updated_after)
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
    }

    try:
        since_date = parse_since_date(since)
    except ValueError:
        logger.error("Readwise backfill invalid since=%s", since)
        summary["errors"].append(f"Invalid since date: {since}")
        return summary

    logger.info(
        "Readwise backfill starting since=%s updated_after=%s",
        since_date.isoformat(),
        updated_after,
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
    logger.info(
        "Readwise backfill finished selected=%s inserted=%s replaced=%s "
        "skipped=%s skipped_missing_journal=%s files_written=%s errors=%s",
        summary["selected"],
        summary["inserted"],
        summary["replaced"],
        summary["skipped"],
        summary["skipped_missing_journal"],
        summary["files_written"],
        len(summary["errors"]),
    )
    if summary["errors"]:
        logger.error("Readwise backfill errors: %s", summary["errors"])
    return summary
