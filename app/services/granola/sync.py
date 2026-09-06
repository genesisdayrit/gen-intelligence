"""Scheduled Granola → Obsidian daily-journal sync.

Pulls notes updated since a Redis last-run cursor and appends each under
``### Transcript Notes`` on the matching daily journal (3am local rollover).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

import dropbox
from dotenv import load_dotenv

from config import SYSTEM_TZ, redis_client
from services.granola.client import (
    GranolaAPIError,
    GranolaNoteNotFound,
    get_note,
    iter_notes,
)
from services.obsidian.add_readwise_buffet import (
    _buffet_bullet_lines,
    _get_dropbox_client,
    _get_file_content,
    _insert_heading_bullet,
    _nonempty,
    _resolve_journal_folder,
    _section_bounds,
    journal_filename,
    parse_highlight_datetime,
)
from services.obsidian.utils.date_helpers import get_effective_date

load_dotenv()

logger = logging.getLogger(__name__)

CURSOR_REDIS_KEY = "granola:notes:cursor"
TRANSCRIPT_NOTES_HEADER = "### Transcript Notes"
SEED_LOOKBACK_MINUTES = 15


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
        logger.exception("Granola sync failed to read cursor from Redis")
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
        logger.exception("Granola sync failed to persist cursor to Redis")
        return False


def seed_updated_after(now: datetime | None = None) -> str:
    """ISO8601 UTC seed used when Redis has no cursor.

    Defaults to now−15m so the first run does not dump the historical library.
    Override lookback with ``GRANOLA_SEED_LOOKBACK_MINUTES``.
    """
    raw = os.getenv("GRANOLA_SEED_LOOKBACK_MINUTES")
    minutes = SEED_LOOKBACK_MINUTES
    if raw and raw.strip():
        minutes = int(raw)
    if minutes < 0:
        raise ValueError(f"GRANOLA_SEED_LOOKBACK_MINUTES must be >= 0, got {minutes}")
    return format_utc_iso(utc_now(now) - timedelta(minutes=minutes))


def resolve_updated_after(
    updated_after: str | None = None,
    now: datetime | None = None,
    stored_cursor: str | None = None,
) -> str:
    """Resolve the list-notes ``updated_after`` filter.

    Precedence:
    1. Explicit ``updated_after`` kwarg
    2. ``GRANOLA_NOTES_UPDATED_AFTER``
    3. Stored Redis cursor
    4. Seed (now−15m, or ``GRANOLA_SEED_LOOKBACK_MINUTES``)
    """
    if updated_after is not None:
        text = updated_after.strip()
        if text:
            return text
    env_updated = os.getenv("GRANOLA_NOTES_UPDATED_AFTER")
    if env_updated and env_updated.strip():
        return env_updated.strip()
    if stored_cursor and stored_cursor.strip():
        return stored_cursor.strip()
    return seed_updated_after(now)


def _calendar_event(note: dict) -> dict:
    event = note.get("calendar_event")
    if isinstance(event, dict):
        return event
    return {}


def meeting_start_value(note: dict) -> object:
    """Meeting start when the API/object provides one."""
    event = _calendar_event(note)
    return (
        note.get("meeting_start")
        or note.get("meetingStartAt")
        or event.get("scheduled_start_time")
        or event.get("scheduledStartTime")
    )


def note_local_datetime(note: dict, now: datetime | None = None) -> datetime:
    """Local time for journal dating: meeting start, else created_at."""
    for value in (meeting_start_value(note), note.get("created_at")):
        parsed = parse_highlight_datetime(value)
        if parsed is not None:
            return parsed
    if now is None:
        return datetime.now(SYSTEM_TZ)
    if now.tzinfo is None:
        return SYSTEM_TZ.localize(now)
    return now.astimezone(SYSTEM_TZ)


def note_effective_date(note: dict, now: datetime | None = None) -> date:
    """Journal calendar date (SYSTEM_TIMEZONE + 3am rollover)."""
    return get_effective_date(note_local_datetime(note, now=now)).date()


def note_journal_path(
    journal_folder_path: str,
    note: dict,
    now: datetime | None = None,
) -> str:
    local = note_local_datetime(note, now=now)
    return f"{journal_folder_path}/{journal_filename(get_effective_date(local))}"


def note_id(note: dict) -> str | None:
    return _nonempty(note.get("id"))


def note_web_url(note: dict) -> str | None:
    for key in ("web_url", "url"):
        text = _nonempty(note.get(key))
        if text and text.startswith(("http://", "https://")):
            return text
    return None


def format_granola_bullet(note: dict) -> str | None:
    """Idempotent Transcript Notes line: title + Granola note id.

    Prefers ``- [Title](web_url) granola:not_…`` when the API provides a URL.
    Otherwise ``- Title granola:not_…``.
    """
    nid = note_id(note)
    if not nid:
        return None
    title = _nonempty(note.get("title")) or "Untitled"
    url = note_web_url(note)
    if url:
        safe_title = title.replace("[", "\\[").replace("]", "\\]")
        return f"- [{safe_title}]({url}) granola:{nid}"
    return f"- {title} granola:{nid}"


def granola_dedup_keys(note: dict) -> list[str]:
    """Dedup on the Granola note id (``not_…`` / ``granola:not_…``)."""
    nid = note_id(note)
    if not nid:
        return []
    return [f"granola:{nid}", nid]


def insert_transcript_notes_bullet(
    content: str,
    bullet: str,
    keys: list[str] | None = None,
) -> tuple[str, str]:
    """Insert ``bullet`` under ``### Transcript Notes``. Returns (content, action).

    Existing heading is reused in place (not moved). Missing heading is
    created at EOF.
    """
    lines = content.split("\n")
    header_idx, _ignored_end = _section_bounds(lines, TRANSCRIPT_NOTES_HEADER)
    if header_idx is not None:
        return _insert_heading_bullet(content, TRANSCRIPT_NOTES_HEADER, bullet, keys)

    bullet_lines = _buffet_bullet_lines(bullet)
    updated = list(lines)
    while updated and updated[-1] == "":
        updated.pop()
    if updated and updated[-1].strip():
        updated.append("")
    updated.extend([TRANSCRIPT_NOTES_HEADER, *bullet_lines, ""])
    return "\n".join(updated), "inserted"


def _note_already_has_detail(note: dict) -> bool:
    return bool(note_web_url(note) or meeting_start_value(note) or note.get("calendar_event"))


def hydrate_note(note: dict) -> dict | None:
    """Merge GET /v1/notes/{id} for web_url + meeting start.

    Returns None when the note 404s (do not invent it). List payloads that
    already carry detail fields skip the extra GET.
    """
    nid = note_id(note)
    if not nid:
        logger.warning("Granola note missing id; skipping")
        return None
    if _note_already_has_detail(note):
        return note
    try:
        detail = get_note(nid)
    except GranolaNoteNotFound:
        logger.warning("Granola note %s 404; skipping (not inventing)", nid)
        return None
    if not isinstance(detail, dict):
        return note
    return {**note, **detail}


def _empty_write_summary(selected: int = 0) -> dict:
    return {
        "selected": selected,
        "inserted": 0,
        "skipped": 0,
        "skipped_missing_journal": 0,
        "files_written": 0,
        "errors": [],
        "paths": [],
    }


def write_notes_by_journal(
    notes: list[dict],
    now: datetime | None = None,
    raise_errors: bool = False,
) -> dict:
    """Append notes grouped by journal file (one download/upload per day).

    Missing journal files are skipped — never created, never dumped onto today.
    """
    summary = _empty_write_summary(selected=len(notes))
    if not notes:
        return summary

    dbx = _get_dropbox_client()
    journal_folder = _resolve_journal_folder(dbx)

    by_path: dict[str, list[dict]] = {}
    for note in notes:
        path = note_journal_path(journal_folder, note, now=now)
        by_path.setdefault(path, []).append(note)

    for file_path, group in by_path.items():
        summary["paths"].append(file_path)
        try:
            try:
                content = _get_file_content(dbx, file_path)
            except FileNotFoundError:
                logger.warning(
                    "Granola sync skipped; journal not found (will not create or write today): %s",
                    file_path,
                )
                summary["skipped_missing_journal"] += len(group)
                continue

            original = content
            file_counts = {"inserted": 0, "skipped": 0}
            for note in group:
                bullet = format_granola_bullet(note)
                if not bullet:
                    continue
                content, action = insert_transcript_notes_bullet(
                    content, bullet, granola_dedup_keys(note)
                )
                if action in file_counts:
                    file_counts[action] += 1
                    summary[action] += 1

            if content != original:
                dbx.files_upload(
                    content.encode("utf-8"),
                    file_path,
                    mode=dropbox.files.WriteMode.overwrite,
                )
                summary["files_written"] += 1
                logger.info(
                    "Granola sync wrote path=%s inserted=%s skipped=%s",
                    file_path,
                    file_counts["inserted"],
                    file_counts["skipped"],
                )
            elif file_counts["skipped"]:
                logger.info("Granola sync skipped (duplicate) path=%s", file_path)
        except Exception as exc:
            logger.exception("Granola sync failed for %s", file_path)
            if raise_errors:
                raise
            summary["errors"].append(f"{file_path}: {exc}")

    return summary


def _empty_sync_summary(
    *,
    updated_after: str | None,
    cursor: str | None,
) -> dict:
    return {
        "selected": 0,
        "inserted": 0,
        "skipped": 0,
        "skipped_missing_journal": 0,
        "files_written": 0,
        "errors": [],
        "cursor": cursor,
        "updated_after": updated_after,
    }


def sync_granola_notes(
    updated_after: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Pull notes updated since the Redis cursor and append to daily journals.

    Empty Redis seeds ``updated_after`` to now−15m (see ``seed_updated_after``)
    so the first run does not dump the whole historical library. The cursor
    advances to this run's start time only when the pull and writes succeed.
    """
    run_started = format_utc_iso(utc_now(now))
    stored_cursor = get_stored_cursor()
    effective_cursor = stored_cursor or seed_updated_after(now)

    summary = _empty_sync_summary(
        updated_after=updated_after,
        cursor=effective_cursor,
    )

    if not os.getenv("GRANOLA_API_KEY"):
        logger.error("GRANOLA_API_KEY not set, skipping Granola notes sync")
        summary["errors"].append("GRANOLA_API_KEY not set")
        return summary

    try:
        resolved_updated = resolve_updated_after(
            updated_after,
            now=now,
            stored_cursor=stored_cursor,
        )
    except ValueError as exc:
        logger.error("Granola sync invalid params: %s", exc)
        summary["errors"].append(str(exc))
        return summary

    summary["updated_after"] = resolved_updated
    logger.info(
        "Granola sync starting updated_after=%s cursor=%s",
        resolved_updated,
        effective_cursor,
    )

    try:
        listed = list(iter_notes(updated_after=resolved_updated))
    except Exception as exc:
        logger.exception("Granola list notes failed")
        summary["errors"].append(str(exc))
        return summary

    selected: list[dict] = []
    for note in listed:
        try:
            hydrated = hydrate_note(note)
        except GranolaAPIError as exc:
            logger.exception("Granola get note failed")
            summary["errors"].append(str(exc))
            continue
        except Exception as exc:
            logger.exception("Granola hydrate failed")
            summary["errors"].append(str(exc))
            continue
        if hydrated is None:
            continue
        selected.append(hydrated)

    if summary["errors"]:
        logger.error("Granola sync hydrate errors; not writing or advancing cursor")
        return summary

    summary["selected"] = len(selected)
    result = write_notes_by_journal(selected, now=now, raise_errors=False)
    summary.update(
        {
            "selected": result["selected"],
            "inserted": result["inserted"],
            "skipped": result["skipped"],
            "skipped_missing_journal": result["skipped_missing_journal"],
            "files_written": result["files_written"],
            "errors": result["errors"],
        }
    )

    if summary["errors"]:
        logger.error("Granola sync errors: %s", summary["errors"])
        logger.info(
            "Granola sync finished without advancing cursor=%s "
            "updated_after=%s selected=%s inserted=%s skipped=%s "
            "skipped_missing_journal=%s files_written=%s errors=%s",
            summary["cursor"],
            summary["updated_after"],
            summary["selected"],
            summary["inserted"],
            summary["skipped"],
            summary["skipped_missing_journal"],
            summary["files_written"],
            len(summary["errors"]),
        )
        return summary

    if set_stored_cursor(run_started):
        summary["cursor"] = run_started
    logger.info(
        "Granola sync finished selected=%s inserted=%s skipped=%s "
        "skipped_missing_journal=%s files_written=%s errors=%s "
        "updated_after=%s cursor=%s",
        summary["selected"],
        summary["inserted"],
        summary["skipped"],
        summary["skipped_missing_journal"],
        summary["files_written"],
        len(summary["errors"]),
        summary["updated_after"],
        summary["cursor"],
    )
    return summary
