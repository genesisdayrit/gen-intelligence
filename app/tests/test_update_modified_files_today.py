"""Folder-journal relations: rev-safe upload, skip no-op, defer on conflict."""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import dropbox
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SYSTEM_TIMEZONE", "America/Los_Angeles")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")

from scripts.obsidian.workflows.file_updates import (  # noqa: E402
    update_modified_files_today as mod,
)

MODULE = "scripts.obsidian.workflows.file_updates.update_modified_files_today"

# 20:00 UTC on 2026-09-19 is 13:00 America/Los_Angeles → Sep 19, 2026
SEP_19_UTC = datetime(2026, 9, 19, 20, 0, tzinfo=pytz.utc)
NOTE_PATH = "/vault/Note.md"
NOTE_WITHOUT_JOURNAL = "---\ntitle: Note\n---\n\nbody\n"
NOTE_WITH_JOURNAL = '---\nJournal:\n  - "[[Sep 19, 2026]]"\n---\n\nbody\n'
# Dropbox FileMetadata.rev is 9+ hex chars (stone validator: [0-9a-f]+).
REV_AAA = "aaaaaaaaaaaaaaaa"
REV_STALE = "bbbbbbbbbbbbbbbb"
REV_DONE = "cccccccccccccccc"
REV_NOOP = "dddddddddddddddd"
REV_OLD = "1111111111111111"
REV_NEW = "2222222222222222"


def _rev_conflict_api_error() -> dropbox.exceptions.ApiError:
    reason = MagicMock()
    reason.is_conflict.return_value = True
    write_failed = MagicMock()
    write_failed.reason = reason
    error = MagicMock()
    error.is_path.return_value = True
    error.get_path.return_value = write_failed
    return dropbox.exceptions.ApiError("req", error, "", "")


def _download(content: str, *, rev: str, modified: datetime = SEP_19_UTC):
    metadata = MagicMock()
    metadata.rev = rev
    metadata.path_display = NOTE_PATH
    metadata.name = "Note.md"
    metadata.client_modified = modified
    response = MagicMock()
    response.content = content.encode("utf-8")
    return metadata, response


def _assert_update_mode(call, expected_rev: str) -> None:
    mode = call.kwargs["mode"]
    assert mode.is_update(), mode
    assert mode.get_update() == expected_rev
    assert not mode.is_overwrite()
    assert call.kwargs.get("autorename") is False


def test_apply_journal_date_noop_when_already_present():
    assert mod._apply_journal_date(NOTE_WITH_JOURNAL, "Sep 19, 2026") is None


def test_apply_journal_date_adds_missing_journal():
    updated = mod._apply_journal_date(NOTE_WITHOUT_JOURNAL, "Sep 19, 2026")
    assert updated is not None
    assert '[[Sep 19, 2026]]' in updated
    assert updated != NOTE_WITHOUT_JOURNAL


def test_matching_rev_uploads_with_update_mode():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(
        NOTE_WITHOUT_JOURNAL, rev=REV_AAA
    )
    mock_dbx.files_upload.return_value = MagicMock()

    status = mod._update_journal_property(mock_dbx, NOTE_PATH, max_attempts=1)

    assert status == mod.STATUS_UPDATED
    mock_dbx.files_upload.assert_called_once()
    _assert_update_mode(mock_dbx.files_upload.call_args, REV_AAA)
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    assert "[[Sep 19, 2026]]" in uploaded


def test_rev_conflict_does_not_overwrite_and_returns_deferred():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(
        NOTE_WITHOUT_JOURNAL, rev=REV_STALE
    )
    mock_dbx.files_upload.side_effect = _rev_conflict_api_error()

    status = mod._update_journal_property(mock_dbx, NOTE_PATH, max_attempts=1)

    assert status == mod.STATUS_DEFERRED
    mock_dbx.files_upload.assert_called_once()
    _assert_update_mode(mock_dbx.files_upload.call_args, REV_STALE)
    for call in mock_dbx.files_upload.call_args_list:
        assert not call.kwargs["mode"].is_overwrite()


def test_noop_when_journal_already_correct_skips_upload():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(
        NOTE_WITH_JOURNAL, rev=REV_NOOP
    )

    status = mod._update_journal_property(mock_dbx, NOTE_PATH, max_attempts=1)

    assert status == mod.STATUS_SKIPPED
    mock_dbx.files_upload.assert_not_called()


def test_job_survives_rev_conflict_and_never_overwrites():
    """A conflicted file is deferred; the scheduled job still returns success."""
    mock_dbx = MagicMock()

    def download(path):
        if path.endswith("done.md"):
            return _download(NOTE_WITH_JOURNAL, rev=REV_DONE)
        return _download(NOTE_WITHOUT_JOURNAL, rev=REV_STALE)

    mock_dbx.files_download.side_effect = download
    mock_dbx.files_upload.side_effect = _rev_conflict_api_error()

    with (
        patch(f"{MODULE}._get_dropbox_client", return_value=mock_dbx),
        patch(f"{MODULE}._load_paths", return_value=["/vault"]),
        patch(f"{MODULE}._get_last_run_time", return_value=SEP_19_UTC),
        patch(f"{MODULE}._set_last_run_time"),
        patch(
            f"{MODULE}._get_modified_files_since_cutoff",
            return_value=["/vault/done.md", "/vault/changed.md"],
        ),
    ):
        assert mod.update_modified_files_today() is True

    # No-op file never uploaded; conflicted file retries once, always update mode.
    assert mock_dbx.files_upload.call_count == 2
    for call in mock_dbx.files_upload.call_args_list:
        _assert_update_mode(call, REV_STALE)
        assert not call.kwargs["mode"].is_overwrite()


def test_rev_conflict_retry_applies_yaml_to_latest_rev():
    mock_dbx = MagicMock()
    mock_dbx.files_download.side_effect = [
        _download(NOTE_WITHOUT_JOURNAL, rev=REV_OLD),
        _download(NOTE_WITHOUT_JOURNAL, rev=REV_NEW),
    ]
    mock_dbx.files_upload.side_effect = [
        _rev_conflict_api_error(),
        MagicMock(),
    ]

    status = mod._update_journal_property(mock_dbx, NOTE_PATH, max_attempts=2)

    assert status == mod.STATUS_UPDATED
    assert mock_dbx.files_download.call_count == 2
    assert mock_dbx.files_upload.call_count == 2
    _assert_update_mode(mock_dbx.files_upload.call_args_list[0], REV_OLD)
    _assert_update_mode(mock_dbx.files_upload.call_args_list[1], REV_NEW)
    for call in mock_dbx.files_upload.call_args_list:
        assert not call.kwargs["mode"].is_overwrite()


def test_conflicted_copy_path_is_ignored_without_download_or_upload():
    """Already-forked Dropbox copies are never mutated by the 15m job."""
    conflicted = (
        "/vault/good enough job vs mission driven "
        "(macbook pro's conflicted copy 2026-09-19).md"
    )
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(
        NOTE_WITHOUT_JOURNAL, rev=REV_AAA
    )
    mock_dbx.files_upload.return_value = MagicMock()

    with (
        patch(f"{MODULE}._get_dropbox_client", return_value=mock_dbx),
        patch(f"{MODULE}._load_paths", return_value=["/vault"]),
        patch(f"{MODULE}._get_last_run_time", return_value=SEP_19_UTC),
        patch(f"{MODULE}._set_last_run_time"),
        patch(
            f"{MODULE}._get_modified_files_since_cutoff",
            return_value=[conflicted, NOTE_PATH],
        ),
    ):
        assert mod.update_modified_files_today() is True

    downloaded_paths = [call.args[0] for call in mock_dbx.files_download.call_args_list]
    assert conflicted not in downloaded_paths
    assert NOTE_PATH in downloaded_paths
    mock_dbx.files_upload.assert_called_once()
    _assert_update_mode(mock_dbx.files_upload.call_args, REV_AAA)


def test_update_journal_property_skips_conflicted_copy_filename():
    mock_dbx = MagicMock()
    status = mod._update_journal_property(
        mock_dbx,
        "Sep 19, 2026 (MacBook Pro's conflicted copy 2026-09-19).md",
        max_attempts=1,
    )
    assert status == mod.STATUS_IGNORED
    mock_dbx.files_download.assert_not_called()
    mock_dbx.files_upload.assert_not_called()


def _file_metadata(name: str, path_lower: str, path_display: str):
    naive = SEP_19_UTC.replace(tzinfo=None)
    return dropbox.files.FileMetadata(
        name=name,
        id="id:test",
        client_modified=naive,
        server_modified=naive,
        rev=REV_AAA,
        size=1,
        path_lower=path_lower,
        path_display=path_display,
    )


def test_list_folder_skips_conflicted_copy_entries():
    conflicted = _file_metadata(
        "Note (MacBook Pro's conflicted copy 2026-09-19).md",
        "/vault/note (macbook pro's conflicted copy 2026-09-19).md",
        "/vault/Note (MacBook Pro's conflicted copy 2026-09-19).md",
    )
    normal = _file_metadata("Note.md", "/vault/note.md", "/vault/Note.md")

    result = MagicMock()
    result.entries = [conflicted, normal]
    result.has_more = False
    mock_dbx = MagicMock()
    mock_dbx.files_list_folder.return_value = result

    paths = mod._get_modified_files_since_cutoff(
        mock_dbx, ["/vault"], SEP_19_UTC.replace(hour=0)
    )

    assert paths == ["/vault/note.md"]
