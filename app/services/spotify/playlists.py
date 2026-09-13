"""Spotify playlist helpers for half-year and Shazam sync jobs.

Uses refresh-on-demand access tokens from ``saved_tracks``. Never logs tokens.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from services.spotify.saved_tracks import (
    _raise_for_spotify_status,
    spotify_request,
    track_id_from_item,
)

logger = logging.getLogger(__name__)

SPOTIFY_ME_URL = "https://api.spotify.com/v1/me"
SPOTIFY_ME_PLAYLISTS_URL = "https://api.spotify.com/v1/me/playlists"
PLAYLIST_PAGE_LIMIT = 50


def _playlist_tracks_url(playlist_id: str) -> str:
    return f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"


def fetch_current_user(*, redis_client=None) -> dict[str, Any]:
    response = spotify_request("GET", SPOTIFY_ME_URL, redis_client=redis_client)
    _raise_for_spotify_status(response, "current-user")
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("Spotify current-user returned non-JSON") from exc
    if not isinstance(body, dict) or not body.get("id"):
        raise RuntimeError("Spotify current-user response missing id")
    return body


def iter_user_playlists(*, redis_client=None) -> Iterator[dict[str, Any]]:
    offset = 0
    while True:
        response = spotify_request(
            "GET",
            SPOTIFY_ME_PLAYLISTS_URL,
            redis_client=redis_client,
            params={"limit": PLAYLIST_PAGE_LIMIT, "offset": offset},
        )
        _raise_for_spotify_status(response, "list-playlists")
        try:
            page = response.json()
        except ValueError as exc:
            raise RuntimeError("Spotify list-playlists returned non-JSON") from exc
        items = page.get("items") or []
        logger.info("Spotify playlists page offset=%s items=%s", offset, len(items))
        if not items:
            return
        for item in items:
            if isinstance(item, dict):
                yield item
        if not page.get("next"):
            return
        offset += len(items)


def find_playlist_by_name(name: str, *, redis_client=None) -> dict[str, Any] | None:
    for playlist in iter_user_playlists(redis_client=redis_client):
        if (playlist.get("name") or "") == name:
            return playlist
    return None


def create_playlist(
    name: str,
    *,
    public: bool = False,
    description: str = "",
    redis_client=None,
) -> dict[str, Any]:
    user = fetch_current_user(redis_client=redis_client)
    user_id = user["id"]
    response = spotify_request(
        "POST",
        f"https://api.spotify.com/v1/users/{user_id}/playlists",
        redis_client=redis_client,
        json={"name": name, "public": public, "description": description},
    )
    _raise_for_spotify_status(response, "create-playlist")
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("Spotify create-playlist returned non-JSON") from exc
    if not isinstance(body, dict) or not body.get("id"):
        raise RuntimeError("Spotify create-playlist response missing id")
    logger.info("Created Spotify playlist name=%s", name)
    return body


def iter_playlist_items(playlist_id: str, *, redis_client=None) -> Iterator[dict[str, Any]]:
    offset = 0
    while True:
        response = spotify_request(
            "GET",
            _playlist_tracks_url(playlist_id),
            redis_client=redis_client,
            params={"limit": PLAYLIST_PAGE_LIMIT, "offset": offset},
        )
        _raise_for_spotify_status(response, "playlist-tracks")
        try:
            page = response.json()
        except ValueError as exc:
            raise RuntimeError("Spotify playlist-tracks returned non-JSON") from exc
        items = page.get("items") or []
        logger.info(
            "Spotify playlist tracks offset=%s items=%s playlist=%s",
            offset,
            len(items),
            playlist_id,
        )
        if not items:
            return
        for item in items:
            if isinstance(item, dict):
                yield item
        if not page.get("next"):
            return
        offset += len(items)


def playlist_track_ids(playlist_id: str, *, redis_client=None) -> set[str]:
    ids: set[str] = set()
    for item in iter_playlist_items(playlist_id, redis_client=redis_client):
        track_id = track_id_from_item(item)
        if track_id:
            ids.add(track_id)
    return ids


def add_track_to_playlist(
    playlist_id: str,
    track_uri: str,
    *,
    redis_client=None,
) -> None:
    response = spotify_request(
        "POST",
        _playlist_tracks_url(playlist_id),
        redis_client=redis_client,
        json={"uris": [track_uri]},
    )
    _raise_for_spotify_status(response, "add-playlist-track")
