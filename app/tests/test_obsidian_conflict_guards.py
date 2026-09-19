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

from scripts.obsidian.workflows.file_updates.add_daily_review_section import (
    add_daily_review_section,
)
from services.obsidian.add_daily_action_issues_touched import (
    upsert_daily_action_issue_touched,
)
from services.obsidian.add_daily_action_updates import upsert_daily_action_update
from services.obsidian.add_manus_task import _upsert_daily_action_manus
from services.obsidian.add_telegram_log import append_telegram_log
from services.obsidian.add_todoist_completed import (
    TODOIST_COMPLETED_HEADER,
    append_todoist_completed,
)
from services.obsidian.remove_todoist_completed import remove_todoist_completed
from services.obsidian.update_telegram_log import update_telegram_log

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

SAMPLE_DA_WITH_TODOIST_TASK = f"""---
date: 2026-09-19
---

Daily Review:
- ok
---

{TODOIST_COMPLETED_HEADER}
[01:00 PM] Finish the conflict guards
"""

SAMPLE_DA_NO_REVIEW = """---
date: 2026-09-19
---

Existing body
"""

SAMPLE_JOURNAL_WITH_TELEGRAM = """---
Date: 2026-09-19
---

# Journal

### Telegram Logs:
[01:00 PM] hello from telegram
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


def test_telegram_rev_conflict_retries_then_enqueues():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(SAMPLE_JOURNAL, JOURNAL_PATH)
    mock_dbx.files_upload.side_effect = _rev_conflict()

    with (
        patch("services.obsidian.add_telegram_log._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_telegram_log._find_daily_folder", return_value="/test/vault/_Daily"),
        patch("services.obsidian.add_telegram_log._get_today_journal_path", return_value=JOURNAL_PATH),
        patch("services.obsidian.utils.dropbox_rev_safe.record_deferred_write") as mock_enqueue,
    ):
        append_telegram_log("[01:00 PM] hello from telegram")

    assert mock_dbx.files_upload.call_count == 2
    for call in mock_dbx.files_upload.call_args_list:
        assert call.kwargs["mode"].is_update()
        assert not call.kwargs["mode"].is_overwrite()
    mock_enqueue.assert_called()
    assert mock_enqueue.call_args.kwargs["source"] == "telegram"


def test_manus_daily_action_uses_update_mode_and_enqueues_on_conflict():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(SAMPLE_DA, DA_PATH)
    mock_dbx.files_upload.side_effect = _rev_conflict()

    with (
        patch("services.obsidian.add_manus_task._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_manus_task._find_daily_folder", return_value="/test/vault/_Daily"),
        patch(
            "services.obsidian.add_manus_task._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch("services.obsidian.add_manus_task._get_today_daily_action_path", return_value=DA_PATH),
        patch("services.obsidian.utils.dropbox_rev_safe.record_deferred_write") as mock_enqueue,
    ):
        result = _upsert_daily_action_manus("abc123", "Test Task", "https://manus.im/app/abc123")

    assert result["success"] is True
    assert result["action"] == "deferred"
    assert mock_dbx.files_upload.call_count == 2
    for call in mock_dbx.files_upload.call_args_list:
        assert call.kwargs["mode"].is_update()
        assert not call.kwargs["mode"].is_overwrite()
    mock_enqueue.assert_called()
    assert mock_enqueue.call_args.kwargs["source"] == "manus"


def test_daily_action_update_uses_update_mode_and_enqueues_on_conflict():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(SAMPLE_DA, DA_PATH)
    mock_dbx.files_upload.side_effect = _rev_conflict()

    with (
        patch("services.obsidian.add_daily_action_updates._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_daily_action_updates._find_daily_folder", return_value="/test/vault/_Daily"),
        patch(
            "services.obsidian.add_daily_action_updates._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch(
            "services.obsidian.add_daily_action_updates._get_today_daily_action_path",
            return_value=DA_PATH,
        ),
        patch("services.obsidian.utils.dropbox_rev_safe.record_deferred_write") as mock_enqueue,
    ):
        result = upsert_daily_action_update(
            "initiative",
            "https://linear.app/x/initiative",
            "Some Initiative",
            "shipped the conflict guards",
        )

    assert result["success"] is True
    assert result["action"] == "deferred"
    assert mock_dbx.files_upload.call_count == 2
    for call in mock_dbx.files_upload.call_args_list:
        assert call.kwargs["mode"].is_update()
        assert not call.kwargs["mode"].is_overwrite()
    mock_enqueue.assert_called()
    assert mock_enqueue.call_args.kwargs["source"] == "daily_action"


def test_issues_touched_uses_update_mode_and_enqueues_on_conflict():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(SAMPLE_DA, DA_PATH)
    mock_dbx.files_upload.side_effect = _rev_conflict()

    with (
        patch("services.obsidian.add_daily_action_issues_touched._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_daily_action_issues_touched._find_daily_folder", return_value="/test/vault/_Daily"),
        patch(
            "services.obsidian.add_daily_action_issues_touched._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch(
            "services.obsidian.add_daily_action_issues_touched._get_today_daily_action_path",
            return_value=DA_PATH,
        ),
        patch("services.obsidian.utils.dropbox_rev_safe.record_deferred_write") as mock_enqueue,
    ):
        result = upsert_daily_action_issue_touched(
            issue_identifier="GD-328",
            project_name="Centralizing OS",
            issue_title="Add Issues Touched",
            status_name="In Progress",
            issue_url="https://linear.app/chapters/issue/gd-328/add-issues-touched",
            status_changed=False,
        )

    assert result["success"] is True
    assert result["action"] == "deferred"
    assert mock_dbx.files_upload.call_count == 2
    for call in mock_dbx.files_upload.call_args_list:
        assert call.kwargs["mode"].is_update()
        assert not call.kwargs["mode"].is_overwrite()
    mock_enqueue.assert_called()
    assert mock_enqueue.call_args.kwargs["source"] == "daily_action"
    assert mock_enqueue.call_args.kwargs["kind"] == "issues_touched"


def test_remove_todoist_rev_conflict_retries_then_enqueues():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(SAMPLE_DA_WITH_TODOIST_TASK, DA_PATH)
    mock_dbx.files_upload.side_effect = _rev_conflict()

    with (
        patch("services.obsidian.remove_todoist_completed._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.remove_todoist_completed._find_daily_folder", return_value="/test/vault/_Daily"),
        patch(
            "services.obsidian.remove_todoist_completed._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch(
            "services.obsidian.remove_todoist_completed._get_today_daily_action_path",
            return_value=DA_PATH,
        ),
        patch("services.obsidian.utils.dropbox_rev_safe.record_deferred_write") as mock_enqueue,
    ):
        assert remove_todoist_completed("Finish the conflict guards") is True

    assert mock_dbx.files_upload.call_count == 2
    for call in mock_dbx.files_upload.call_args_list:
        assert call.kwargs["mode"].is_update()
        assert not call.kwargs["mode"].is_overwrite()
    mock_enqueue.assert_called()
    assert mock_enqueue.call_args.kwargs["source"] == "todoist"
    assert mock_enqueue.call_args.kwargs["kind"] == "uncompleted"


def test_telegram_update_rev_conflict_retries_then_enqueues():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(SAMPLE_JOURNAL_WITH_TELEGRAM, JOURNAL_PATH)
    mock_dbx.files_upload.side_effect = _rev_conflict()

    with (
        patch("services.obsidian.update_telegram_log._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.update_telegram_log._find_daily_folder", return_value="/test/vault/_Daily"),
        patch("services.obsidian.update_telegram_log._get_today_journal_path", return_value=JOURNAL_PATH),
        patch("services.obsidian.update_telegram_log.redis_client.get", return_value="01:00 PM"),
        patch("services.obsidian.utils.dropbox_rev_safe.record_deferred_write") as mock_enqueue,
    ):
        assert update_telegram_log(42, "edited telegram line") is True

    assert mock_dbx.files_upload.call_count == 2
    for call in mock_dbx.files_upload.call_args_list:
        assert call.kwargs["mode"].is_update()
        assert not call.kwargs["mode"].is_overwrite()
    mock_enqueue.assert_called()
    assert mock_enqueue.call_args.kwargs["source"] == "telegram"
    assert mock_enqueue.call_args.kwargs["kind"] == "log_update"


def test_daily_review_section_rev_conflict_retries_then_enqueues():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(SAMPLE_DA_NO_REVIEW, DA_PATH)
    mock_dbx.files_upload.side_effect = _rev_conflict()

    def fake_find(_dbx, _base, term):
        if term == "_Daily":
            return "/test/vault/_Daily"
        if term == "_Daily-Action":
            return "/test/vault/_Daily/_Daily-Action"
        return None

    class _FixedDateTime:
        @staticmethod
        def now(tz=None):
            from datetime import datetime, timezone

            when = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)
            return when.astimezone(tz) if tz is not None else when

    with (
        patch(
            "scripts.obsidian.workflows.file_updates.add_daily_review_section._get_dropbox_client",
            return_value=mock_dbx,
        ),
        patch(
            "scripts.obsidian.workflows.file_updates.add_daily_review_section._find_folder_in_path",
            side_effect=fake_find,
        ),
        patch(
            "scripts.obsidian.workflows.file_updates.add_daily_review_section.datetime",
            _FixedDateTime,
        ),
        patch("services.obsidian.utils.dropbox_rev_safe.record_deferred_write") as mock_enqueue,
    ):
        assert add_daily_review_section() is True

    assert mock_dbx.files_upload.call_count == 2
    for call in mock_dbx.files_upload.call_args_list:
        assert call.kwargs["mode"].is_update()
        assert not call.kwargs["mode"].is_overwrite()
    mock_enqueue.assert_called()
    assert mock_enqueue.call_args.kwargs["source"] == "daily_action"
    assert mock_enqueue.call_args.kwargs["kind"] == "review_section"
