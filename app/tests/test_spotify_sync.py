"""Spotify library/playlist job tests (refresh-on-demand, no token cron)."""

import logging
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"
os.environ.setdefault("SPOTIFY_CLIENT_ID", "test-client-id")
os.environ.setdefault("SPOTIFY_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("SPOTIFY_REFRESH_TOKEN", "test-refresh-token")
os.environ.setdefault("SHAZAM_PLAYLIST_ID", "shazam-playlist-id")

from services.spotify.saved_tracks import (
    ACCESS_TOKEN_REDIS_KEY,
    SPOTIFY_TOKEN_URL,
    get_spotify_access_token,
    refresh_spotify_access_token,
    spotify_request,
)
from services.spotify.sync import (
    SHAZAM_WATERMARK_REDIS_KEY,
    TRACK_SAVE_SPACING_SECONDS,
    create_half_year_playlist,
    half_year_playlist_name,
    sync_saved_today_to_half_year,
    sync_shazam_to_library,
)

LA = pytz.timezone("America/Los_Angeles")


class FakeRedis:
    def __init__(self, initial=None):
        self.store = dict(initial or {})
        self.ttls = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex


def _token_response(access_token="fresh-access-token", expires_in=3600, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = {
        "access_token": access_token,
        "expires_in": expires_in,
        "token_type": "Bearer",
    }
    return response


def _http_response(status=200, payload=None):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload if payload is not None else {}
    return response


def _playlist_item(track_id, added_at, name=None):
    return {
        "added_at": added_at,
        "track": {
            "id": track_id,
            "type": "track",
            "name": name or track_id,
            "uri": f"spotify:track:{track_id}",
        },
    }


def _saved_item(track_id, added_at):
    return _playlist_item(track_id, added_at)


# ---------------------------------------------------------------------------
# Refresh-on-demand
# ---------------------------------------------------------------------------


def test_refresh_spotify_access_token_does_not_log_secrets(caplog):
    refresh_secret = "super-secret-refresh-token"
    access_secret = "super-secret-access-token"

    def fake_post(url, **kwargs):
        assert url == SPOTIFY_TOKEN_URL
        assert kwargs["data"]["refresh_token"] == refresh_secret
        return _token_response(access_secret)

    with caplog.at_level(logging.DEBUG), patch(
        "services.spotify.saved_tracks.requests.post", side_effect=fake_post
    ):
        token = refresh_spotify_access_token("id", "secret", refresh_secret)

    assert token == access_secret
    assert refresh_secret not in caplog.text
    assert access_secret not in caplog.text
    assert "Authorization" not in caplog.text


def test_get_access_token_uses_hub_redis_cache():
    cache = FakeRedis({ACCESS_TOKEN_REDIS_KEY: "cached-access-token"})

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("token endpoint should not be called when cache hits")

    with patch("services.spotify.saved_tracks.requests.post", side_effect=fail_if_called):
        token = get_spotify_access_token(
            "id",
            "secret",
            "refresh",
            redis_client=cache,
        )

    assert token == "cached-access-token"


def test_get_access_token_refreshes_when_missing_and_caches_ttl():
    cache = FakeRedis()

    with patch(
        "services.spotify.saved_tracks.requests.post",
        return_value=_token_response("fresh-access-token", expires_in=3600),
    ) as mock_post:
        token = get_spotify_access_token(
            "id",
            "secret",
            "refresh",
            redis_client=cache,
        )

    assert token == "fresh-access-token"
    assert cache.get(ACCESS_TOKEN_REDIS_KEY) == "fresh-access-token"
    assert cache.ttls[ACCESS_TOKEN_REDIS_KEY] == 3540
    mock_post.assert_called_once()


def test_get_access_token_force_refresh_bypasses_cache():
    cache = FakeRedis({ACCESS_TOKEN_REDIS_KEY: "stale-access-token"})

    with patch(
        "services.spotify.saved_tracks.requests.post",
        return_value=_token_response("rotated-access-token", expires_in=3600),
    ):
        token = get_spotify_access_token(
            "id",
            "secret",
            "refresh",
            force_refresh=True,
            redis_client=cache,
        )

    assert token == "rotated-access-token"
    assert cache.get(ACCESS_TOKEN_REDIS_KEY) == "rotated-access-token"


def test_spotify_request_retries_once_on_401():
    cache = FakeRedis()
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs["headers"].get("Authorization")))
        if len(calls) == 1:
            return _http_response(401, {"error": "invalid"})
        return _http_response(200, {"ok": True})

    with patch(
        "services.spotify.saved_tracks.get_spotify_access_token",
        side_effect=["expired-token", "fresh-token"],
    ), patch(
        "services.spotify.saved_tracks.requests.request",
        side_effect=fake_request,
    ):
        response = spotify_request(
            "GET",
            "https://api.spotify.com/v1/me/tracks",
            redis_client=cache,
        )

    assert response.status_code == 200
    assert calls[0][2] == "Bearer expired-token"
    assert calls[1][2] == "Bearer fresh-token"
    assert "expired-token" not in str(response.json.return_value)
    assert "fresh-token" not in str(response.json.return_value)


# ---------------------------------------------------------------------------
# Shazam → Liked Songs
# ---------------------------------------------------------------------------


def test_shazam_missing_watermark_seeds_now_and_saves_nothing():
    cache = FakeRedis()
    now = datetime(2026, 9, 12, 15, 53, tzinfo=timezone.utc)

    def fail_spotify(*_args, **_kwargs):
        raise AssertionError("Spotify should not be called when seeding")

    with patch("services.spotify.sync.redis_client", cache), patch(
        "services.spotify.sync.iter_playlist_items", side_effect=fail_spotify
    ), patch(
        "services.spotify.sync.save_track_to_library", side_effect=fail_spotify
    ):
        result = sync_shazam_to_library(now=now, sleep=lambda _: None)

    assert result["status"] == "seeded"
    assert result["saved"] == 0
    assert result["watermark"] == "2026-09-12T15:53:00Z"
    assert cache.get(SHAZAM_WATERMARK_REDIS_KEY) == "2026-09-12T15:53:00Z"


def test_shazam_advances_watermark_only_over_saved_tracks():
    cache = FakeRedis({SHAZAM_WATERMARK_REDIS_KEY: "2026-09-12T10:00:00Z"})
    saved = []

    items = [
        _playlist_item("old", "2026-09-12T09:00:00Z"),
        _playlist_item("first", "2026-09-12T11:00:00Z"),
        _playlist_item("second", "2026-09-12T12:00:00Z"),
        _playlist_item("third", "2026-09-12T13:00:00Z"),
    ]

    def fake_save(track_id, **_kwargs):
        if track_id == "second":
            raise RuntimeError("Spotify save-track returned HTTP 500")
        saved.append(track_id)

    sleeps = []

    with patch("services.spotify.sync.redis_client", cache), patch(
        "services.spotify.sync.get_spotify_access_token", return_value="tok"
    ), patch(
        "services.spotify.sync.iter_playlist_items", return_value=items
    ), patch(
        "services.spotify.sync.library_contains_track", return_value=False
    ), patch(
        "services.spotify.sync.save_track_to_library", side_effect=fake_save
    ):
        with pytest.raises(RuntimeError, match="HTTP 500"):
            sync_shazam_to_library(sleep=sleeps.append)

    assert saved == ["first"]
    assert cache.get(SHAZAM_WATERMARK_REDIS_KEY) == "2026-09-12T11:00:00Z"
    assert sleeps == [TRACK_SAVE_SPACING_SECONDS]


def test_shazam_skips_already_liked_without_resaving():
    cache = FakeRedis({SHAZAM_WATERMARK_REDIS_KEY: "2026-09-12T10:00:00Z"})
    saved = []
    items = [
        _playlist_item("already", "2026-09-12T11:00:00Z"),
        _playlist_item("fresh", "2026-09-12T12:00:00Z"),
    ]

    def contains(track_id, **_kwargs):
        return track_id == "already"

    with patch("services.spotify.sync.redis_client", cache), patch(
        "services.spotify.sync.get_spotify_access_token", return_value="tok"
    ), patch(
        "services.spotify.sync.iter_playlist_items", return_value=items
    ), patch(
        "services.spotify.sync.library_contains_track", side_effect=contains
    ), patch(
        "services.spotify.sync.save_track_to_library", side_effect=saved.append
    ):
        result = sync_shazam_to_library(sleep=lambda _: None)

    assert saved == ["fresh"]
    assert result["saved"] == 1
    assert result["already_saved"] == 1
    assert cache.get(SHAZAM_WATERMARK_REDIS_KEY) == "2026-09-12T12:00:00Z"


def test_shazam_unparseable_watermark_raises():
    cache = FakeRedis({SHAZAM_WATERMARK_REDIS_KEY: "not-a-timestamp"})

    with patch("services.spotify.sync.redis_client", cache):
        with pytest.raises(RuntimeError, match="not a valid timestamp"):
            sync_shazam_to_library(sleep=lambda _: None)


# ---------------------------------------------------------------------------
# Liked Songs → half-year playlist
# ---------------------------------------------------------------------------


def test_half_year_playlist_name_h1_and_h2():
    assert half_year_playlist_name(LA.localize(datetime(2026, 1, 1, 0, 5))) == "2026 - 1/2"
    assert half_year_playlist_name(LA.localize(datetime(2026, 6, 30, 23, 0))) == "2026 - 1/2"
    assert half_year_playlist_name(LA.localize(datetime(2026, 7, 1, 0, 5))) == "2026 - 2/2"
    assert half_year_playlist_name(LA.localize(datetime(2027, 12, 31, 12, 0))) == "2027 - 2/2"


def test_drain_adds_today_tracks_not_already_in_playlist():
    now = LA.localize(datetime(2026, 9, 12, 16, 20))
    # Saved Tracks API is newest-first; older-than-today stops pagination.
    today = [
        _saved_item("new-two", "2026-09-12T20:00:00Z"),
        _saved_item("new-one", "2026-09-12T19:00:00Z"),
        _saved_item("already", "2026-09-12T18:00:00Z"),
        _saved_item("old-day", "2026-09-11T20:00:00Z"),
    ]
    added = []
    sleeps = []

    with patch("services.spotify.sync.get_spotify_access_token", return_value="tok"), patch(
        "services.spotify.sync.find_playlist_by_name",
        return_value={"id": "half-id", "name": "2026 - 2/2"},
    ), patch(
        "services.spotify.sync.iter_saved_track_items", return_value=today
    ), patch(
        "services.spotify.sync.playlist_track_ids", return_value={"already"}
    ), patch(
        "services.spotify.sync.add_track_to_playlist",
        side_effect=lambda playlist_id, uri, **_k: added.append((playlist_id, uri)),
    ):
        result = sync_saved_today_to_half_year(now=now, sleep=sleeps.append)

    assert result["status"] == "ok"
    assert result["playlist"] == "2026 - 2/2"
    assert result["added"] == 2
    assert result["skipped"] == 1
    assert added == [
        ("half-id", "spotify:track:new-one"),
        ("half-id", "spotify:track:new-two"),
    ]
    assert sleeps == [TRACK_SAVE_SPACING_SECONDS, TRACK_SAVE_SPACING_SECONDS]


def test_drain_skips_when_half_year_playlist_missing():
    now = LA.localize(datetime(2026, 9, 12, 16, 20))

    with patch("services.spotify.sync.get_spotify_access_token", return_value="tok"), patch(
        "services.spotify.sync.find_playlist_by_name", return_value=None
    ), patch(
        "services.spotify.sync.add_track_to_playlist"
    ) as mock_add:
        result = sync_saved_today_to_half_year(now=now, sleep=lambda _: None)

    assert result["status"] == "playlist_missing"
    assert result["playlist"] == "2026 - 2/2"
    assert result["added"] == 0
    mock_add.assert_not_called()


# ---------------------------------------------------------------------------
# Half-year playlist create
# ---------------------------------------------------------------------------


def test_create_half_year_playlist_creates_when_missing():
    now = LA.localize(datetime(2026, 7, 1, 0, 5))

    with patch("services.spotify.sync.get_spotify_access_token", return_value="tok"), patch(
        "services.spotify.sync.find_playlist_by_name", return_value=None
    ), patch(
        "services.spotify.sync.create_playlist",
        return_value={"id": "new-pl", "name": "2026 - 2/2"},
    ) as mock_create:
        result = create_half_year_playlist(now=now)

    assert result["status"] == "created"
    assert result["playlist"] == "2026 - 2/2"
    assert result["playlist_id"] == "new-pl"
    mock_create.assert_called_once_with(
        "2026 - 2/2",
        public=False,
        description="Liked Songs H2 2026",
    )


def test_create_half_year_playlist_skips_when_exists():
    now = LA.localize(datetime(2026, 1, 1, 0, 5))

    with patch("services.spotify.sync.get_spotify_access_token", return_value="tok"), patch(
        "services.spotify.sync.find_playlist_by_name",
        return_value={"id": "existing", "name": "2026 - 1/2"},
    ), patch("services.spotify.sync.create_playlist") as mock_create:
        result = create_half_year_playlist(now=now)

    assert result["status"] == "exists"
    assert result["playlist"] == "2026 - 1/2"
    assert result["playlist_id"] == "existing"
    mock_create.assert_not_called()
