"""find_hub_note_by_identity must stay cheap — never walk the Knowledge Hub."""

import logging
import os
import sys
from unittest.mock import MagicMock

import dropbox

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ.setdefault("SYSTEM_TIMEZONE", "America/Los_Angeles")
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")

from services.obsidian.add_readwise_buffet import find_hub_note_by_identity  # noqa: E402

KH_FOLDER = "/obsidian/personal/01_knowledge-hub"
YOUTUBE_URL = "https://www.youtube.com/watch?v=abcdefghijk"
VIDEO_ID = "abcdefghijk"
READWISE_ID = "01readerdoc"
SECRET_BODY = "SECRET_NOTE_BODY_MUST_NOT_BE_LOGGED"

EXISTING_NOTE = f"""---
URL: {YOUTUBE_URL}
readwise_id: {READWISE_ID}
---

## Cool Video

{SECRET_BODY}
"""


def _download_response(content: str):
    response = MagicMock()
    response.content = content.encode("utf-8")
    return None, response


def _search_result(matches):
    result = MagicMock()
    result.matches = matches
    result.has_more = False
    result.cursor = None
    return result


def _search_match(path: str, name: str | None = None):
    match = MagicMock()
    meta = MagicMock()
    meta.name = name or path.rsplit("/", 1)[-1]
    meta.path_display = path
    meta.path_lower = path
    match.metadata.get_metadata.return_value = meta
    return match


def test_find_hub_note_by_identity_stem_hit_does_not_search():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download_response(EXISTING_NOTE)

    found = find_hub_note_by_identity(
        mock_dbx,
        KH_FOLDER,
        stem="Cool Video by A Channel",
        url=YOUTUBE_URL,
        readwise_id=READWISE_ID,
    )

    assert found == (f"{KH_FOLDER}/Cool Video by A Channel.md", EXISTING_NOTE)
    mock_dbx.files_search_v2.assert_not_called()
    mock_dbx.files_search_continue_v2.assert_not_called()
    mock_dbx.files_list_folder.assert_not_called()
    mock_dbx.files_list_folder_continue.assert_not_called()
    assert mock_dbx.files_download.call_count == 1


def test_find_hub_note_by_identity_match_uses_search_and_one_download(caplog):
    note_path = f"{KH_FOLDER}/Cool Video.md"
    mock_dbx = MagicMock()
    mock_dbx.files_download.side_effect = lambda path: _download_response(EXISTING_NOTE)
    mock_dbx.files_search_v2.return_value = _search_result(
        [_search_match(note_path, "Cool Video.md")]
    )

    with caplog.at_level(logging.INFO):
        found = find_hub_note_by_identity(
            mock_dbx,
            KH_FOLDER,
            url=YOUTUBE_URL,
            readwise_id=READWISE_ID,
        )

    assert found == (note_path, EXISTING_NOTE)
    assert mock_dbx.files_search_v2.called
    queries = [call.args[0] for call in mock_dbx.files_search_v2.call_args_list]
    assert YOUTUBE_URL in queries
    assert VIDEO_ID in queries
    assert READWISE_ID in queries
    for call in mock_dbx.files_search_v2.call_args_list:
        options = call.kwargs.get("options")
        assert options is not None
        assert options.path == KH_FOLDER
        assert options.filename_only is False
    mock_dbx.files_list_folder.assert_not_called()
    mock_dbx.files_list_folder_continue.assert_not_called()
    assert mock_dbx.files_download.call_count == 1
    mock_dbx.files_download.assert_called_once_with(note_path)
    assert SECRET_BODY not in caplog.text
    assert "unique_hits=1" in caplog.text or "hits=" in caplog.text


def test_find_hub_note_by_identity_search_miss_does_not_scan_hub():
    mock_dbx = MagicMock()
    mock_dbx.files_search_v2.return_value = _search_result([])

    found = find_hub_note_by_identity(
        mock_dbx,
        KH_FOLDER,
        url="https://www.youtube.com/watch?v=zPLc3jjHbnU",
        readwise_id=READWISE_ID,
    )

    assert found is None
    assert mock_dbx.files_search_v2.called
    mock_dbx.files_list_folder.assert_not_called()
    mock_dbx.files_list_folder_continue.assert_not_called()
    mock_dbx.files_download.assert_not_called()


def test_find_hub_note_by_identity_search_error_does_not_scan_hub():
    mock_dbx = MagicMock()
    mock_dbx.files_search_v2.side_effect = dropbox.exceptions.ApiError(
        "req", MagicMock(), "", ""
    )

    found = find_hub_note_by_identity(
        mock_dbx,
        KH_FOLDER,
        url="https://www.youtube.com/watch?v=zPLc3jjHbnU",
        readwise_id=READWISE_ID,
    )

    assert found is None
    assert mock_dbx.files_search_v2.called
    mock_dbx.files_list_folder.assert_not_called()
    mock_dbx.files_list_folder_continue.assert_not_called()
    mock_dbx.files_download.assert_not_called()
    mock_dbx.files_search_continue_v2.assert_not_called()
