"""Spotify Web API client for the current user's Saved Tracks.

Used by the Sunday wrap-up email and the library/playlist write jobs.
Refreshes an access token at call time from env credentials; does not
read host Redis token keys. Access tokens may be cached briefly in hub
Redis with TTL. The durable secret stays ``SPOTIFY_REFRESH_TOKEN`` in env.

Required env:
    SPOTIFY_CLIENT_ID
    SPOTIFY_CLIENT_SECRET
    SPOTIFY_REFRESH_TOKEN

Required scopes on the refresh token (write jobs need more than
Sunday wrap-up's ``user-library-read``):
    user-library-read
    user-library-modify
    playlist-read-private
    playlist-modify-public
    playlist-modify-private

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
SPOTIFY_SAVED_TRACKS_CONTAINS_URL = "https://api.spotify.com/v1/me/tracks/contains"
SAVED_TRACKS_PAGE_LIMIT = 50

# Hub Redis only — not the personal-ec2 host Redis that the old crontab used.
ACCESS_TOKEN_REDIS_KEY = "spotify_access_token"
ACCESS_TOKEN_TTL_BUFFER_SECONDS = 60
DEFAULT_ACCESS_TOKEN_EXPIRES_IN = 3600

SPOTIFY_REQUIRED_SCOPES = (
    "user-library-read",
    "user-library-modify",
    "playlist-read-private",
    "playlist-modify-public",
    "playlist-modify-private",
)


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


def _hub_redis():
    from config import redis_client

    return redis_client


def _request_spotify_token_payload(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict[str, Any]:
    """Exchange the refresh token. Never log the request or response body."""
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

    if not isinstance(body, dict):
        raise RuntimeError("Spotify token refresh response was not an object")
    if not body.get("access_token"):
        raise RuntimeError("Spotify token refresh response missing access_token")
    return body


def refresh_spotify_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> str:
    """Exchange the refresh token for a short-lived access token."""
    payload = _request_spotify_token_payload(client_id, client_secret, refresh_token)
    return str(payload["access_token"])


def _cache_ttl_seconds(expires_in: Any) -> int:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = DEFAULT_ACCESS_TOKEN_EXPIRES_IN
    return max(seconds - ACCESS_TOKEN_TTL_BUFFER_SECONDS, 30)


def get_spotify_access_token(
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
    *,
    force_refresh: bool = False,
    redis_client=None,
) -> str:
    """Return an access token, refreshing on demand.

    Optional hub-Redis cache uses ``spotify_access_token`` with TTL from
    ``expires_in``. The refresh token is never written to Redis.
    """
    if not client_id or not client_secret or not refresh_token:
        client_id, client_secret, refresh_token = require_spotify_credentials()

    cache = redis_client if redis_client is not None else _hub_redis()
    if not force_refresh:
        try:
            cached = cache.get(ACCESS_TOKEN_REDIS_KEY)
        except Exception:
            logger.warning("Spotify access-token cache unreadable; refreshing")
            cached = None
        if cached:
            return str(cached)

    payload = _request_spotify_token_payload(client_id, client_secret, refresh_token)
    access_token = str(payload["access_token"])
    ttl = _cache_ttl_seconds(payload.get("expires_in", DEFAULT_ACCESS_TOKEN_EXPIRES_IN))
    try:
        cache.set(ACCESS_TOKEN_REDIS_KEY, access_token, ex=ttl)
    except Exception:
        logger.warning("Spotify access-token cache write failed; continuing without cache")
    return access_token


def spotify_request(
    method: str,
    url: str,
    *,
    access_token: str | None = None,
    retry_on_401: bool = True,
    timeout: int = 30,
    redis_client=None,
    **kwargs,
) -> requests.Response:
    """Authenticated Spotify HTTP call. Refreshes once on 401.

    Never logs Authorization headers, tokens, or response bodies.
    """
    token = access_token or get_spotify_access_token(redis_client=redis_client)
    headers = dict(kwargs.pop("headers", None) or {})
    headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=timeout,
            **kwargs,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Spotify {method} request failed") from exc

    if response.status_code == 401 and retry_on_401:
        token = get_spotify_access_token(force_refresh=True, redis_client=redis_client)
        headers["Authorization"] = f"Bearer {token}"
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Spotify {method} request failed") from exc
    return response


def _raise_for_spotify_status(response: requests.Response, action: str) -> None:
    if response.status_code in (200, 201, 204):
        return
    raise RuntimeError(f"Spotify {action} returned HTTP {response.status_code}")


def track_id_from_item(item: dict[str, Any] | None) -> str | None:
    """Return a playable track id, or None for episodes / local files."""
    if not isinstance(item, dict):
        return None
    track = item.get("track")
    if not isinstance(track, dict):
        return None
    track_type = track.get("type")
    if track_type and track_type != "track":
        return None
    track_id = track.get("id")
    if not track_id or not isinstance(track_id, str):
        return None
    return track_id


def library_contains_track(
    track_id: str,
    *,
    access_token: str | None = None,
    redis_client=None,
) -> bool:
    response = spotify_request(
        "GET",
        SPOTIFY_SAVED_TRACKS_CONTAINS_URL,
        access_token=access_token,
        redis_client=redis_client,
        params={"ids": track_id},
    )
    _raise_for_spotify_status(response, "library-contains")
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("Spotify library-contains returned non-JSON") from exc
    if not isinstance(body, list) or not body:
        return False
    return bool(body[0])


def save_track_to_library(
    track_id: str,
    *,
    access_token: str | None = None,
    redis_client=None,
) -> None:
    response = spotify_request(
        "PUT",
        SPOTIFY_SAVED_TRACKS_URL,
        access_token=access_token,
        redis_client=redis_client,
        params={"ids": track_id},
    )
    _raise_for_spotify_status(response, "save-track")


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
