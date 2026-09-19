"""Music of the Day: Liked Songs → daily-journal section."""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import dropbox
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
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")

from services.spotify.music_of_the_day import (  # noqa: E402
    MUSIC_OF_THE_DAY_HEADER,
    format_track_line,
    liked_tracks_for_local_day,
    merge_music_of_the_day_section,
    previous_calendar_day,
    resolve_journal_day,
    write_music_of_the_day,
)

LA = pytz.timezone("America/Los_Angeles")
JOURNAL_FOLDER = "/obsidian/personal/01_daily/_journal"
JOURNAL_PATH = f"{JOURNAL_FOLDER}/Sep 19, 2026.md"
REV_MATCH = "0123456789abcdef"
REV_STALE = "fedcba9876543210"
REV_NEW = "aaaaaaaaaaaaaaaa"

SAMPLE_JOURNAL = """---
date: 2026-09-19
---

# Sep 19, 2026

### Morning Pages
- something

### Content Buffet:
- existing item

### Transcript Notes
- older meeting
"""


def _saved_item(track_id, added_at, name=None, artists=None, url=None):
    return {
        "added_at": added_at,
        "track": {
            "id": track_id,
            "type": "track",
            "name": name or track_id,
            "artists": [{"name": artist} for artist in (artists or ["Artist"])],
            "external_urls": {
                "spotify": url or f"https://open.spotify.com/track/{track_id}"
            },
        },
    }


def _download(content: str, *, rev: str = REV_MATCH, path: str = JOURNAL_PATH):
    metadata = MagicMock()
    metadata.rev = rev
    metadata.path_display = path
    metadata.name = "Sep 19, 2026.md"
    response = MagicMock()
    response.content = content.encode("utf-8")
    return metadata, response


def _rev_conflict_api_error() -> dropbox.exceptions.ApiError:
    reason = dropbox.files.WriteError.conflict(dropbox.files.WriteConflictError.file)
    failed = dropbox.files.UploadWriteFailed(reason=reason, upload_session_id="sess")
    error = dropbox.files.UploadError.path(failed)
    return dropbox.exceptions.ApiError("req", error, "", "")


def _assert_update_mode(call, expected_rev: str) -> None:
    mode = call.kwargs["mode"]
    assert mode.is_update(), mode
    assert mode.get_update() == expected_rev
    assert not mode.is_overwrite()
    assert call.kwargs.get("autorename") is False


def test_previous_calendar_day_at_3am_is_yesterday():
    now = LA.localize(datetime(2026, 9, 20, 3, 0))
    assert previous_calendar_day(now).isoformat() == "2026-09-19"


def test_resolve_journal_day_explicit_iso_and_default():
    assert resolve_journal_day(date_value="2026-09-19").isoformat() == "2026-09-19"
    now = LA.localize(datetime(2026, 9, 20, 3, 0))
    assert resolve_journal_day(now=now).isoformat() == "2026-09-19"


def test_resolve_journal_day_rejects_bad_iso():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        resolve_journal_day(date_value="09/19/2026")


def test_liked_tracks_for_local_day_filters_and_orders():
    # Sep 19 00:00 PT = 07:00 UTC. Newest-first Saved Tracks page.
    items = [
        _saved_item("next-day", "2026-09-20T08:00:00Z", name="Tomorrow"),
        _saved_item(
            "late",
            "2026-09-19T20:00:00Z",
            name="Evening Song",
            artists=["B Artist"],
        ),
        _saved_item(
            "early",
            "2026-09-19T08:00:00Z",
            name="Morning Song",
            artists=["A Artist"],
        ),
        _saved_item(
            "prev-local",
            "2026-09-19T06:00:00Z",
            name="Still Friday PT",
        ),
        _saved_item("old", "2026-09-18T20:00:00Z", name="Older"),
    ]

    with patch(
        "services.spotify.music_of_the_day.iter_saved_track_items",
        return_value=items,
    ):
        selected = liked_tracks_for_local_day("tok", resolve_journal_day(date_value="2026-09-19"))

    assert [track["id"] for track in selected] == ["early", "late"]
    assert selected[0]["name"] == "Morning Song"
    assert selected[1]["artists"] == "B Artist"


def test_format_track_line_uses_spotify_https_and_em_dash():
    line = format_track_line(
        {
            "id": "abc123",
            "name": "Song [Live]",
            "artists": "The Band",
            "url": "https://open.spotify.com/track/abc123",
        }
    )
    assert line == "- [Song \\[Live\\]](https://open.spotify.com/track/abc123) — The Band"


def test_merge_builds_section_from_tracks_before_transcript_notes():
    tracks = [
        {
            "id": "early",
            "name": "Morning Song",
            "artists": "A Artist",
            "url": "https://open.spotify.com/track/early",
        },
        {
            "id": "late",
            "name": "Evening Song",
            "artists": "B Artist",
            "url": "https://open.spotify.com/track/late",
        },
    ]

    updated = merge_music_of_the_day_section(SAMPLE_JOURNAL, tracks)

    assert updated is not None
    assert updated.count(MUSIC_OF_THE_DAY_HEADER) == 1
    section_idx = updated.index(MUSIC_OF_THE_DAY_HEADER)
    transcript_idx = updated.index("### Transcript Notes")
    assert section_idx < transcript_idx
    assert "- [Morning Song](https://open.spotify.com/track/early) — A Artist" in updated
    assert "- [Evening Song](https://open.spotify.com/track/late) — B Artist" in updated
    morning_idx = updated.index("Morning Song")
    evening_idx = updated.index("Evening Song")
    assert morning_idx < evening_idx
    assert updated[transcript_idx:].startswith("### Transcript Notes\n- older meeting")


def test_merge_idempotent_second_run_adds_nothing():
    tracks = [
        {
            "id": "early",
            "name": "Morning Song",
            "artists": "A Artist",
            "url": "https://open.spotify.com/track/early",
        }
    ]
    once = merge_music_of_the_day_section(SAMPLE_JOURNAL, tracks)
    twice = merge_music_of_the_day_section(once, tracks)
    assert twice is None


def test_merge_appends_missing_track_without_reshuffling():
    existing = merge_music_of_the_day_section(
        SAMPLE_JOURNAL,
        [
            {
                "id": "early",
                "name": "Morning Song",
                "artists": "A Artist",
                "url": "https://open.spotify.com/track/early",
            }
        ],
    )
    updated = merge_music_of_the_day_section(
        existing,
        [
            {
                "id": "late",
                "name": "Evening Song",
                "artists": "B Artist",
                "url": "https://open.spotify.com/track/late",
            },
            {
                "id": "early",
                "name": "Morning Song Renamed",
                "artists": "A Artist",
                "url": "https://open.spotify.com/track/early",
            },
        ],
    )

    assert updated is not None
    assert updated.count(MUSIC_OF_THE_DAY_HEADER) == 1
    assert "Morning Song Renamed" not in updated
    morning_idx = updated.index("Morning Song")
    evening_idx = updated.index("Evening Song")
    assert morning_idx < evening_idx


def test_merge_empty_tracks_returns_none():
    assert merge_music_of_the_day_section(SAMPLE_JOURNAL, []) is None


def _write_motd(tracks, mock_dbx):
    return (
        patch(
            "services.spotify.music_of_the_day.get_spotify_access_token",
            return_value="tok",
        ),
        patch(
            "services.spotify.music_of_the_day.liked_tracks_for_local_day",
            return_value=tracks,
        ),
        patch(
            "services.spotify.music_of_the_day._get_dropbox_client",
            return_value=mock_dbx,
        ),
        patch(
            "services.spotify.music_of_the_day._resolve_journal_folder",
            return_value=JOURNAL_FOLDER,
        ),
        patch("services.spotify.music_of_the_day.record_deferred_write"),
    )


def test_empty_day_skips_dropbox_entirely():
    mock_dbx = MagicMock()
    token, liked, dbx, folder, enqueue = _write_motd([], mock_dbx)

    with token, liked, dbx, folder, enqueue as mock_enqueue:
        result = write_music_of_the_day(date="2026-09-19")

    assert result["status"] == "empty"
    assert result["tracks"] == 0
    assert result["journal"] == "Sep 19, 2026.md"
    mock_dbx.files_download.assert_not_called()
    mock_dbx.files_upload.assert_not_called()
    mock_enqueue.assert_not_called()


def test_write_builds_section_and_uploads_rev_safe():
    tracks = [
        {
            "id": "early",
            "name": "Morning Song",
            "artists": "A Artist",
            "url": "https://open.spotify.com/track/early",
            "added_at": LA.localize(datetime(2026, 9, 19, 1, 0)),
        }
    ]
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(SAMPLE_JOURNAL)
    mock_dbx.files_upload.return_value = MagicMock()

    token, liked, dbx, folder, enqueue = _write_motd(tracks, mock_dbx)

    with token, liked, dbx, folder, enqueue as mock_enqueue:
        result = write_music_of_the_day(date="2026-09-19")

    assert result["status"] == "updated"
    assert result["tracks"] == 1
    assert result["inserted"] == 1
    mock_dbx.files_upload.assert_called_once()
    mock_enqueue.assert_not_called()
    _assert_update_mode(mock_dbx.files_upload.call_args, REV_MATCH)
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    assert MUSIC_OF_THE_DAY_HEADER in uploaded
    assert "- [Morning Song](https://open.spotify.com/track/early) — A Artist" in uploaded


def test_write_idempotent_second_run_skips_upload():
    journal = merge_music_of_the_day_section(
        SAMPLE_JOURNAL,
        [
            {
                "id": "early",
                "name": "Morning Song",
                "artists": "A Artist",
                "url": "https://open.spotify.com/track/early",
            }
        ],
    )
    tracks = [
        {
            "id": "early",
            "name": "Morning Song",
            "artists": "A Artist",
            "url": "https://open.spotify.com/track/early",
            "added_at": LA.localize(datetime(2026, 9, 19, 1, 0)),
        }
    ]
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(journal)

    token, liked, dbx, folder, enqueue = _write_motd(tracks, mock_dbx)

    with token, liked, dbx, folder, enqueue as mock_enqueue:
        result = write_music_of_the_day(date="2026-09-19")

    assert result["status"] == "skipped"
    assert result["inserted"] == 0
    mock_dbx.files_upload.assert_not_called()
    mock_enqueue.assert_not_called()


def test_rev_conflict_retries_once_then_succeeds():
    tracks = [
        {
            "id": "early",
            "name": "Morning Song",
            "artists": "A Artist",
            "url": "https://open.spotify.com/track/early",
            "added_at": LA.localize(datetime(2026, 9, 19, 1, 0)),
        }
    ]
    mock_dbx = MagicMock()
    mock_dbx.files_download.side_effect = [
        _download(SAMPLE_JOURNAL, rev=REV_STALE),
        _download(SAMPLE_JOURNAL, rev=REV_NEW),
    ]
    mock_dbx.files_upload.side_effect = [
        _rev_conflict_api_error(),
        MagicMock(),
    ]

    token, liked, dbx, folder, enqueue = _write_motd(tracks, mock_dbx)

    with token, liked, dbx, folder, enqueue as mock_enqueue:
        result = write_music_of_the_day(date="2026-09-19")

    assert result["status"] == "updated"
    assert mock_dbx.files_download.call_count == 2
    assert mock_dbx.files_upload.call_count == 2
    _assert_update_mode(mock_dbx.files_upload.call_args_list[0], REV_STALE)
    _assert_update_mode(mock_dbx.files_upload.call_args_list[1], REV_NEW)
    mock_enqueue.assert_not_called()


def test_rev_conflict_after_retry_defers():
    tracks = [
        {
            "id": "early",
            "name": "Morning Song",
            "artists": "A Artist",
            "url": "https://open.spotify.com/track/early",
            "added_at": LA.localize(datetime(2026, 9, 19, 1, 0)),
        }
    ]
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(SAMPLE_JOURNAL, rev=REV_STALE)
    mock_dbx.files_upload.side_effect = _rev_conflict_api_error()

    token, liked, dbx, folder, enqueue = _write_motd(tracks, mock_dbx)

    with token, liked, dbx, folder, enqueue as mock_enqueue:
        result = write_music_of_the_day(date="2026-09-19")

    assert result["status"] == "deferred"
    assert mock_dbx.files_upload.call_count == 2
    for call in mock_dbx.files_upload.call_args_list:
        assert not call.kwargs["mode"].is_overwrite()
    mock_enqueue.assert_called_once_with(
        source="spotify",
        kind="music_of_the_day",
        payload_ref="2026-09-19",
        target=JOURNAL_PATH,
        payload={"date": "2026-09-19"},
    )


def test_missing_journal_skips_upload():
    tracks = [
        {
            "id": "early",
            "name": "Morning Song",
            "artists": "A Artist",
            "url": "https://open.spotify.com/track/early",
            "added_at": LA.localize(datetime(2026, 9, 19, 1, 0)),
        }
    ]
    mock_dbx = MagicMock()
    mock_dbx.files_download.side_effect = FileNotFoundError("Journal not found")

    token, liked, dbx, folder, enqueue = _write_motd(tracks, mock_dbx)

    with token, liked, dbx, folder, enqueue as mock_enqueue:
        result = write_music_of_the_day(date="2026-09-19")

    assert result["status"] == "skipped_missing_journal"
    mock_dbx.files_upload.assert_not_called()
    mock_enqueue.assert_not_called()
