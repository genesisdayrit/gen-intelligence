"""Spotify Web API client for the current user's Saved Tracks.

Used by the Sunday wrap-up email. Refreshes an access token at send time
from env credentials; does not read host Redis keys.

Required env:
    SPOTIFY_CLIENT_ID
    SPOTIFY_CLIENT_SECRET
    SPOTIFY_REFRESH_TOKEN   (user-library-read scope)

Never log tokens, secrets, or Authorization headers.
"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import Iterator
from typing import Any

import requests

logger = logging.getLogger(__name__)

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SAVED_TRACKS_URL = "https://api.spotify.com/v1/me/tracks"
SAVED_TRACKS_PAGE_LIMIT = 50


def require_spotify_credentials() -> tuple[str, str, str]:
    """Return (client_id, client_secret, refresh_token) or raise if any is missing."""
    client_id = (os.getenv("SPOTIFY_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("SPOTIFY_CLIENT_SECRET") or "").strip()
    refresh_token = (os.getenv("SPOTIFY_REFRESH_TOKEN") or "").strip()
    missing = [
        name
        for name, value in (
            ("SPOTIFY_CLIENT_ID", client_id),
            ("SPOTIFY_CLIENT_SECRET", client_secret),
            ("SPOTIFY_REFRESH_TOKEN", refresh_token),
        )
        if not value
    ]
    if missing:
        raise EnvironmentError(f"{', '.join(missing)} not set")
    return client_id, client_secret, refresh_token


def refresh_spotify_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> str:
    """Exchange the refresh token for a short-lived access token."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    try:
        response = requests.post(
            SPOTIFY_TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError("Spotify token refresh request failed") from exc

    if response.status_code != 200:
        raise RuntimeError(f"Spotify token refresh returned HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("Spotify token refresh returned non-JSON") from exc

    access_token = body.get("access_token")
    if not access_token:
        raise RuntimeError("Spotify token refresh response missing access_token")
    return str(access_token)


def fetch_saved_tracks_page(
    access_token: str,
    *,
    limit: int = SAVED_TRACKS_PAGE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    try:
        response = requests.get(
            SPOTIFY_SAVED_TRACKS_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"limit": limit, "offset": offset},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError("Spotify saved-tracks request failed") from exc

    if response.status_code != 200:
        raise RuntimeError(f"Spotify saved-tracks returned HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("Spotify saved-tracks returned non-JSON") from exc

    if not isinstance(body, dict):
        raise RuntimeError("Spotify saved-tracks response was not an object")
    return body


def iter_saved_track_items(
    access_token: str,
    *,
    limit: int = SAVED_TRACKS_PAGE_LIMIT,
) -> Iterator[dict[str, Any]]:
    """Yield Saved Track items newest-first, following pagination."""
    offset = 0
    while True:
        page = fetch_saved_tracks_page(access_token, limit=limit, offset=offset)
        items = page.get("items") or []
        logger.info("Spotify saved tracks page offset=%s items=%s", offset, len(items))
        if not items:
            return
        for item in items:
            if isinstance(item, dict):
                yield item
        if not page.get("next"):
            return
        offset += len(items)
