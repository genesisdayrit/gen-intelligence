"""Unit tests for daily journal YAML property updates.

Covers Previous Day / Next Day wikilinks relative to tomorrow (scheduled
default) and today (``--today`` / ``use_today=True``), with mocked dates.
"""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SYSTEM_TIMEZONE", "America/Los_Angeles")
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/test/vault")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")

from scripts.obsidian.workflows.file_updates import update_daily_journal_properties as mod

LA = pytz.timezone("America/Los_Angeles")
# Evening-before scheduled run (18:10 PT) on a mid-month Wednesday.
FIXED_NOW = LA.localize(datetime(2026, 9, 16, 18, 10))
MODULE = "scripts.obsidian.workflows.file_updates.update_daily_journal_properties"


def _fake_target_day(use_today: bool = False) -> datetime:
    return FIXED_NOW if use_today else FIXED_NOW + timedelta(days=1)


def _metadata_with_null_adjacent_days() -> dict:
    """Template-shaped frontmatter: adjacent-day keys present but null."""
    return {
        "Day of Week": None,
        "Date": None,
        "Daily Action": None,
        "On this Day": None,
        "Previous Day": None,
        "Next Day": None,
    }


def test_format_journal_stem_drops_leading_zero():
    day = LA.localize(datetime(2026, 9, 9, 12, 0))
    assert mod._format_journal_stem(day) == "Sep 9, 2026"


def test_previous_next_filenames_for_tomorrow_target():
    """Default scheduled run: target is tomorrow (Sep 17)."""
    with patch(f"{MODULE}._get_target_day", side_effect=_fake_target_day):
        assert mod._get_previous_day_filename(use_today=False) == "Sep 16, 2026"
        assert mod._get_next_day_filename(use_today=False) == "Sep 18, 2026"
        assert mod._get_target_filename(use_today=False) == "Sep 17, 2026.md"


def test_previous_next_filenames_for_today_target():
    """``--today`` / morning recovery: target is today (Sep 16)."""
    with patch(f"{MODULE}._get_target_day", side_effect=_fake_target_day):
        assert mod._get_previous_day_filename(use_today=True) == "Sep 15, 2026"
        assert mod._get_next_day_filename(use_today=True) == "Sep 17, 2026"
        assert mod._get_target_filename(use_today=True) == "Sep 16, 2026.md"


def test_update_yaml_metadata_sets_adjacent_days_for_tomorrow():
    with patch(f"{MODULE}._get_target_day", side_effect=_fake_target_day):
        updated = mod._update_yaml_metadata(
            _metadata_with_null_adjacent_days(),
            {},
            use_today=False,
        )

    assert updated["Previous Day"] == ["[[Sep 16, 2026]]"]
    assert updated["Next Day"] == ["[[Sep 18, 2026]]"]
    assert updated["Date"] == "2026-09-17"
    assert updated["Day of Week"] == "Thursday"
    assert updated["Daily Action"] == ["[[DA 2026-09-17]]"]
    assert updated["On this Day"] == ["[[Sep 17, 2025]]"]


def test_update_yaml_metadata_sets_adjacent_days_for_today():
    with patch(f"{MODULE}._get_target_day", side_effect=_fake_target_day):
        updated = mod._update_yaml_metadata(
            _metadata_with_null_adjacent_days(),
            {},
            use_today=True,
        )

    assert updated["Previous Day"] == ["[[Sep 15, 2026]]"]
    assert updated["Next Day"] == ["[[Sep 17, 2026]]"]
    assert updated["Date"] == "2026-09-16"
    assert updated["Day of Week"] == "Wednesday"
    assert updated["Daily Action"] == ["[[DA 2026-09-16]]"]
    assert updated["On this Day"] == ["[[Sep 16, 2025]]"]


def test_update_yaml_metadata_overwrites_template_nulls_in_yaml_dump():
    """Dumped frontmatter must be a YAML list of wikilinks, not null."""
    with patch(f"{MODULE}._get_target_day", side_effect=_fake_target_day):
        updated = mod._update_yaml_metadata(
            _metadata_with_null_adjacent_days(),
            {},
            use_today=False,
        )

    dumped = yaml.safe_dump(updated, default_flow_style=False, sort_keys=False)
    reloaded = yaml.safe_load(dumped)

    assert reloaded["Previous Day"] == ["[[Sep 16, 2026]]"]
    assert reloaded["Next Day"] == ["[[Sep 18, 2026]]"]
    assert "Previous Day: null" not in dumped
    assert "Next Day: null" not in dumped


def test_adjacent_days_cross_month_and_year_boundaries():
    new_years_eve = LA.localize(datetime(2026, 12, 31, 18, 10))

    def fake_nye_target(use_today: bool = False) -> datetime:
        return new_years_eve if use_today else new_years_eve + timedelta(days=1)

    with patch(f"{MODULE}._get_target_day", side_effect=fake_nye_target):
        tomorrow = mod._update_yaml_metadata({}, {}, use_today=False)
        today = mod._update_yaml_metadata({}, {}, use_today=True)

    assert tomorrow["Previous Day"] == ["[[Dec 31, 2026]]"]
    assert tomorrow["Next Day"] == ["[[Jan 2, 2027]]"]
    assert today["Previous Day"] == ["[[Dec 30, 2026]]"]
    assert today["Next Day"] == ["[[Jan 1, 2027]]"]


def test_update_daily_journal_properties_uses_update_mode_not_overwrite():
    from datetime import datetime as dt

    import dropbox
    from unittest.mock import MagicMock

    entry = dropbox.files.FileMetadata(
        name="Sep 17, 2026.md",
        id="id:journal",
        client_modified=dt(2026, 9, 16, 18, 10),
        server_modified=dt(2026, 9, 16, 18, 10),
        rev="aaaaaaaaaaaaaaaa",
        size=32,
        path_lower="/test/vault/_daily/_journal/sep 17, 2026.md",
        path_display="/test/vault/_Daily/_Journal/Sep 17, 2026.md",
    )
    metadata = MagicMock()
    metadata.rev = "aaaaaaaaaaaaaaaa"
    metadata.path_display = entry.path_display
    response = MagicMock()
    response.content = (
        "---\nDay of Week: null\nDate: null\nPrevious Day: null\nNext Day: null\n---\n\n# Journal\n"
    ).encode("utf-8")
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = (metadata, response)

    with (
        patch(f"{MODULE}._get_dropbox_client", return_value=mock_dbx),
        patch(f"{MODULE}._find_folder_in_path", side_effect=["/test/vault/_Daily", "/test/vault/_Daily/_Journal"]),
        patch(f"{MODULE}._list_all_entries", return_value=[entry]),
        patch(f"{MODULE}._get_dynamic_mappings", return_value={}),
        patch(f"{MODULE}._get_target_day", side_effect=_fake_target_day),
        patch(f"{MODULE}._get_target_filename", return_value="Sep 17, 2026.md"),
    ):
        assert mod.update_daily_journal_properties(use_today=False) is True

    mock_dbx.files_upload.assert_called_once()
    kwargs = mock_dbx.files_upload.call_args.kwargs
    assert kwargs["mode"].is_update()
    assert kwargs["mode"].get_update() == "aaaaaaaaaaaaaaaa"
    assert not kwargs["mode"].is_overwrite()
    assert kwargs["autorename"] is False


def test_update_daily_journal_properties_rev_conflict_enqueues():
    from datetime import datetime as dt

    import dropbox
    from unittest.mock import MagicMock

    entry = dropbox.files.FileMetadata(
        name="Sep 17, 2026.md",
        id="id:journal",
        client_modified=dt(2026, 9, 16, 18, 10),
        server_modified=dt(2026, 9, 16, 18, 10),
        rev="aaaaaaaaaaaaaaaa",
        size=32,
        path_lower="/test/vault/_daily/_journal/sep 17, 2026.md",
        path_display="/test/vault/_Daily/_Journal/Sep 17, 2026.md",
    )
    metadata = MagicMock()
    metadata.rev = "aaaaaaaaaaaaaaaa"
    metadata.path_display = entry.path_display
    response = MagicMock()
    response.content = "---\nDate: null\n---\n\n# Journal\n".encode("utf-8")
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = (metadata, response)
    reason = dropbox.files.WriteError.conflict(dropbox.files.WriteConflictError.file)
    failed = dropbox.files.UploadWriteFailed(reason=reason, upload_session_id="sess")
    mock_dbx.files_upload.side_effect = dropbox.exceptions.ApiError(
        "req", dropbox.files.UploadError.path(failed), "", ""
    )

    with (
        patch(f"{MODULE}._get_dropbox_client", return_value=mock_dbx),
        patch(f"{MODULE}._find_folder_in_path", side_effect=["/test/vault/_Daily", "/test/vault/_Daily/_Journal"]),
        patch(f"{MODULE}._list_all_entries", return_value=[entry]),
        patch(f"{MODULE}._get_dynamic_mappings", return_value={}),
        patch(f"{MODULE}._get_target_day", side_effect=_fake_target_day),
        patch(f"{MODULE}._get_target_filename", return_value="Sep 17, 2026.md"),
        patch("services.obsidian.utils.dropbox_rev_safe.record_deferred_write") as mock_enqueue,
    ):
        assert mod.update_daily_journal_properties(use_today=False) is True

    assert mock_dbx.files_upload.call_count == 2
    mock_enqueue.assert_called()
    assert mock_enqueue.call_args.kwargs["source"] == "journal_properties"
