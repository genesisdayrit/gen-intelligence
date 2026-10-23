"""One-off Tweets-from-@handle X Profile YAML backfill tests."""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import dropbox

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ.setdefault("SYSTEM_TIMEZONE", "America/Los_Angeles")
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")

from services.obsidian.backfill_tweet_x_profile import (  # noqa: E402
    _is_tweets_from_note_name,
    _list_tweets_from_note_paths,
    build_parser,
    plan_x_profile_backfill,
    process_note,
    run_backfill_tweet_x_profile,
)

REV = "aaaaaaaaaaaaaaaa"
CLIENT_MODIFIED = datetime(2026, 10, 8, 16, 30, tzinfo=timezone.utc)
KH = "/obsidian/personal/05_Knowledge-Hub"
TWEET_PATH = f"{KH}/Tweets from @poteto.md"
JOURNAL_PATH = "/obsidian/personal/01_daily/_journal/Oct 23, 2026.md"


def _missing_x_profile() -> str:
    return """---
title: "Tweets from @poteto"
People:
  - "[[@poteto]]"

Journal:
  - "[[Oct 8, 2026]]"
---

# Tweets from @poteto

### Bookmarked Tweets
- "old quote" ([Link](https://readwise.io/open/1))
"""


def _with_x_profile() -> str:
    return """---
title: "Tweets from @poteto"
People:
  - "[[@poteto]]"
X Profile: "https://x.com/poteto"

Journal:
  - "[[Oct 8, 2026]]"
---

# Tweets from @poteto

### Bookmarked Tweets
- "old quote" ([Link](https://readwise.io/open/1))
"""


def _cased_missing() -> str:
    return """---
title: "Tweets from @InternetH0F"
People:
  - "[[@InternetH0F]]"

Journal:
  - "[[Oct 8, 2026]]"
---

# Tweets from @InternetH0F
"""


def test_plan_inserts_after_people_and_is_idempotent():
    updated, handle = plan_x_profile_backfill(_missing_x_profile(), path=TWEET_PATH)
    assert handle == "poteto"
    assert updated == _with_x_profile()
    again, handle_again = plan_x_profile_backfill(updated, path=TWEET_PATH)
    assert handle_again == "poteto"
    assert again == updated
    assert again.count("X Profile:") == 1


def test_plan_keeps_title_handle_casing():
    path = f"{KH}/Tweets from @InternetH0F.md"
    updated, handle = plan_x_profile_backfill(_cased_missing(), path=path)
    assert handle == "InternetH0F"
    assert 'X Profile: "https://x.com/InternetH0F"' in updated
    assert "https://x.com/interneth0f" not in updated
    assert '  - "[[Oct 8, 2026]]"' in updated
    assert updated.split("X Profile:")[0].endswith('  - "[[@InternetH0F]]"\n')


def test_is_tweets_from_note_name():
    assert _is_tweets_from_note_name("Tweets from @poteto.md") is True
    assert _is_tweets_from_note_name("Tweets from @InternetH0F.md") is True
    assert _is_tweets_from_note_name("Deep Work by Cal Newport.md") is False
    assert _is_tweets_from_note_name("Tweets from Klaas.md") is False


def _download(content: str, *, rev: str = REV, path: str | None = None):
    metadata = MagicMock()
    metadata.rev = rev
    metadata.path_display = path
    metadata.client_modified = CLIENT_MODIFIED
    response = MagicMock()
    response.content = content.encode("utf-8")
    return metadata, response


def _rev_conflict() -> dropbox.exceptions.ApiError:
    reason = dropbox.files.WriteError.conflict(dropbox.files.WriteConflictError.file)
    failed = dropbox.files.UploadWriteFailed(reason=reason, upload_session_id="sess")
    error = dropbox.files.UploadError.path(failed)
    return dropbox.exceptions.ApiError("req", error, "", "")


def test_process_note_dry_run_does_not_upload():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(_missing_x_profile(), path=TWEET_PATH)

    action, handle = process_note(mock_dbx, TWEET_PATH, apply=False)

    assert action == "would_add"
    assert handle == "poteto"
    mock_dbx.files_upload.assert_not_called()


def test_process_note_apply_uploads_rev_safe_and_preserves_client_modified():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(_missing_x_profile(), path=TWEET_PATH)

    action, handle = process_note(mock_dbx, TWEET_PATH, apply=True)

    assert action == "added"
    assert handle == "poteto"
    mock_dbx.files_upload.assert_called_once()
    args, kwargs = mock_dbx.files_upload.call_args
    uploaded = args[0].decode("utf-8")
    assert kwargs["autorename"] is False
    assert kwargs["mute"] is True
    assert kwargs["mode"].is_update()
    assert kwargs["mode"].get_update() == REV
    assert not kwargs["mode"].is_overwrite()
    assert kwargs["client_modified"] == datetime(2026, 10, 8, 16, 30)
    assert kwargs["client_modified"].tzinfo is None
    assert uploaded == _with_x_profile()
    assert "modified time" not in uploaded
    assert uploaded.count("Journal:") == 1
    assert '  - "[[Oct 8, 2026]]"' in uploaded
    assert "[[Oct 23, 2026]]" not in uploaded


def test_process_note_skips_already_present_conflicted_and_rev():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(_with_x_profile(), path=TWEET_PATH)
    action, handle = process_note(mock_dbx, TWEET_PATH, apply=True)
    assert action == "skipped_already_present"
    assert handle == "poteto"
    mock_dbx.files_upload.assert_not_called()

    conflicted = f"{KH}/Tweets from @poteto (MacBook Pro's conflicted copy 2026-10-08).md"
    action, _handle = process_note(mock_dbx, conflicted, apply=True)
    assert action == "skipped_conflicted"
    mock_dbx.files_upload.assert_not_called()

    mock_dbx.files_download.return_value = _download(_missing_x_profile(), path=TWEET_PATH)
    mock_dbx.files_upload.side_effect = _rev_conflict()
    action, _handle = process_note(mock_dbx, TWEET_PATH, apply=True)
    assert action == "skipped_rev"
    mode = mock_dbx.files_upload.call_args.kwargs["mode"]
    assert mode.is_update()
    assert not mode.is_overwrite()


def test_run_apply_does_not_touch_journal_or_non_tweet_notes():
    mock_dbx = MagicMock()
    tweet_entry = MagicMock(spec=dropbox.files.FileMetadata)
    tweet_entry.name = "Tweets from @poteto.md"
    tweet_entry.path_display = TWEET_PATH
    tweet_entry.path_lower = TWEET_PATH
    article = MagicMock(spec=dropbox.files.FileMetadata)
    article.name = "Deep Work by Cal Newport.md"
    article.path_display = f"{KH}/Deep Work by Cal Newport.md"
    folder = MagicMock(spec=dropbox.files.FolderMetadata)
    folder.name = "sub"
    listed = MagicMock()
    listed.has_more = False
    listed.entries = [tweet_entry, article, folder]
    mock_dbx.files_list_folder.return_value = listed
    mock_dbx.files_download.return_value = _download(_missing_x_profile(), path=TWEET_PATH)

    journal_watch = MagicMock()

    def _upload(data, path, **_kwargs):
        assert path != JOURNAL_PATH
        assert "Content Buffet" not in data.decode("utf-8")
        return MagicMock()

    mock_dbx.files_upload.side_effect = _upload

    with patch(
        "services.obsidian.backfill_tweet_x_profile._resolve_knowledge_hub_folder",
        return_value=KH,
    ), patch(
        "services.obsidian.backfill_tweet_x_profile.append_wikilink_to_journal_buffet",
        journal_watch,
        create=True,
    ):
        counts = run_backfill_tweet_x_profile(apply=True, dbx=mock_dbx)

    assert counts["added"] == 1
    assert counts["would_add"] == 0
    mock_dbx.files_upload.assert_called_once()
    uploaded_path = mock_dbx.files_upload.call_args.args[1]
    assert uploaded_path == TWEET_PATH
    journal_watch.assert_not_called()
    names = [entry.name for entry in listed.entries]
    assert "Deep Work by Cal Newport.md" in names
    listed_paths = _list_tweets_from_note_paths(mock_dbx, KH)
    assert listed_paths == [TWEET_PATH]


def test_run_dry_run_is_default_and_idempotent_on_present_notes():
    mock_dbx = MagicMock()
    mock_dbx.files_download.return_value = _download(_with_x_profile(), path=TWEET_PATH)

    counts = run_backfill_tweet_x_profile(apply=False, dbx=mock_dbx, paths=[TWEET_PATH])

    assert counts["would_add"] == 0
    assert counts["added"] == 0
    assert counts["skipped_already_present"] == 1
    mock_dbx.files_upload.assert_not_called()


def test_cli_requires_apply_flag_to_write():
    assert build_parser().parse_args([]).apply is False
    assert build_parser().parse_args(["--apply"]).apply is True
