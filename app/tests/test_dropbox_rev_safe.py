"""Rev-safe Dropbox upload helper: update-on-rev, never overwrite on conflict."""

import os
import sys
from unittest.mock import MagicMock

import dropbox
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.obsidian.utils.dropbox_rev_safe import (  # noqa: E402
    is_dropbox_write_conflict,
    upload_if_rev_matches,
    write_mode_update,
)


def _rev_conflict_api_error() -> dropbox.exceptions.ApiError:
    reason = MagicMock()
    reason.is_conflict.return_value = True
    write_failed = MagicMock()
    write_failed.reason = reason
    error = MagicMock()
    error.is_path.return_value = True
    error.get_path.return_value = write_failed
    return dropbox.exceptions.ApiError("req", error, "", "")


def _real_rev_conflict_api_error() -> dropbox.exceptions.ApiError:
    reason = dropbox.files.WriteError.conflict(dropbox.files.WriteConflictError.file)
    failed = dropbox.files.UploadWriteFailed(reason=reason, upload_session_id="sess")
    error = dropbox.files.UploadError.path(failed)
    return dropbox.exceptions.ApiError("req", error, "", "")


# Dropbox FileMetadata.rev is 9+ hex chars (stone validator: [0-9a-f]+).
REV_MATCH = "0123456789abcdef"
REV_STALE = "fedcba9876543210"
REV_OTHER = "aaaaaaaaaaaaaaaa"


def test_write_mode_update_is_not_overwrite():
    mode = write_mode_update(REV_MATCH)
    assert mode.is_update()
    assert mode.get_update() == REV_MATCH
    assert not mode.is_overwrite()


def test_write_mode_update_requires_rev():
    with pytest.raises(ValueError, match="rev"):
        write_mode_update("")


def test_upload_if_rev_matches_uses_update_mode():
    mock_dbx = MagicMock()
    mock_dbx.files_upload.return_value = MagicMock(name="metadata")

    result = upload_if_rev_matches(
        mock_dbx,
        "/vault/note.md",
        b"updated",
        REV_MATCH,
    )

    assert result.status == "updated"
    assert result.rev == REV_MATCH
    mock_dbx.files_upload.assert_called_once()
    kwargs = mock_dbx.files_upload.call_args.kwargs
    assert kwargs["autorename"] is False
    assert kwargs["mode"].is_update()
    assert kwargs["mode"].get_update() == REV_MATCH
    assert not kwargs["mode"].is_overwrite()


def test_upload_if_rev_matches_conflict_does_not_overwrite():
    mock_dbx = MagicMock()
    mock_dbx.files_upload.side_effect = _real_rev_conflict_api_error()

    result = upload_if_rev_matches(
        mock_dbx,
        "/vault/note.md",
        b"updated",
        REV_STALE,
    )

    assert result.status == "deferred"
    assert result.metadata is None
    mock_dbx.files_upload.assert_called_once()
    mode = mock_dbx.files_upload.call_args.kwargs["mode"]
    assert mode.is_update()
    assert mode.get_update() == REV_STALE
    assert not mode.is_overwrite()


def test_is_dropbox_write_conflict_detects_path_conflict():
    assert is_dropbox_write_conflict(_rev_conflict_api_error()) is True
    assert is_dropbox_write_conflict(_real_rev_conflict_api_error()) is True


def test_is_dropbox_write_conflict_ignores_other_errors():
    error = MagicMock()
    error.is_path.return_value = False
    exc = dropbox.exceptions.ApiError("req", error, "", "")
    assert is_dropbox_write_conflict(exc) is False
    assert is_dropbox_write_conflict(RuntimeError("nope")) is False


def test_non_conflict_api_error_is_reraised():
    mock_dbx = MagicMock()
    error = MagicMock()
    error.is_path.return_value = False
    mock_dbx.files_upload.side_effect = dropbox.exceptions.ApiError(
        "req", error, "", ""
    )

    with pytest.raises(dropbox.exceptions.ApiError):
        upload_if_rev_matches(mock_dbx, "/vault/note.md", b"x", REV_OTHER)
