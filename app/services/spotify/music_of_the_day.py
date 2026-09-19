"""Once-daily Liked Songs → daily-journal ``### Music of the Day``.

Scheduled at 03:00 ``SYSTEM_TZ``. Writes yesterday's likes into yesterday's
journal (at 3am Sep 20 → ``Sep 19, 2026.md``). Not part of the 15-minute
Spotify drain.

Reuses Saved Tracks helpers (``iter_saved_track_items``,
``track_id_from_item``, refresh-on-demand tokens). Dropbox writes are
rev-safe via ``upload_if_rev_matches``. Empty days skip the write entirely.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import dropbox

from config import SYSTEM_TZ
from services.obsidian.add_readwise_buffet import (
    _get_dropbox_client,
    _resolve_journal_folder,
    journal_filename,
)
from services.obsidian.utils.dropbox_rev_safe import upload_if_rev_matches
from services.spotify.saved_tracks import (
    get_spotify_access_token,
    iter_saved_track_items,
    require_spotify_credentials,
    track_id_from_item,
)
from services.spotify.sync import local_now, parse_spotify_datetime

logger = logging.getLogger(__name__)

MUSIC_OF_THE_DAY_HEADER = "### Music of the Day"
TRANSCRIPT_NOTES_HEADER = "### Transcript Notes"
HEADING_PREFIX = "### "
SPOTIFY_TRACK_URL_PREFIX = "https://open.spotify.com/track/"
TRACK_ID_IN_URL = re.compile(
    r"https://open\.spotify\.com/track/([A-Za-z0-9]+)",
    re.IGNORECASE,
)


def previous_calendar_day(now: datetime | None = None) -> date:
    """Previous calendar day in ``SYSTEM_TZ`` (not the 3am journal rollover)."""
    return local_now(now).date() - timedelta(days=1)


def parse_journal_day(value: date | datetime | str | None) -> date | None:
    """Parse an explicit journal day. ``None`` means use previous calendar day."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}") from exc


def resolve_journal_day(
    *,
    date_value: date | datetime | str | None = None,
    now: datetime | None = None,
) -> date:
    parsed = parse_journal_day(date_value)
    if parsed is not None:
        return parsed
    return previous_calendar_day(now)


def journal_path_for_day(journal_folder_path: str, day: date) -> str:
    return f"{journal_folder_path}/{journal_filename(datetime(day.year, day.month, day.day))}"


def _nonempty(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def track_spotify_url(track: dict[str, Any], track_id: str) -> str:
    urls = track.get("external_urls")
    if isinstance(urls, dict):
        url = _nonempty(urls.get("spotify"))
        if url and url.startswith(("http://", "https://")):
            return url.split("?", 1)[0].split("#", 1)[0]
    return f"{SPOTIFY_TRACK_URL_PREFIX}{track_id}"


def normalize_liked_track(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """Title, artist, Spotify https URL, id, and ``added_at`` — or None."""
    if not isinstance(item, dict):
        return None
    track_id = track_id_from_item(item)
    if not track_id:
        return None
    added_at = parse_spotify_datetime(item.get("added_at"))
    if added_at is None:
        return None
    track = item.get("track")
    if not isinstance(track, dict):
        return None
    artists = track.get("artists") or []
    artist_names: list[str] = []
    if isinstance(artists, list):
        for artist in artists:
            if not isinstance(artist, dict):
                continue
            name = _nonempty(artist.get("name"))
            if name:
                artist_names.append(name)
    return {
        "id": track_id,
        "name": _nonempty(track.get("name")) or "Untitled",
        "artists": ", ".join(artist_names) or "Unknown artist",
        "url": track_spotify_url(track, track_id),
        "added_at": added_at,
    }


def liked_tracks_for_local_day(
    access_token: str,
    day: date,
) -> list[dict[str, Any]]:
    """Liked Songs whose ``added_at`` falls on ``day`` in ``SYSTEM_TZ``.

    Saved Tracks are newest-first; stop once items are older than the day.
    """
    day_start = SYSTEM_TZ.localize(datetime(day.year, day.month, day.day))
    day_end = day_start + timedelta(days=1)
    selected: list[dict[str, Any]] = []
    for item in iter_saved_track_items(access_token):
        added_at = parse_spotify_datetime(
            item.get("added_at") if isinstance(item, dict) else None
        )
        track_id = track_id_from_item(item)
        if added_at is None or track_id is None:
            continue
        if added_at < day_start:
            break
        if day_start <= added_at < day_end:
            track = normalize_liked_track(item)
            if track:
                selected.append(track)
    selected.sort(key=lambda row: row["added_at"])
    return selected


def format_track_line(track: dict[str, Any]) -> str:
    """``- [Song Title](https://open.spotify.com/track/…) — Artist``."""
    title = str(track.get("name") or "Untitled")
    safe_title = title.replace("[", "\\[").replace("]", "\\]")
    url = str(track.get("url") or "").strip()
    if not url:
        track_id = str(track.get("id") or "").strip()
        url = f"{SPOTIFY_TRACK_URL_PREFIX}{track_id}" if track_id else ""
    artist = str(track.get("artists") or "Unknown artist")
    return f"- [{safe_title}]({url}) — {artist}"


def track_id_from_url(value: str) -> str | None:
    match = TRACK_ID_IN_URL.search(value)
    if match:
        return match.group(1)
    return None


def track_ids_in_text(text: str) -> set[str]:
    return set(TRACK_ID_IN_URL.findall(text))


def _section_bounds(lines: list[str]) -> tuple[int | None, int]:
    header_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == MUSIC_OF_THE_DAY_HEADER),
        None,
    )
    if header_idx is None:
        return None, -1
    section_end = len(lines)
    for i in range(header_idx + 1, len(lines)):
        if lines[i].startswith(HEADING_PREFIX):
            section_end = i
            break
    return header_idx, section_end


def _append_section_at(
    lines: list[str],
    insert_at: int,
    track_lines: list[str],
) -> list[str]:
    block = [MUSIC_OF_THE_DAY_HEADER, *track_lines]
    prefix = list(lines[:insert_at])
    suffix = list(lines[insert_at:])
    while prefix and prefix[-1] == "":
        prefix.pop()
    if prefix and prefix[-1].strip():
        prefix.append("")
    if suffix:
        if suffix[0].strip():
            block.append("")
        return prefix + block + suffix
    return prefix + block


def merge_music_of_the_day_section(
    content: str,
    tracks: list[dict[str, Any]],
) -> str | None:
    """Insert or append Music of the Day lines. None if nothing to add.

    Existing heading is reused in place. Missing heading is created just
    before ``### Transcript Notes`` when that sibling exists, otherwise
    at EOF. Already-present track ids/URLs are skipped; order of existing
    lines is not changed.
    """
    if not tracks:
        return None

    lines = content.split("\n")
    header_idx, section_end = _section_bounds(lines)
    existing_ids: set[str] = set()
    if header_idx is not None:
        existing_ids = track_ids_in_text("\n".join(lines[header_idx:section_end]))

    new_lines: list[str] = []
    seen = set(existing_ids)
    for track in tracks:
        track_id = _nonempty(track.get("id")) or track_id_from_url(
            str(track.get("url") or "")
        )
        if not track_id or track_id in seen:
            continue
        new_lines.append(format_track_line(track))
        seen.add(track_id)

    if not new_lines:
        return None

    if header_idx is None:
        transcript_idx = next(
            (i for i, line in enumerate(lines) if line.strip() == TRANSCRIPT_NOTES_HEADER),
            None,
        )
        insert_at = transcript_idx if transcript_idx is not None else len(lines)
        updated = _append_section_at(lines, insert_at, new_lines)
        return "\n".join(updated)

    section_body = lines[header_idx + 1 : section_end]
    insert_offset = len(section_body)
    for i, line in enumerate(section_body):
        if line.strip():
            insert_offset = i + 1
    new_body = section_body[:insert_offset] + new_lines + section_body[insert_offset:]
    if new_body and new_body[-1].strip() and section_end < len(lines):
        new_body.append("")
    return "\n".join(lines[: header_idx + 1] + new_body + lines[section_end:])


def _download_journal(
    dbx: dropbox.Dropbox,
    file_path: str,
) -> tuple[str, str, str]:
    """Return ``(content, rev, path_display)``. Missing file → FileNotFoundError."""
    try:
        metadata, response = dbx.files_download(file_path)
    except dropbox.exceptions.ApiError as exc:
        if isinstance(exc.error, dropbox.files.DownloadError):
            raise FileNotFoundError(f"Journal not found: {file_path}") from exc
        raise
    content = response.content.decode("utf-8")
    rev = getattr(metadata, "rev", None)
    if not rev:
        raise RuntimeError(f"No Dropbox rev on download for {file_path}")
    path_display = getattr(metadata, "path_display", None) or file_path
    return content, rev, path_display


def _write_journal_once(
    dbx: dropbox.Dropbox,
    file_path: str,
    tracks: list[dict[str, Any]],
) -> dict[str, Any]:
    content, rev, path_display = _download_journal(dbx, file_path)
    updated = merge_music_of_the_day_section(content, tracks)
    if updated is None:
        return {
            "status": "skipped",
            "path": path_display,
            "inserted": 0,
            "rev": rev,
        }

    inserted = updated.count(SPOTIFY_TRACK_URL_PREFIX) - content.count(
        SPOTIFY_TRACK_URL_PREFIX
    )
    result = upload_if_rev_matches(
        dbx,
        path_display,
        updated.encode("utf-8"),
        rev,
    )
    if result.status == "deferred":
        return {
            "status": "deferred",
            "path": path_display,
            "inserted": 0,
            "rev": rev,
        }
    return {
        "status": "updated",
        "path": path_display,
        "inserted": max(inserted, 0),
        "rev": rev,
    }


def write_music_of_the_day(
    *,
    date: date | datetime | str | None = None,
    now: datetime | None = None,
    access_token: str | None = None,
) -> dict[str, Any]:
    """Write previous-day (or ``date``) Liked Songs into that day's journal.

    Empty likes skip Dropbox entirely. On rev mismatch, re-download once and
    retry; a second mismatch defers (next scheduled run is a different day).
    """
    day = resolve_journal_day(date_value=date, now=now)
    summary: dict[str, Any] = {
        "status": "empty",
        "date": day.isoformat(),
        "journal": journal_filename(datetime(day.year, day.month, day.day)),
        "tracks": 0,
        "inserted": 0,
        "path": None,
    }

    require_spotify_credentials()
    token = access_token or get_spotify_access_token()
    tracks = liked_tracks_for_local_day(token, day)
    summary["tracks"] = len(tracks)
    if not tracks:
        logger.info(
            "Music of the Day: no Liked Songs on %s; skipping write",
            day.isoformat(),
        )
        return summary

    dbx = _get_dropbox_client()
    journal_folder = _resolve_journal_folder(dbx)
    file_path = journal_path_for_day(journal_folder, day)
    summary["path"] = file_path

    try:
        result = _write_journal_once(dbx, file_path, tracks)
    except FileNotFoundError:
        logger.warning(
            "Music of the Day skipped; journal not found (will not create): %s",
            file_path,
        )
        summary["status"] = "skipped_missing_journal"
        return summary

    if result["status"] == "deferred":
        logger.info(
            "Rev conflict for %s; re-downloading once to apply Music of the Day.",
            file_path,
        )
        try:
            result = _write_journal_once(dbx, file_path, tracks)
        except FileNotFoundError:
            logger.warning(
                "Music of the Day skipped after rev retry; journal missing: %s",
                file_path,
            )
            summary["status"] = "skipped_missing_journal"
            return summary
        if result["status"] == "deferred":
            logger.warning(
                "Deferring Music of the Day for %s; cloud file left unchanged "
                "(no overwrite / no conflicted copy). Next scheduled run is a "
                "different day — re-trigger with ?date=%s if needed.",
                file_path,
                day.isoformat(),
            )

    summary["status"] = result["status"]
    summary["inserted"] = result["inserted"]
    summary["path"] = result.get("path") or file_path
    logger.info(
        "Music of the Day %s date=%s path=%s tracks=%s inserted=%s",
        summary["status"],
        day.isoformat(),
        summary["path"],
        summary["tracks"],
        summary["inserted"],
    )
    return summary
