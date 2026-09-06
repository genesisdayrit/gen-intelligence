"""Granola → daily journal sync tests."""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ.setdefault("GRANOLA_API_KEY", "test-granola-key")
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")


@pytest.fixture(autouse=True)
def _force_la_timezone(monkeypatch):
    monkeypatch.setenv("SYSTEM_TIMEZONE", "America/Los_Angeles")
    monkeypatch.setenv("GRANOLA_API_KEY", "test-granola-key")
    monkeypatch.delenv("GRANOLA_NOTES_UPDATED_AFTER", raising=False)
    monkeypatch.delenv("GRANOLA_SEED_LOOKBACK_MINUTES", raising=False)


from fastapi.testclient import TestClient

from main import app
from services.granola.client import NOTES_URL, iter_notes
from services.granola.sync import (
    CURSOR_REDIS_KEY,
    SEED_LOOKBACK_MINUTES,
    TRANSCRIPT_NOTES_HEADER,
    format_granola_bullet,
    insert_transcript_notes_bullet,
    note_effective_date,
    seed_updated_after,
    sync_granola_notes,
)

client = TestClient(app)
LA = pytz.timezone("America/Los_Angeles")

SAMPLE_JOURNAL = """---
date: 2026-09-05
---

# Sep 5, 2026

### Morning Pages
- something

### Content Buffet:
- existing item
"""

JOURNAL_WITH_TRANSCRIPT_NOTES = """---
date: 2026-09-05
---

# Sep 5, 2026

### Transcript Notes
- [Older meeting](https://notes.granola.ai/d/old) granola:not_alreadyThere1

### Content Planning
- plan something
"""

JOURNAL_FOLDER = "/obsidian/personal/01_daily/_journal"


def _note(
    note_id="not_1d3tmYTlCICgjy",
    title="Quarterly yoghurt budget review",
    created_at="2026-09-05T20:00:00Z",
    updated_at="2026-09-05T21:00:00Z",
    web_url="https://notes.granola.ai/d/f3e45e0f-24cc-480b-9a6c-8b1f5e3d7a2c",
    **overrides,
):
    note = {
        "id": note_id,
        "object": "note",
        "title": title,
        "created_at": created_at,
        "updated_at": updated_at,
        "web_url": web_url,
    }
    note.update(overrides)
    return note


def _list_page(notes, has_more=False, cursor=None):
    return {"notes": notes, "hasMore": has_more, "cursor": cursor}


def _mock_dropbox(contents_by_path=None, missing_paths=None):
    contents_by_path = dict(contents_by_path or {})
    missing_paths = set(missing_paths or [])
    uploaded = []

    mock_dbx = MagicMock()

    def download(path):
        if path in missing_paths or path not in contents_by_path:
            raise FileNotFoundError(f"Journal not found: {path}")
        response = MagicMock()
        response.content = contents_by_path[path].encode("utf-8")
        return None, response

    def upload(data, path, mode=None):
        text = data.decode("utf-8")
        contents_by_path[path] = text
        uploaded.append({"path": path, "content": text})
        return None

    mock_dbx.files_download.side_effect = download
    mock_dbx.files_upload.side_effect = upload
    return mock_dbx, uploaded, contents_by_path


def _fake_redis(cursor_store=None):
    cursor_store = {} if cursor_store is None else cursor_store
    mock_redis = MagicMock()
    mock_redis.get.side_effect = lambda key: cursor_store.get(key)
    mock_redis.set.side_effect = lambda key, value, **_kwargs: cursor_store.__setitem__(key, value)
    return mock_redis, cursor_store


def _run_sync(
    pages,
    contents_by_path=None,
    missing_paths=None,
    cursor_store=None,
    list_error=None,
    write_error=None,
    **kwargs,
):
    mock_dbx, uploaded, store = _mock_dropbox(contents_by_path, missing_paths)
    if write_error is not None:
        mock_dbx.files_upload.side_effect = write_error
    page_iter = iter(pages)
    mock_redis, cursor_store = _fake_redis(cursor_store)

    def fake_get(url, params=None, headers=None, timeout=None):
        if list_error is not None:
            raise list_error
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = next(page_iter)
        response.raise_for_status = MagicMock()
        return response

    with patch("services.granola.client.requests.get", side_effect=fake_get) as mock_get, \
         patch("services.granola.sync.redis_client", mock_redis), \
         patch("services.granola.sync._get_dropbox_client", return_value=mock_dbx), \
         patch(
             "services.granola.sync._resolve_journal_folder",
             return_value=JOURNAL_FOLDER,
         ):
        result = sync_granola_notes(**kwargs)
    return result, uploaded, store, mock_get, cursor_store


# ---------------------------------------------------------------------------
# Empty Redis seed (not full history)
# ---------------------------------------------------------------------------


def test_empty_redis_uses_seed_not_full_history():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    result, _, _, mock_get, cursor_store = _run_sync(
        [_list_page([_note()])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        now=now,
    )
    expected_seed = seed_updated_after(now)
    assert SEED_LOOKBACK_MINUTES == 15
    assert expected_seed == "2026-09-06T17:45:00Z"
    assert result["updated_after"] == expected_seed
    assert mock_get.call_args_list[0].kwargs["params"]["updated_after"] == expected_seed
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T18:00:00Z"
    assert result["cursor"] == "2026-09-06T18:00:00Z"


# ---------------------------------------------------------------------------
# Cursor advance / API failure
# ---------------------------------------------------------------------------


def test_successful_run_advances_cursor_and_next_run_uses_it():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    first_now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    first, _, store, _, cursor_store = _run_sync(
        [_list_page([_note()])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        now=first_now,
    )
    assert first["updated_after"] == "2026-09-06T17:45:00Z"
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T18:00:00Z"

    second_now = datetime(2026, 9, 6, 18, 15, tzinfo=timezone.utc)
    second, _, _, mock_get, _ = _run_sync(
        [_list_page([_note()])],
        contents_by_path=store,
        cursor_store=cursor_store,
        now=second_now,
    )
    assert second["updated_after"] == "2026-09-06T18:00:00Z"
    assert mock_get.call_args_list[0].kwargs["params"]["updated_after"] == "2026-09-06T18:00:00Z"
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T18:15:00Z"
    assert second["cursor"] == "2026-09-06T18:15:00Z"


def test_api_failure_does_not_advance_cursor():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    stored = {CURSOR_REDIS_KEY: "2026-09-06T17:00:00Z"}
    result, uploaded, _, mock_get, cursor_store = _run_sync(
        [_list_page([_note()])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        cursor_store=stored,
        list_error=RuntimeError("granola down"),
    )
    assert result["errors"]
    assert "granola down" in result["errors"][0]
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T17:00:00Z"
    assert result["cursor"] == "2026-09-06T17:00:00Z"
    assert uploaded == []
    mock_get.assert_called()


def test_write_errors_do_not_advance_cursor():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    stored = {CURSOR_REDIS_KEY: "2026-09-06T17:00:00Z"}
    result, _, _, _, cursor_store = _run_sync(
        [_list_page([_note()])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        cursor_store=stored,
        write_error=RuntimeError("dropbox down"),
    )
    assert result["errors"]
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T17:00:00Z"
    assert result["cursor"] == "2026-09-06T17:00:00Z"
    assert result["updated_after"] == "2026-09-06T17:00:00Z"


# ---------------------------------------------------------------------------
# 3am rollover / meeting start
# ---------------------------------------------------------------------------


def test_note_at_2am_pt_lands_on_previous_journal_day():
    """2026-09-06T09:00Z is 2:00am PT → journal date Sep 5."""
    note = _note(created_at="2026-09-06T09:00:00Z")
    assert note_effective_date(note).isoformat() == "2026-09-05"

    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    result, uploaded, _, _, _ = _run_sync(
        [_list_page([note])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        now=datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc),
    )
    assert result["inserted"] == 1
    assert result["skipped_missing_journal"] == 0
    assert uploaded[0]["path"] == sep_path
    assert TRANSCRIPT_NOTES_HEADER in uploaded[0]["content"]
    assert uploaded[0]["content"].rstrip().endswith(
        "- [Quarterly yoghurt budget review]"
        "(https://notes.granola.ai/d/f3e45e0f-24cc-480b-9a6c-8b1f5e3d7a2c) "
        "granola:not_1d3tmYTlCICgjy"
    )


def test_meeting_start_preferred_over_created_at_for_journal_day():
    """Meeting at 2am PT, created later the same morning → previous journal day."""
    note = _note(
        created_at="2026-09-06T16:00:00Z",
        calendar_event={"scheduled_start_time": "2026-09-06T09:00:00Z"},
    )
    assert note_effective_date(note).isoformat() == "2026-09-05"
    created_only = _note(created_at="2026-09-06T16:00:00Z")
    assert note_effective_date(created_only).isoformat() == "2026-09-06"


# ---------------------------------------------------------------------------
# Dedup / missing journal / heading placement
# ---------------------------------------------------------------------------


def test_dedup_skips_existing_granola_id_in_transcript_notes():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    note = _note(note_id="not_alreadyThere1", title="Older meeting")
    result, uploaded, _, _, _ = _run_sync(
        [_list_page([note])],
        contents_by_path={sep_path: JOURNAL_WITH_TRANSCRIPT_NOTES},
        now=datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc),
    )
    assert result["selected"] == 1
    assert result["skipped"] == 1
    assert result["inserted"] == 0
    assert result["files_written"] == 0
    assert uploaded == []


def test_missing_journal_is_skipped_not_created():
    note = _note(created_at="2026-08-01T18:00:00Z")
    result, uploaded, _, _, _ = _run_sync(
        [_list_page([note])],
        missing_paths={f"{JOURNAL_FOLDER}/Aug 1, 2026.md"},
        now=datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc),
    )
    assert result["selected"] == 1
    assert result["skipped_missing_journal"] == 1
    assert result["files_written"] == 0
    assert result["inserted"] == 0
    assert uploaded == []


def test_missing_heading_is_created_at_eof_not_after_title():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    result, uploaded, _, _, _ = _run_sync(
        [_list_page([_note()])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        now=datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc),
    )
    assert result["inserted"] == 1
    content = uploaded[0]["content"]
    title_idx = content.index("# Sep 5, 2026")
    buffet_idx = content.index("### Content Buffet:")
    section_idx = content.index(TRANSCRIPT_NOTES_HEADER)
    assert title_idx < buffet_idx < section_idx
    assert content.rstrip().endswith("granola:not_1d3tmYTlCICgjy")


def test_existing_mid_note_section_is_not_moved():
    updated, action = insert_transcript_notes_bullet(
        JOURNAL_WITH_TRANSCRIPT_NOTES,
        format_granola_bullet(_note()),
        ["granola:not_1d3tmYTlCICgjy", "not_1d3tmYTlCICgjy"],
    )
    assert action == "inserted"
    heading_idx = updated.index(TRANSCRIPT_NOTES_HEADER)
    planning_idx = updated.index("### Content Planning")
    assert heading_idx < planning_idx
    assert "granola:not_1d3tmYTlCICgjy" in updated
    assert updated.count(TRANSCRIPT_NOTES_HEADER) == 1


# ---------------------------------------------------------------------------
# List pagination
# ---------------------------------------------------------------------------


def test_get_note_404_is_skipped_not_invented():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    listed = _note(web_url=None)
    listed.pop("web_url", None)

    def fake_get(url, params=None, headers=None, timeout=None):
        response = MagicMock()
        if url.rstrip("/").endswith(listed["id"]):
            response.status_code = 404
            return response
        response.status_code = 200
        response.json.return_value = _list_page([listed])
        return response

    mock_dbx, uploaded, _store = _mock_dropbox({sep_path: SAMPLE_JOURNAL})
    mock_redis, cursor_store = _fake_redis()
    now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    with patch("services.granola.client.requests.get", side_effect=fake_get), \
         patch("services.granola.sync.redis_client", mock_redis), \
         patch("services.granola.sync._get_dropbox_client", return_value=mock_dbx), \
         patch(
             "services.granola.sync._resolve_journal_folder",
             return_value=JOURNAL_FOLDER,
         ):
        result = sync_granola_notes(now=now)

    assert result["selected"] == 0
    assert result["inserted"] == 0
    assert uploaded == []
    assert not result["errors"]
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T18:00:00Z"


def test_list_notes_pagination_follows_cursor():
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params or {}, "headers": headers})
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        if not params or "cursor" not in params:
            response.json.return_value = _list_page(
                [_note(note_id="not_pageOne000001", title="Page one")],
                has_more=True,
                cursor="cursor-2",
            )
        else:
            response.json.return_value = _list_page(
                [_note(note_id="not_pageTwo000002", title="Page two")],
            )
        return response

    with patch("services.granola.client.requests.get", side_effect=fake_get):
        notes = list(iter_notes(updated_after="2026-09-06T17:00:00Z"))

    assert [n["id"] for n in notes] == ["not_pageOne000001", "not_pageTwo000002"]
    assert len(calls) == 2
    assert calls[0]["url"] == NOTES_URL
    assert calls[0]["params"]["updated_after"] == "2026-09-06T17:00:00Z"
    assert "folder_id" not in calls[0]["params"]
    assert calls[0]["headers"]["Authorization"] == "Bearer test-granola-key"
    assert calls[1]["params"]["cursor"] == "cursor-2"


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def test_granola_job_is_registered_with_15m_cadence():
    from scheduler import SCHEDULED_JOBS

    job = next(j for j in SCHEDULED_JOBS if j["id"] == "sync_granola_notes")
    assert job["name"] == "Sync Granola Notes to Daily Journal"
    trigger = str(job["trigger"])
    assert "*/15" in trigger
    assert "2099" not in trigger


def test_trigger_granola_sync_passes_updated_after():
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            "/scheduler/jobs/sync_granola_notes/run",
            params={"updated_after": "2026-09-06T00:00:00Z"},
        )
    assert response.status_code == 200
    assert response.json()["job_id"] == "sync_granola_notes"
    assert response.json()["updated_after"] == "2026-09-06T00:00:00Z"
    mock_run.assert_called_once_with(
        "sync_granola_notes",
        updated_after="2026-09-06T00:00:00Z",
    )


def test_trigger_other_job_does_not_forward_granola_params():
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            "/scheduler/jobs/send_arxiv_email/run",
            params={"updated_after": "2026-09-06T00:00:00Z"},
        )
    assert response.status_code == 200
    mock_run.assert_called_once_with("send_arxiv_email")
