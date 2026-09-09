"""Manual Granola → Obsidian daily-journal backfill.

Default (no params): list every note the API returns by omitting
``updated_after`` — ``GET /v1/notes`` treats that filter as optional.
Does not use the incremental empty-Redis now−15m seed.

On success, advances the shared ``granola:notes:cursor`` to this run's
start so a later manual ``sync_granola_notes`` run continues from a
known-good point. Live notes arrive via ``POST /granola/webhook``.
Writes reuse ``format_granola_block`` / ``write_notes_by_journal``
(idempotent on Granola note id; upgrades legacy title-only bullets).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

from services.granola.sync import (
    format_utc_iso,
    get_stored_cursor,
    run_granola_notes_sync,
    utc_now,
)

load_dotenv()

logger = logging.getLogger(__name__)


def parse_since_date(value: str | date) -> date:
    """Parse an ISO date (YYYY-MM-DD) or ISO8601 datetime into a cutoff date."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1]
    if "T" in text:
        text = text.split("T", 1)[0]
    return date.fromisoformat(text)


def _parse_lookback_days(value) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def resolve_backfill_params(
    updated_after: str | None = None,
    lookback_days: int | str | None = None,
    since: str | None = None,
    now: datetime | None = None,
) -> tuple[str | None, int | None, str | None]:
    """Resolve list-notes ``updated_after`` / lookback / optional journal ``since``.

    Precedence for ``updated_after``:
    1. Explicit ``updated_after`` kwarg (empty string omits the API filter)
    2. ``GRANOLA_BACKFILL_UPDATED_AFTER``
    3. ``lookback_days`` kwarg or ``GRANOLA_BACKFILL_LOOKBACK_DAYS``
    4. Omit the filter (full history; API ``updated_after`` is optional)

    Does **not** consult ``granola:notes:cursor``, ``GRANOLA_NOTES_UPDATED_AFTER``,
    or the incremental now−15m seed.
    """
    resolved_since = since or os.getenv("GRANOLA_BACKFILL_SINCE") or None
    if resolved_since is not None:
        resolved_since = resolved_since.strip() or None

    if lookback_days is None:
        env_lookback = os.getenv("GRANOLA_BACKFILL_LOOKBACK_DAYS")
        lookback_days = env_lookback.strip() if env_lookback else None
    try:
        resolved_lookback = _parse_lookback_days(lookback_days)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid lookback_days: {lookback_days}") from exc

    if updated_after is not None:
        resolved_updated = updated_after.strip() if updated_after else None
        return resolved_updated, resolved_lookback, resolved_since

    env_updated = os.getenv("GRANOLA_BACKFILL_UPDATED_AFTER")
    if env_updated and env_updated.strip():
        return env_updated.strip(), resolved_lookback, resolved_since

    if resolved_lookback is not None:
        if resolved_lookback < 0:
            raise ValueError(f"lookback_days must be >= 0, got {resolved_lookback}")
        lookback_after = format_utc_iso(utc_now(now) - timedelta(days=resolved_lookback))
        return lookback_after, resolved_lookback, resolved_since

    return None, None, resolved_since


def backfill_granola_notes(
    updated_after: str | None = None,
    lookback_days: int | str | None = None,
    since: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Pull historical Granola notes and append summaries to daily journals.

    Parameters
    ----------
    updated_after:
        Optional ISO8601 timestamp passed through to ``GET /v1/notes``.
        Overrides lookback / env for this run. Empty string omits the
        filter (same as the no-param default).
    lookback_days:
        Optional integer. When ``updated_after`` is not explicit, set
        ``updated_after`` to now minus this many days (UTC). Also
        ``GRANOLA_BACKFILL_LOOKBACK_DAYS``.
    since:
        Optional ISO date. Notes whose local (3am-rollover) journal date
        is before this are skipped. No far-past default — Granola listing
        is update-cursor based; journal dating already uses meeting start
        / ``created_at``. Also ``GRANOLA_BACKFILL_SINCE``.
    """
    run_started = format_utc_iso(utc_now(now))
    stored_cursor = get_stored_cursor()

    summary = {
        "selected": 0,
        "inserted": 0,
        "skipped": 0,
        "skipped_missing_journal": 0,
        "files_written": 0,
        "errors": [],
        "since": since,
        "updated_after": updated_after,
        "cursor": stored_cursor,
        "lookback_days": None,
    }

    try:
        resolved_updated, resolved_lookback, resolved_since = resolve_backfill_params(
            updated_after,
            lookback_days=lookback_days,
            since=since,
            now=now,
        )
    except ValueError as exc:
        logger.error("Granola backfill invalid params: %s", exc)
        summary["errors"].append(str(exc))
        return summary

    summary["updated_after"] = resolved_updated
    summary["lookback_days"] = resolved_lookback
    summary["since"] = resolved_since

    since_date = None
    if resolved_since:
        try:
            since_date = parse_since_date(resolved_since)
        except ValueError:
            logger.error("Granola backfill invalid since=%s", resolved_since)
            summary["errors"].append(f"Invalid since date: {resolved_since}")
            return summary

    result = run_granola_notes_sync(
        resolved_updated_after=resolved_updated,
        now=now,
        run_started=run_started,
        stored_cursor=stored_cursor,
        since_date=since_date,
        log_label="Granola backfill",
    )
    result["since"] = resolved_since
    result["lookback_days"] = resolved_lookback
    return result
