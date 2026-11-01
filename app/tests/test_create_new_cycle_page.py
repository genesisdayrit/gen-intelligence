"""Tests for weekly cycle note creation, numbering, and day headings."""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import dropbox
import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SYSTEM_TIMEZONE", "America/Los_Angeles")
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")

from scripts.obsidian.workflows.file_creation import create_new_cycle_page as mod
from services.obsidian.add_weekly_cycle_completed import DAY_SECTION_PATTERN
from services.obsidian.add_weekly_cycle_updates import (
    DAY_SECTION_PATTERN as UPDATES_DAY_SECTION_PATTERN,
)

MODULE = "scripts.obsidian.workflows.file_creation.create_new_cycle_page"
LA = pytz.timezone("America/Los_Angeles")
VAULT = "/obsidian/personal"
CYCLES = "/obsidian/personal/04_Cycles"
WEEKLY_CYCLES = "/obsidian/personal/04_Cycles/_Weekly-Cycles"
CYCLE_151 = "Cycle 151 (Oct. 28 - Nov. 03, 2026).md"
CYCLE_151_PATH = f"{WEEKLY_CYCLES}/{CYCLE_151}"
CYCLE_150 = "Cycle 150 (Oct. 21 - Oct. 27, 2026).md"


@pytest.fixture(autouse=True)
def _pin_vault_path(monkeypatch):
    monkeypatch.setenv("DROPBOX_OBSIDIAN_VAULT_PATH", VAULT)


def _dt(year, month, day, hour=1, minute=30):
    return LA.localize(datetime(year, month, day, hour, minute))


@pytest.mark.parametrize(
    "now, expected",
    [
        (_dt(2026, 10, 27, 1, 30), "2026-10-28"),  # Tuesday scheduled slot
        (_dt(2026, 10, 28, 1, 30), "2026-11-04"),  # Wednesday still targets next week
        (_dt(2026, 10, 26, 12, 0), "2026-10-28"),  # Monday → this week's Wednesday
    ],
)
def test_next_cycle_wednesday_is_wednesday_after_now(now, expected):
    target = mod.next_cycle_wednesday(now)
    assert target.strftime("%Y-%m-%d") == expected
    assert target.weekday() == 2


def test_cycle_bounds_for_date_are_wed_tue_containing_target():
    start, end = mod.cycle_bounds_for_date(datetime(2026, 10, 29))  # Thursday
    assert start.strftime("%Y-%m-%d") == "2026-10-28"
    assert end.strftime("%Y-%m-%d") == "2026-11-03"
    assert start.weekday() == 2
    assert end.weekday() == 1


def test_day_headings_match_weekly_cycle_writer_search():
    assert mod.WEEKLY_CYCLE_DAY_HEADINGS == (
        "### Wednesday -",
        "### Thursday -",
        "### Friday -",
        "### Saturday -",
        "### Sunday -",
        "### Monday -",
        "### Tuesday -",
    )
    for heading in mod.WEEKLY_CYCLE_DAY_HEADINGS:
        assert heading == f"### {heading[4:-2]} -"
        assert DAY_SECTION_PATTERN.match(heading)
        assert UPDATES_DAY_SECTION_PATTERN.match(heading)
        assert heading.strip() == heading


def test_build_cycle_content_includes_dates_and_writer_headings():
    start, end = mod.cycle_bounds_for_date(datetime(2026, 10, 28))
    content = mod.build_cycle_content(start, end)
    assert content.startswith("Cycle Start Date: 2026-10-28\nCycle End Date: 2026-11-03\n")
    lines = [line.strip() for line in content.splitlines()]
    for heading in mod.WEEKLY_CYCLE_DAY_HEADINGS:
        assert heading in lines
        assert f"Could not find day section '{heading}'" not in content


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
        client_modified=datetime(2026, 10, 27, 1, 30),
        server_modified=datetime(2026, 10, 27, 1, 30),
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


def _listings(*, cycle_files=None):
    cycle_files = cycle_files if cycle_files is not None else []
    mapping = {
        VAULT: [_folder("04_Cycles", CYCLES)],
        CYCLES: [_folder("_Weekly-Cycles", WEEKLY_CYCLES)],
        WEEKLY_CYCLES: cycle_files,
    }
    return {key.lower(): value for key, value in mapping.items()} | mapping


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


def test_create_new_cycle_page_tuesday_creates_cycle_151_with_headings():
    """Tue 2026-10-27 01:30 PT creates Cycle 151 (Oct. 28 - Nov. 03, 2026)."""
    existing = _file(CYCLE_150, f"{WEEKLY_CYCLES}/{CYCLE_150}")
    mock_dbx = _mock_dbx(_listings(cycle_files=[existing]))

    with (
        patch(f"{MODULE}._get_dropbox_client", return_value=mock_dbx),
        patch(f"{MODULE}._now", return_value=_dt(2026, 10, 27, 1, 30)),
    ):
        assert mod.create_new_cycle_page() is True

    mock_dbx.files_upload.assert_called_once()
    args, kwargs = mock_dbx.files_upload.call_args
    assert args[1] == CYCLE_151_PATH
    assert kwargs["mode"].is_add()
    assert not kwargs["mode"].is_overwrite()
    assert kwargs["autorename"] is False
    uploaded = args[0].decode("utf-8")
    assert "Cycle Start Date: 2026-10-28" in uploaded
    assert "Cycle End Date: 2026-11-03" in uploaded
    for heading in mod.WEEKLY_CYCLE_DAY_HEADINGS:
        assert heading in uploaded.splitlines()


def test_create_new_cycle_page_skips_when_date_range_exists_case_insensitive():
    existing = _file(
        "cycle 151 (oct. 28 - nov. 03, 2026).md",
        f"{WEEKLY_CYCLES}/cycle 151 (oct. 28 - nov. 03, 2026).md",
    )
    mock_dbx = _mock_dbx(_listings(cycle_files=[existing]))

    with (
        patch(f"{MODULE}._get_dropbox_client", return_value=mock_dbx),
        patch(f"{MODULE}._now", return_value=_dt(2026, 10, 27, 1, 30)),
    ):
        assert mod.create_new_cycle_page() is True

    mock_dbx.files_upload.assert_not_called()


def test_ensure_weekly_cycle_file_uses_target_date_not_next_wednesday():
    """Self-heal path: create the journal's cycle, not the scheduled next week."""
    existing = _file(CYCLE_150, f"{WEEKLY_CYCLES}/{CYCLE_150}")
    mock_dbx = _mock_dbx(_listings(cycle_files=[existing]))
    stem = mod.ensure_weekly_cycle_file(mock_dbx, VAULT, datetime(2026, 10, 29))
    assert stem == "Cycle 151 (Oct. 28 - Nov. 03, 2026)"
    args, kwargs = mock_dbx.files_upload.call_args
    assert args[1] == CYCLE_151_PATH
    assert kwargs["mode"].is_add()
    uploaded = args[0].decode("utf-8")
    assert "### Wednesday -" in uploaded
    assert "### Tuesday -" in uploaded
