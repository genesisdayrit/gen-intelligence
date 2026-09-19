"""Conflict guards: remaining Obsidian writers use update(rev)/add, never overwrite."""

import os
import sys
from unittest.mock import MagicMock, patch

import dropbox

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/test/vault")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")
os.environ.setdefault("SYSTEM_TIMEZONE", "US/Eastern")

from services.obsidian.add_telegram_log import append_telegram_log
from services.obsidian.add_todoist_completed import (
    TODOIST_COMPLETED_HEADER,
    append_todoist_completed,
)

REV = "aaaaaaaaaaaaaaaa"
DA_PATH = "/test/vault/_Daily/_Daily-Action/DA 2026-09-19.md"
JOURNAL_PATH = "/test/vault/_Daily/_Journal/Sep 19, 2026.md"

SAMPLE_DA = f"""---
date: 2026-09-19
---

Daily Review:
- ok
---

{TODOIST_COMPLETED_HEADER}
"""

SAMPLE_JOURNAL = """---
Date: 2026-09-19
---

# Journal
"""


def _rev_conflict():
    reason = dropbox.files.WriteError.conflict(dropbox.files.WriteConflictError.file)
    failed = dropbox.files.UploadWriteFailed(reason=reason, upload_session_id="sess")
    error = dropbox.files.UploadError.path(failed)
    return dropbox.exceptions.ApiError("req", error, "", "")


def _download(content: str, path: str):
    metadata = MagicMock()
    metadata.rev = REV
    metadata.path_display = path
    response = MagicMock()
    response.content = content.encode("utf-8")
    return metadata, response


def test_todoist_append_uses_update_mode_not_overwrite():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(SAMPLE_DA, DA_PATH)

    with (
        patch("services.obsidian.add_todoist_completed._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_todoist_completed._find_daily_folder", return_value="/test/vault/_Daily"),
        patch(
            "services.obsidian.add_todoist_completed._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch(
            "services.obsidian.add_todoist_completed._get_today_daily_action_path",
            return_value=DA_PATH,
        ),
    ):
        append_todoist_completed("Finish the conflict guards")

    mock_dbx.files_upload.assert_called_once()
    kwargs = mock_dbx.files_upload.call_args.kwargs
    assert kwargs["mode"].is_update()
    assert kwargs["mode"].get_update() == REV
    assert not kwargs["mode"].is_overwrite()
    assert kwargs["autorename"] is False
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    assert "Finish the conflict guards" in uploaded


def test_todoist_rev_conflict_retries_then_enqueues():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(SAMPLE_DA, DA_PATH)
    mock_dbx.files_upload.side_effect = _rev_conflict()

    with (
        patch("services.obsidian.add_todoist_completed._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_todoist_completed._find_daily_folder", return_value="/test/vault/_Daily"),
        patch(
            "services.obsidian.add_todoist_completed._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch(
            "services.obsidian.add_todoist_completed._get_today_daily_action_path",
            return_value=DA_PATH,
        ),
        patch("services.obsidian.utils.dropbox_rev_safe.record_deferred_write") as mock_enqueue,
    ):
        append_todoist_completed("Retry me")

    assert mock_dbx.files_upload.call_count == 2
    for call in mock_dbx.files_upload.call_args_list:
        assert call.kwargs["mode"].is_update()
        assert not call.kwargs["mode"].is_overwrite()
    mock_enqueue.assert_called()
    assert mock_enqueue.call_args.kwargs["source"] == "todoist"


def test_telegram_append_uses_update_mode_not_overwrite():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(SAMPLE_JOURNAL, JOURNAL_PATH)

    with (
        patch("services.obsidian.add_telegram_log._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_telegram_log._find_daily_folder", return_value="/test/vault/_Daily"),
        patch("services.obsidian.add_telegram_log._get_today_journal_path", return_value=JOURNAL_PATH),
    ):
        append_telegram_log("[01:00 PM] hello from telegram")

    mock_dbx.files_upload.assert_called_once()
    kwargs = mock_dbx.files_upload.call_args.kwargs
    assert kwargs["mode"].is_update()
    assert not kwargs["mode"].is_overwrite()
    assert kwargs["autorename"] is False
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    assert "### Telegram Logs:" in uploaded
    assert "[01:00 PM] hello from telegram" in uploaded
