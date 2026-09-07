"""Scheduled Granola → Obsidian daily-journal sync.

Pulls notes updated since a Redis last-run cursor and appends each summary
block under ``### Transcript Notes`` on the matching daily journal
(3am local rollover).
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
    _nonempty,
    _resolve_journal_folder,
    journal_filename,
    parse_highlight_datetime,
)
from services.obsidian.utils.date_helpers import get_effective_date

load_dotenv()

logger = logging.getLogger(__name__)

CURSOR_REDIS_KEY = "granola:notes:cursor"
TRANSCRIPT_NOTES_HEADER = "### Transcript Notes"
SEED_LOOKBACK_MINUTES = 15

# Daily-journal ### siblings only. Transcript Notes is last in the template,
# so the insert window usually runs to EOF. An existing mid-note heading
# still stops at these so later sections stay outside the section body.
# ATX headings from summary_markdown (``### Church and Spiritual Practice``)
# are not siblings and must not end the section.
_JOURNAL_SIBLING_HEADERS = frozenset(
    {
        "### Morning Pages",
        "### Content Buffet:",
        "### Content Buffet",
        "### Content Planning",
        TRANSCRIPT_NOTES_HEADER,
    }
)


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


def note_summary_body(note: dict) -> str:
    """Prefer ``summary_markdown``; fall back to ``summary_text``.

    Never uses ``transcript`` / private notes. Empty or whitespace-only
    markdown falls through to plain text. The chosen body is returned
    as-is (trailing newlines stripped only so the journal block joins cleanly).
    """
    for key in ("summary_markdown", "summary_text"):
        raw = note.get(key)
        if raw is None:
            continue
        text = str(raw)
        if text.strip():
            return text.rstrip("\n")
    return ""


def granola_html_comment(note_id_value: str) -> str:
    """Dedup marker: ``<!-- granola:not_… -->``."""
    return f"<!-- granola:{note_id_value} -->"


def format_granola_block(note: dict) -> str | None:
    """Multi-line Transcript Notes block: heading, HTML comment id, summary.

    Shape::

        #### [Title](web_url)
        <!-- granola:not_… -->

        <summary_markdown>
    """
    nid = note_id(note)
    if not nid:
        return None
    title = _nonempty(note.get("title")) or "Untitled"
    url = note_web_url(note)
    if url:
        safe_title = title.replace("[", "\\[").replace("]", "\\]")
        heading = f"#### [{safe_title}]({url})"
    else:
        heading = f"#### {title}"
    parts = [heading, granola_html_comment(nid)]
    summary = note_summary_body(note)
    if summary:
        parts.extend(["", summary])
    return "\n".join(parts)


def format_granola_bullet(note: dict) -> str | None:
    """Backward-compatible name for ``format_granola_block``."""
    return format_granola_block(note)


def granola_dedup_keys(note: dict) -> list[str]:
    """Dedup on the Granola note id (``not_…`` / ``granola:not_…``)."""
    nid = note_id(note)
    if not nid:
        return []
    return [f"granola:{nid}", nid]


def _line_has_dedup_key(line: str, keys: list[str]) -> bool:
    stripped = line.strip()
    return any(key in stripped for key in keys if key)


def _is_title_only_granola_line(line: str, keys: list[str]) -> bool:
    """Legacy ``- [Title](url) granola:not_…`` / ``- Title granola:not_…``."""
    stripped = line.strip()
    return stripped.startswith("- ") and _line_has_dedup_key(stripped, keys)


def _is_full_block_marker_line(line: str, keys: list[str]) -> bool:
    """``<!-- granola:not_… -->`` — summary block already written."""
    stripped = line.strip()
    if not (stripped.startswith("<!--") and "-->" in stripped):
        return False
    return _line_has_dedup_key(stripped, keys)


def _ensure_blank_after_header(body: list[str]) -> list[str]:
    if not body:
        return [""]
    if body[0].strip() == "":
        return body
    return [""] + body


def _with_note_separator(prefix: list[str], block_lines: list[str]) -> list[str]:
    """Join an existing section prefix to a new block with ``---`` (not before first)."""
    body = list(prefix)
    while body and not body[-1].strip():
        body.pop()
    if body:
        body.extend(["", "---", ""])
    body.extend(block_lines)
    return _ensure_blank_after_header(body)


def _replace_title_only_line(
    section_body: list[str],
    replace_idx: int,
    block_lines: list[str],
) -> list[str]:
    prefix = section_body[:replace_idx]
    suffix = section_body[replace_idx + 1 :]
    while suffix and not suffix[0].strip():
        suffix.pop()
    body = _with_note_separator(prefix, block_lines)
    if suffix:
        body = _with_note_separator(body, suffix)
    return body


def _append_block_to_section(section_body: list[str], block_lines: list[str]) -> list[str]:
    return _with_note_separator(section_body, block_lines)


def _is_journal_sibling_header(line: str) -> bool:
    """True for real daily-journal ``###`` siblings, not summary-body ATX."""
    stripped = line.strip()
    if stripped in _JOURNAL_SIBLING_HEADERS:
        return True
    # Same prefix rule as Content Buffet placement (``### Content Planning``).
    return stripped.startswith("### Content Planning")


def _transcript_notes_bounds(lines: list[str]) -> tuple[int | None, int]:
    """Bounds of ``### Transcript Notes``.

    Starts at the existing heading (exact ``### Transcript Notes``). Ends at
    the next journal sibling ``###`` header, or EOF when none follows
    (Transcript Notes is last in the daily template). Does **not** treat
    ``### `` headings inside a note's ``summary_markdown`` as the section end.
    """
    header_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == TRANSCRIPT_NOTES_HEADER),
        None,
    )
    if header_idx is None:
        return None, -1
    section_end = len(lines)
    for i in range(header_idx + 1, len(lines)):
        if _is_journal_sibling_header(lines[i]):
            section_end = i
            break
    return header_idx, section_end


def insert_transcript_notes_bullet(
    content: str,
    bullet: str,
    keys: list[str] | None = None,
) -> tuple[str, str]:
    """Insert a summary block under ``### Transcript Notes``.

    Returns ``(content, action)`` where action is ``inserted``,
    ``replaced`` (legacy title-only bullet upgraded), or ``skipped``
    (full block for this id already present).

    Existing heading is reused in place (not moved). Missing heading is
    created at EOF. ``####`` note headings and ATX headings that belong
    to a note's ``summary_markdown`` stay inside the section. The section
    extends to EOF unless a later daily-journal sibling ``###`` header
    is present (Morning Pages, Content Buffet, Content Planning).
    """
    keys = [key for key in (keys or []) if key]
    lines = content.split("\n")
    header_idx, section_end = _transcript_notes_bounds(lines)
    block_lines = _buffet_bullet_lines(bullet)

    if header_idx is None:
        updated = list(lines)
        while updated and updated[-1] == "":
            updated.pop()
        if updated and updated[-1].strip():
            updated.append("")
        updated.extend([TRANSCRIPT_NOTES_HEADER, "", *block_lines, ""])
        return "\n".join(updated), "inserted"

    section_body = lines[header_idx + 1 : section_end]
    if any(_is_full_block_marker_line(line, keys) for line in section_body):
        return content, "skipped"

    replace_idx = next(
        (i for i, line in enumerate(section_body) if _is_title_only_granola_line(line, keys)),
        None,
    )
    if replace_idx is not None:
        new_body = _replace_title_only_line(section_body, replace_idx, block_lines)
    else:
        new_body = _append_block_to_section(section_body, block_lines)
    if new_body and new_body[-1].strip() and section_end < len(lines):
        new_body.append("")
    action = "replaced" if replace_idx is not None else "inserted"
    updated = lines[: header_idx + 1] + new_body + lines[section_end:]
    return "\n".join(updated), action


def hydrate_note(note: dict) -> dict | None:
    """Always GET /v1/notes/{id} so ``summary_markdown`` is present.

    List payloads typically omit summary fields. Returns None when the
    note 404s (do not invent it).
    """
    nid = note_id(note)
    if not nid:
        logger.warning("Granola note missing id; skipping")
        return None
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
    """Append summary blocks grouped by journal file (one download/upload per day).

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
                block = format_granola_block(note)
                if not block:
                    continue
                content, action = insert_transcript_notes_bullet(
                    content, block, granola_dedup_keys(note)
                )
                if action == "replaced":
                    action = "inserted"
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


def run_granola_notes_sync(
    *,
    resolved_updated_after: str | None,
    now: datetime | None = None,
    run_started: str | None = None,
    stored_cursor: str | None = None,
    since_date: date | None = None,
    log_label: str = "Granola sync",
) -> dict:
    """Pull, hydrate, write journals, and advance ``granola:notes:cursor``.

    ``resolved_updated_after`` is passed through to ``iter_notes`` as-is
    (``None`` omits the API filter). This does **not** apply the
    incremental empty-Redis 15m seed — callers resolve their own filter.
    The cursor advances to ``run_started`` only when the pull and writes
    succeed.
    """
    if run_started is None:
        run_started = format_utc_iso(utc_now(now))
    if stored_cursor is None:
        stored_cursor = get_stored_cursor()
    effective_cursor = stored_cursor or seed_updated_after(now)

    summary = _empty_sync_summary(
        updated_after=resolved_updated_after,
        cursor=effective_cursor,
    )

    if not os.getenv("GRANOLA_API_KEY"):
        logger.error("GRANOLA_API_KEY not set, skipping %s", log_label)
        summary["errors"].append("GRANOLA_API_KEY not set")
        return summary

    logger.info(
        "%s starting updated_after=%s cursor=%s",
        log_label,
        resolved_updated_after,
        effective_cursor,
    )

    try:
        listed = list(iter_notes(updated_after=resolved_updated_after))
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
        if since_date is not None and note_effective_date(hydrated, now=now) < since_date:
            continue
        selected.append(hydrated)

    if summary["errors"]:
        logger.error("%s hydrate errors; not writing or advancing cursor", log_label)
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
        logger.error("%s errors: %s", log_label, summary["errors"])
        logger.info(
            "%s finished without advancing cursor=%s "
            "updated_after=%s selected=%s inserted=%s skipped=%s "
            "skipped_missing_journal=%s files_written=%s errors=%s",
            log_label,
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
        "%s finished selected=%s inserted=%s skipped=%s "
        "skipped_missing_journal=%s files_written=%s errors=%s "
        "updated_after=%s cursor=%s",
        log_label,
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


def sync_granola_notes(
    updated_after: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Pull notes updated since the Redis cursor and append summaries to journals.

    Empty Redis seeds ``updated_after`` to now−15m (see ``seed_updated_after``)
    so the first run does not dump the whole historical library. The cursor
    advances to this run's start time only when the pull and writes succeed.
    """
    run_started = format_utc_iso(utc_now(now))
    stored_cursor = get_stored_cursor()
    effective_cursor = stored_cursor or seed_updated_after(now)

    if not os.getenv("GRANOLA_API_KEY"):
        summary = _empty_sync_summary(
            updated_after=updated_after,
            cursor=effective_cursor,
        )
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
        summary = _empty_sync_summary(
            updated_after=updated_after,
            cursor=effective_cursor,
        )
        logger.error("Granola sync invalid params: %s", exc)
        summary["errors"].append(str(exc))
        return summary

    return run_granola_notes_sync(
        resolved_updated_after=resolved_updated,
        now=now,
        run_started=run_started,
        stored_cursor=stored_cursor,
        log_label="Granola sync",
    )
