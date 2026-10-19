"""Tests for Week-Ending note creation date math and Dropbox writes."""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import dropbox
import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SYSTEM_TIMEZONE", "America/Los_Angeles")
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/test/vault")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")

from scripts.obsidian.workflows.file_creation import create_weeks as mod

MODULE = "scripts.obsidian.workflows.file_creation.create_weeks"
LA = pytz.timezone("America/Los_Angeles")
VAULT = "/test/vault"


@pytest.fixture(autouse=True)
def _pin_vault_path(monkeypatch):
    """Full-suite imports may have set DROPBOX_OBSIDIAN_VAULT_PATH first."""
    monkeypatch.setenv("DROPBOX_OBSIDIAN_VAULT_PATH", VAULT)
WEEKLY = "/test/vault/02_Weekly"
WEEKS = "/test/vault/02_Weekly/_Weeks"
WEEK_FILE = f"{WEEKS}/Week-Ending-2026-11-01.md"


def _dt(year, month, day, hour=2, minute=40):
    return LA.localize(datetime(year, month, day, hour, minute))


@pytest.mark.parametrize(
    "now, expected",
    [
        (_dt(2026, 10, 24, 2, 40), "2026-11-01"),  # Saturday scheduled slot
        (_dt(2026, 10, 25, 23, 0), "2026-11-01"),  # former Sunday 23:00 slot
        (_dt(2026, 10, 21, 12, 0), "2026-11-01"),  # midweek Wednesday
        (_dt(2026, 10, 19, 9, 0), "2026-11-01"),  # Monday still targets next week
        (_dt(2026, 10, 26, 8, 0), "2026-11-08"),  # following Monday → week after
    ],
)
def test_next_week_ending_sunday_is_sunday_after_next_monday(now, expected):
    """Target is the Sunday that ends the week starting the next Monday."""
    target = mod.next_week_ending_sunday(now)
    assert target.strftime("%Y-%m-%d") == expected
    assert target.weekday() == 6


def _folder(name: str, path_display: str):
    return dropbox.files.FolderMetadata(
        name=name,
        id=f"id:{name}",
        path_lower=path_display.lower(),
        path_display=path_display,
    )


def _file(name: str, path_display: str):
    return dropbox.files.FileMetadata(
        name=name,
        id=f"id:{name}",
        client_modified=datetime(2026, 10, 24, 2, 40),
        server_modified=datetime(2026, 10, 24, 2, 40),
        rev="aaaaaaaaaaaaaaaa",
        size=16,
        path_lower=path_display.lower(),
        path_display=path_display,
    )


def _list_result(entries):
    result = MagicMock()
    result.entries = entries
    result.has_more = False
    return result


def _listings(*, week_files=None):
    week_files = week_files if week_files is not None else []
    return {
        VAULT: [_folder("01_Daily", f"{VAULT}/01_Daily"), _folder("02_Weekly", WEEKLY)],
        WEEKLY: [_folder("_Weeks", WEEKS)],
        WEEKS: week_files,
        VAULT.lower(): [_folder("01_Daily", f"{VAULT}/01_Daily"), _folder("02_Weekly", WEEKLY)],
        WEEKLY.lower(): [_folder("_Weeks", WEEKS)],
        WEEKS.lower(): week_files,
    }


def _mock_dbx(listings):
    mock_dbx = MagicMock()

    def list_folder(path):
        for key, entries in listings.items():
            if key.lower() == path.lower():
                return _list_result(entries)
        return _list_result([])

    mock_dbx.files_list_folder.side_effect = list_folder
    mock_dbx.files_upload.return_value = MagicMock(rev="bbbbbbbbbbbbbbbb")
    return mock_dbx


def test_create_weeks_saturday_creates_week_ending_nov_1():
    """Sat 2026-10-24 02:40 creates the same file Sunday 23:00 on Oct 25 would."""
    mock_dbx = _mock_dbx(_listings())

    with (
        patch(f"{MODULE}._get_dropbox_client", return_value=mock_dbx),
        patch(f"{MODULE}._now", return_value=_dt(2026, 10, 24, 2, 40)),
    ):
        assert mod.create_weeks() is True

    mock_dbx.files_upload.assert_called_once()
    args, kwargs = mock_dbx.files_upload.call_args
    assert args[1] == WEEK_FILE
    assert kwargs["mode"].is_add()
    assert not kwargs["mode"].is_overwrite()
    assert kwargs["autorename"] is False
    uploaded = args[0].decode("utf-8")
    assert "# Weekly Artifacts: 2026-10-26 to 2026-11-01" in uploaded


def test_create_weeks_skips_when_file_exists_case_insensitive():
    existing = _file("week-ending-2026-11-01.md", f"{WEEKS}/week-ending-2026-11-01.md")
    mock_dbx = _mock_dbx(_listings(week_files=[existing]))

    with (
        patch(f"{MODULE}._get_dropbox_client", return_value=mock_dbx),
        patch(f"{MODULE}._now", return_value=_dt(2026, 10, 24, 2, 40)),
    ):
        assert mod.create_weeks() is True

    mock_dbx.files_upload.assert_not_called()


def test_ensure_week_ending_file_creates_specific_sunday_not_next_week():
    """Self-heal path: create the journal's week, not the scheduled next week."""
    mock_dbx = _mock_dbx(_listings())
    stem = mod.ensure_week_ending_file(
        mock_dbx, VAULT, datetime(2026, 10, 25)
    )
    assert stem == "Week-Ending-2026-10-25"
    args, kwargs = mock_dbx.files_upload.call_args
    assert args[1] == f"{WEEKS}/Week-Ending-2026-10-25.md"
    assert kwargs["mode"].is_add()
    uploaded = args[0].decode("utf-8")
    assert "# Weekly Artifacts: 2026-10-19 to 2026-10-25" in uploaded
