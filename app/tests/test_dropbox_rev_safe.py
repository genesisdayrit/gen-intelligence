"""Rev-safe Dropbox upload helper: update-on-rev, never overwrite on conflict."""

import os
import sys
from unittest.mock import MagicMock, patch

import dropbox
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.obsidian.utils.dropbox_rev_safe import (  # noqa: E402
    create_or_defer,
    download_text_with_rev,
    is_conflicted_copy_path,
    is_dropbox_write_conflict,
    record_deferred_write,
    update_with_retry,
    upload_if_rev_matches,
    upload_new_file,
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


def test_is_conflicted_copy_path_is_case_insensitive():
    assert is_conflicted_copy_path(
        "Sep 19, 2026 (MacBook Pro's conflicted copy 2026-09-19).md"
    )
    assert is_conflicted_copy_path(
        "/vault/good enough job vs mission driven "
        "(macbook pro's conflicted copy 2026-09-19).md"
    )
    assert is_conflicted_copy_path("Note (CONFLICTED COPY).md")
    assert not is_conflicted_copy_path("Sep 19, 2026.md")
    assert not is_conflicted_copy_path("")
    assert not is_conflicted_copy_path(None)


def test_upload_new_file_uses_add_mode():
    mock_dbx = MagicMock()
    mock_dbx.files_upload.return_value = MagicMock(rev=REV_MATCH)

    result = upload_new_file(mock_dbx, "/vault/new.md", b"created")

    assert result.status == "updated"
    kwargs = mock_dbx.files_upload.call_args.kwargs
    assert kwargs["autorename"] is False
    assert kwargs["mode"].is_add()
    assert not kwargs["mode"].is_overwrite()


def test_upload_new_file_conflict_does_not_overwrite():
    mock_dbx = MagicMock()
    mock_dbx.files_upload.side_effect = _real_rev_conflict_api_error()

    result = upload_new_file(mock_dbx, "/vault/new.md", b"created")

    assert result.status == "deferred"
    mode = mock_dbx.files_upload.call_args.kwargs["mode"]
    assert mode.is_add()
    assert not mode.is_overwrite()


def test_record_deferred_write_enqueues_on_queue():
    with patch(
        "services.obsidian.reconcile.queue.enqueue_deferred",
        return_value={"source": "readwise"},
    ) as mock_enqueue:
        record_deferred_write(
            source="readwise",
            kind="journal_highlight",
            payload_ref="111",
            target="/vault/note.md",
            payload={"id": 111},
        )
    mock_enqueue.assert_called_once_with(
        source="readwise",
        kind="journal_highlight",
        payload_ref="111",
        target="/vault/note.md",
        payload={"id": 111},
    )


def test_record_deferred_write_swallows_redis_errors():
    with patch(
        "services.obsidian.reconcile.queue.enqueue_deferred",
        side_effect=RuntimeError("redis down"),
    ):
        record_deferred_write(
            source="readwise",
            kind="journal_highlight",
            payload_ref="111",
            target="/vault/note.md",
        )


def _download(content: str, *, rev: str = REV_MATCH, path: str = "/vault/note.md"):
    metadata = MagicMock()
    metadata.rev = rev
    metadata.path_display = path
    response = MagicMock()
    response.content = content.encode("utf-8")
    return metadata, response


def test_download_text_with_rev_requires_rev():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download("hello", rev=REV_MATCH)
    note = download_text_with_rev(mock_dbx, "/vault/note.md")
    assert note.content == "hello"
    assert note.rev == REV_MATCH


def test_update_with_retry_uses_update_mode_then_succeeds():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download("old")
    mock_dbx.files_upload.return_value = MagicMock()

    status, meta, updated = update_with_retry(
        mock_dbx, "/vault/note.md", lambda content: (content + "\nnew", "wrote")
    )

    assert status == "updated"
    assert meta == "wrote"
    assert updated.endswith("new")
    kwargs = mock_dbx.files_upload.call_args.kwargs
    assert kwargs["mode"].is_update()
    assert kwargs["mode"].get_update() == REV_MATCH
    assert not kwargs["mode"].is_overwrite()
    assert kwargs["autorename"] is False


def test_update_with_retry_rematches_once_then_enqueues():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download("old", rev=REV_STALE)
    mock_dbx.files_upload.side_effect = _real_rev_conflict_api_error()

    with patch(
        "services.obsidian.utils.dropbox_rev_safe.record_deferred_write"
    ) as mock_enqueue:
        status, _meta, _content = update_with_retry(
            mock_dbx,
            "/vault/note.md",
            lambda content: (content + "\nnew", None),
            defer={
                "source": "granola",
                "kind": "journal_note",
                "payload_ref": "not_1",
                "target": "/vault/note.md",
            },
        )

    assert status == "deferred"
    assert mock_dbx.files_upload.call_count == 2
    for call in mock_dbx.files_upload.call_args_list:
        assert call.kwargs["mode"].is_update()
        assert not call.kwargs["mode"].is_overwrite()
    mock_enqueue.assert_called_once()
    assert mock_enqueue.call_args.kwargs["source"] == "granola"


def test_create_or_defer_uses_add_and_enqueues_on_conflict():
    mock_dbx = MagicMock()
    mock_dbx.files_upload.side_effect = _real_rev_conflict_api_error()

    with patch(
        "services.obsidian.utils.dropbox_rev_safe.record_deferred_write"
    ) as mock_enqueue:
        created = create_or_defer(
            mock_dbx,
            "/vault/new.md",
            "created",
            defer={"source": "share_link", "kind": "kh_create", "payload_ref": "u"},
        )

    assert created is False
    mode = mock_dbx.files_upload.call_args.kwargs["mode"]
    assert mode.is_add()
    assert not mode.is_overwrite()
    mock_enqueue.assert_called_once()
