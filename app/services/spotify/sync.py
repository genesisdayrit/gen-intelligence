"""Spotify library/playlist jobs migrated from personal-ec2 crontab.

Port of ``~/repos/spotify-api`` scripts onto hub APScheduler:

* ``add_shazam_songs_to_libary_today`` → ``sync_shazam_to_library``
* ``add_songs_saved_today`` → ``sync_saved_today_to_half_year``
* ``create_this_half_year_playlist`` → ``create_half_year_playlist``

``refresh_redis_token.py`` is **not** ported. Access tokens refresh on
demand (and on HTTP 401). The durable secret is ``SPOTIFY_REFRESH_TOKEN``
in env. Hub Redis may cache a short-lived access token and stores the
Shazam watermark; it is a different instance than host Redis.

Never log tokens, secrets, or Authorization headers.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from config import SYSTEM_TZ, redis_client
from services.spotify.playlists import (
    add_track_to_playlist,
    create_playlist,
    find_playlist_by_name,
    iter_playlist_items,
    playlist_track_ids,
)
from services.spotify.saved_tracks import (
    get_spotify_access_token,
    iter_saved_track_items,
    library_contains_track,
    require_spotify_credentials,
    save_track_to_library,
    track_id_from_item,
)

logger = logging.getLogger(__name__)

SHAZAM_WATERMARK_REDIS_KEY = "spotify_shazam_last_processed_added_at"
TRACK_SAVE_SPACING_SECONDS = 1.2


def require_shazam_playlist_id() -> str:
    playlist_id = (os.getenv("SHAZAM_PLAYLIST_ID") or "").strip()
    if not playlist_id:
        raise EnvironmentError("SHAZAM_PLAYLIST_ID not set")
    return playlist_id


def parse_spotify_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_spotify_datetime(value: datetime) -> str:
    utc = value.astimezone(timezone.utc).replace(microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def local_now(now: datetime | None = None) -> datetime:
    instant = datetime.now(SYSTEM_TZ) if now is None else now
    if instant.tzinfo is None:
        return SYSTEM_TZ.localize(instant)
    return instant.astimezone(SYSTEM_TZ)


def half_year_playlist_name(when: datetime) -> str:
    local = local_now(when)
    half = 1 if local.month <= 6 else 2
    return f"{local.year} - {half}/2"


def get_shazam_watermark() -> str | None:
    try:
        value = redis_client.get(SHAZAM_WATERMARK_REDIS_KEY)
    except Exception:
        logger.exception("Failed to read Shazam watermark from hub Redis")
        raise
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def set_shazam_watermark(value: str) -> None:
    redis_client.set(SHAZAM_WATERMARK_REDIS_KEY, value)


def _item_added_at(item: dict[str, Any]) -> datetime | None:
    return parse_spotify_datetime(item.get("added_at") if isinstance(item, dict) else None)


def sync_shazam_to_library(
    *,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll the Shazam playlist and save new tracks to Liked Songs.

    Missing watermark seeds to now and processes nothing. That avoids
    dumping the historical Shazam playlist on a fresh hub Redis, but it
    is not a permanent skip: set ``spotify_shazam_last_processed_added_at``
    in hub Redis to an earlier ISO timestamp (or copy it from host Redis)
    and re-run. Do this **before** the first successful hub run on cutover.
    """
    require_spotify_credentials()
    playlist_id = require_shazam_playlist_id()

    watermark = get_shazam_watermark()
    if watermark is None:
        seed = format_spotify_datetime(utc_now(now))
        set_shazam_watermark(seed)
        logger.warning(
            "Shazam watermark missing; seeded to %s and processed nothing. "
            "If this was a cutover, copy %s from host Redis into hub Redis "
            "and re-run. Hub Redis is not the personal-ec2 host instance.",
            seed,
            SHAZAM_WATERMARK_REDIS_KEY,
        )
        return {
            "status": "seeded",
            "watermark": seed,
            "saved": 0,
            "already_saved": 0,
            "failed": 0,
        }

    watermark_dt = parse_spotify_datetime(watermark)
    if watermark_dt is None:
        raise RuntimeError("Shazam watermark is not a valid timestamp")

    get_spotify_access_token()

    candidates: list[tuple[datetime, str, str]] = []
    for item in iter_playlist_items(playlist_id):
        added_at = _item_added_at(item)
        track_id = track_id_from_item(item)
        if added_at is None or track_id is None:
            continue
        if added_at <= watermark_dt:
            continue
        added_at_text = item.get("added_at")
        added_raw = (
            str(added_at_text)
            if isinstance(added_at_text, str) and added_at_text.strip()
            else format_spotify_datetime(added_at)
        )
        candidates.append((added_at, track_id, added_raw))
    candidates.sort(key=lambda row: row[0])

    saved = 0
    already_saved = 0
    for _added_at, track_id, added_raw in candidates:
        if library_contains_track(track_id):
            already_saved += 1
            set_shazam_watermark(added_raw)
            logger.info("Shazam track already in library; advanced watermark")
            continue
        save_track_to_library(track_id)
        saved += 1
        set_shazam_watermark(added_raw)
        logger.info("Saved Shazam track to library")
        sleep(TRACK_SAVE_SPACING_SECONDS)

    return {
        "status": "ok",
        "watermark": get_shazam_watermark(),
        "saved": saved,
        "already_saved": already_saved,
        "considered": len(candidates),
    }


def _saved_tracks_for_local_day(
    access_token: str,
    day_start: datetime,
    day_end: datetime,
) -> list[tuple[datetime, str]]:
    selected: list[tuple[datetime, str]] = []
    for item in iter_saved_track_items(access_token):
        added_at = _item_added_at(item)
        track_id = track_id_from_item(item)
        if added_at is None or track_id is None:
            continue
        if added_at < day_start:
            break
        if day_start <= added_at < day_end:
            selected.append((added_at, track_id))
    selected.sort(key=lambda row: row[0])
    return selected


def sync_saved_today_to_half_year(
    *,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Add Liked Songs saved today to the current half-year playlist."""
    require_spotify_credentials()
    access_token = get_spotify_access_token()

    current = local_now(now)
    playlist_name = half_year_playlist_name(current)
    playlist = find_playlist_by_name(playlist_name)
    if playlist is None or not playlist.get("id"):
        logger.warning(
            "Half-year playlist %s not found; skip drain. "
            "Run spotify_create_half_year_playlist if the half just started.",
            playlist_name,
        )
        return {
            "status": "playlist_missing",
            "playlist": playlist_name,
            "added": 0,
            "skipped": 0,
        }

    day_start = SYSTEM_TZ.localize(datetime(current.year, current.month, current.day))
    day_end = day_start + timedelta(days=1)
    today_tracks = _saved_tracks_for_local_day(access_token, day_start, day_end)
    existing = playlist_track_ids(playlist["id"])

    added = 0
    pending = [
        (added_at, track_id)
        for added_at, track_id in today_tracks
        if track_id not in existing
    ]
    for _added_at, track_id in pending:
        add_track_to_playlist(playlist["id"], f"spotify:track:{track_id}")
        added += 1
        existing.add(track_id)
        logger.info("Added saved track to half-year playlist")
        sleep(TRACK_SAVE_SPACING_SECONDS)
    skipped = len(today_tracks) - added

    return {
        "status": "ok",
        "playlist": playlist_name,
        "playlist_id": playlist["id"],
        "added": added,
        "skipped": skipped,
        "today": len(today_tracks),
    }


def create_half_year_playlist(*, now: datetime | None = None) -> dict[str, Any]:
    """Create ``{year} - 1/2`` or ``{year} - 2/2`` if it does not exist."""
    require_spotify_credentials()
    get_spotify_access_token()

    current = local_now(now)
    playlist_name = half_year_playlist_name(current)
    existing = find_playlist_by_name(playlist_name)
    if existing and existing.get("id"):
        logger.info("Half-year playlist already exists: %s", playlist_name)
        return {
            "status": "exists",
            "playlist": playlist_name,
            "playlist_id": existing["id"],
        }

    half = 1 if current.month <= 6 else 2
    created = create_playlist(
        playlist_name,
        public=False,
        description=f"Liked Songs H{half} {current.year}",
    )
    return {
        "status": "created",
        "playlist": playlist_name,
        "playlist_id": created["id"],
    }
