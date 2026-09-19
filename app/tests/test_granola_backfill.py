"""Manual Granola → daily journal backfill tests."""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

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
    monkeypatch.delenv("GRANOLA_BACKFILL_UPDATED_AFTER", raising=False)
    monkeypatch.delenv("GRANOLA_BACKFILL_LOOKBACK_DAYS", raising=False)
    monkeypatch.delenv("GRANOLA_BACKFILL_SINCE", raising=False)


from fastapi.testclient import TestClient

from main import app
from services.granola.backfill import (
    backfill_granola_notes,
    resolve_backfill_params,
)
from services.granola.client import NOTES_URL
from services.granola.sync import (
    CURSOR_REDIS_KEY,
    seed_updated_after,
    sync_granola_notes,
)

client = TestClient(app)

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

JOURNAL_WITH_SUMMARY_BLOCK = """---
date: 2026-09-05
---

# Sep 5, 2026

### Transcript Notes

#### [Older meeting](https://notes.granola.ai/d/old)
<!-- granola:not_alreadyThere1 -->

Already synced summary.

### Content Planning
- plan something
"""

AUG_JOURNAL = """---
date: 2026-08-01
---

# Aug 1, 2026
"""

JOURNAL_FOLDER = "/obsidian/personal/01_daily/_journal"

DEFAULT_SUMMARY_MARKDOWN = (
    "## Quarterly Yoghurt Budget Review\n"
    "\n"
    "The quarterly yoghurt budget review was a success.\n"
    "\n"
    "- Spent **$100,000** on yoghurt"
)


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
        metadata = MagicMock()
        metadata.rev = "aaaaaaaaaaaaaaaa"
        metadata.path_display = path
        response = MagicMock()
        response.content = contents_by_path[path].encode("utf-8")
        return metadata, response

    def upload(data, path, mode=None, autorename=None):
        text = data.decode("utf-8")
        contents_by_path[path] = text
        uploaded.append({"path": path, "content": text, "mode": mode, "autorename": autorename})
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


def _note_detail_id(url):
    path = (url or "").split("?", 1)[0].rstrip("/")
    prefix = NOTES_URL.rstrip("/") + "/"
    if path.startswith(prefix):
        return path[len(prefix):] or None
    return None


def _run_job(
    job,
    pages,
    contents_by_path=None,
    missing_paths=None,
    cursor_store=None,
    list_error=None,
    write_error=None,
    details_by_id=None,
    **kwargs,
):
    mock_dbx, uploaded, store = _mock_dropbox(contents_by_path, missing_paths)
    if write_error is not None:
        mock_dbx.files_upload.side_effect = write_error
    pages = list(pages)
    listed_notes = [note for page in pages for note in (page.get("notes") or [])]
    details_by_id = dict(details_by_id or {})
    page_iter = iter(pages)
    mock_redis, cursor_store = _fake_redis(cursor_store)

    def fake_get(url, params=None, headers=None, timeout=None):
        if list_error is not None:
            raise list_error
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        nid = _note_detail_id(url)
        if nid is not None:
            listed = next((note for note in listed_notes if note.get("id") == nid), {"id": nid})
            detail = {**listed, **details_by_id.get(nid, {})}
            if "summary_markdown" not in detail and "summary_text" not in details_by_id.get(nid, {}):
                detail["summary_markdown"] = DEFAULT_SUMMARY_MARKDOWN
            response.json.return_value = detail
            return response
        response.json.return_value = next(page_iter)
        return response

    with patch("services.granola.client.requests.get", side_effect=fake_get) as mock_get, \
         patch("services.granola.sync.redis_client", mock_redis), \
         patch("services.granola.sync._get_dropbox_client", return_value=mock_dbx), \
         patch(
             "services.granola.sync._resolve_journal_folder",
             return_value=JOURNAL_FOLDER,
         ):
        result = job(**kwargs)
    return result, uploaded, store, mock_get, cursor_store


def _run_backfill(*args, **kwargs):
    return _run_job(backfill_granola_notes, *args, **kwargs)


def _run_sync(*args, **kwargs):
    return _run_job(sync_granola_notes, *args, **kwargs)


def _list_call_params(mock_get):
    for call in mock_get.call_args_list:
        url = call.args[0] if call.args else ""
        if url.rstrip("/") == NOTES_URL.rstrip("/"):
            return call.kwargs.get("params") or {}
    return {}


# ---------------------------------------------------------------------------
# Param resolution (no 15m seed, omit filter by default)
# ---------------------------------------------------------------------------


def test_default_params_omit_updated_after_filter():
    now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    updated_after, lookback, since = resolve_backfill_params(now=now)
    assert updated_after is None
    assert lookback is None
    assert since is None
    assert seed_updated_after(now) == "2026-09-06T17:45:00Z"


def test_explicit_updated_after_wins_over_lookback_and_env(monkeypatch):
    monkeypatch.setenv("GRANOLA_BACKFILL_UPDATED_AFTER", "2025-01-01T00:00:00Z")
    monkeypatch.setenv("GRANOLA_BACKFILL_LOOKBACK_DAYS", "30")
    now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    updated_after, lookback, since = resolve_backfill_params(
        updated_after="2026-01-01T00:00:00Z",
        lookback_days=7,
        now=now,
    )
    assert updated_after == "2026-01-01T00:00:00Z"
    assert lookback == 7
    assert since is None


def test_empty_updated_after_omits_filter_even_with_lookback():
    now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    updated_after, lookback, _since = resolve_backfill_params(
        updated_after="",
        lookback_days=7,
        now=now,
    )
    assert updated_after is None
    assert lookback == 7


def test_lookback_days_sets_updated_after_from_now():
    now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    updated_after, lookback, _since = resolve_backfill_params(
        lookback_days=7,
        now=now,
    )
    assert updated_after == "2026-08-30T18:00:00Z"
    assert lookback == 7


def test_since_is_optional_with_no_far_past_default(monkeypatch):
    monkeypatch.setenv("GRANOLA_BACKFILL_SINCE", "2024-08-13")
    updated_after, _lookback, since = resolve_backfill_params(since="2026-01-01")
    assert updated_after is None
    assert since == "2026-01-01"

    _updated, _lookback, env_since = resolve_backfill_params()
    assert env_since == "2024-08-13"


def test_invalid_lookback_days_raises():
    with pytest.raises(ValueError, match="lookback_days"):
        resolve_backfill_params(lookback_days="nope")


def test_negative_lookback_days_raises():
    with pytest.raises(ValueError, match="lookback_days"):
        resolve_backfill_params(lookback_days=-1)


def test_backfill_does_not_use_incremental_env_or_seed(monkeypatch):
    monkeypatch.setenv("GRANOLA_NOTES_UPDATED_AFTER", "2026-09-06T00:00:00Z")
    monkeypatch.setenv("GRANOLA_SEED_LOOKBACK_MINUTES", "15")
    now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    updated_after, lookback, since = resolve_backfill_params(now=now)
    assert updated_after is None
    assert lookback is None
    assert since is None


# ---------------------------------------------------------------------------
# Default full-history list + shared cursor
# ---------------------------------------------------------------------------


def test_default_backfill_omits_updated_after_and_writes_block():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    result, uploaded, _, mock_get, cursor_store = _run_backfill(
        [_list_page([_note()])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        now=now,
    )
    params = _list_call_params(mock_get)
    assert "updated_after" not in params
    assert result["updated_after"] is None
    assert result["lookback_days"] is None
    assert result["since"] is None
    assert result["inserted"] == 1
    assert result["selected"] == 1
    content = uploaded[0]["content"]
    assert (
        "#### [Quarterly yoghurt budget review]"
        "(https://notes.granola.ai/d/f3e45e0f-24cc-480b-9a6c-8b1f5e3d7a2c)"
    ) in content
    assert "<!-- granola:not_1d3tmYTlCICgjy -->" in content
    assert DEFAULT_SUMMARY_MARKDOWN in content
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T18:00:00Z"
    assert result["cursor"] == "2026-09-06T18:00:00Z"


def test_empty_redis_backfill_does_not_use_15m_seed():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    result, _, _, mock_get, _ = _run_backfill(
        [_list_page([_note()])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        now=now,
    )
    params = _list_call_params(mock_get)
    assert "updated_after" not in params
    assert result["updated_after"] is None
    assert seed_updated_after(now) == "2026-09-06T17:45:00Z"


def test_successful_backfill_advances_shared_cursor_for_incremental():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    backfill_now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    first, _, store, _, cursor_store = _run_backfill(
        [_list_page([_note()])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        now=backfill_now,
    )
    assert first["updated_after"] is None
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T18:00:00Z"

    sync_now = datetime(2026, 9, 6, 18, 15, tzinfo=timezone.utc)
    second, _, _, mock_get, _ = _run_sync(
        [_list_page([_note()])],
        contents_by_path=store,
        cursor_store=cursor_store,
        now=sync_now,
    )
    assert second["updated_after"] == "2026-09-06T18:00:00Z"
    assert _list_call_params(mock_get)["updated_after"] == "2026-09-06T18:00:00Z"
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T18:15:00Z"
    assert second["skipped"] == 1
    assert second["inserted"] == 0


def test_lookback_days_is_passed_to_list_notes():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    result, _, _, mock_get, cursor_store = _run_backfill(
        [_list_page([_note()])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        now=now,
        lookback_days=1,
    )
    assert result["lookback_days"] == 1
    assert result["updated_after"] == "2026-09-05T18:00:00Z"
    assert _list_call_params(mock_get)["updated_after"] == "2026-09-05T18:00:00Z"
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T18:00:00Z"


def test_explicit_updated_after_is_passed_to_list_notes():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    now = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    result, _, _, mock_get, _ = _run_backfill(
        [_list_page([_note()])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        now=now,
        updated_after="2026-01-01T00:00:00Z",
        lookback_days=1,
    )
    assert result["updated_after"] == "2026-01-01T00:00:00Z"
    assert _list_call_params(mock_get)["updated_after"] == "2026-01-01T00:00:00Z"


def test_invalid_lookback_does_not_call_api_or_advance_cursor():
    result, uploaded, _, mock_get, cursor_store = _run_backfill(
        [_list_page([_note()])],
        contents_by_path={f"{JOURNAL_FOLDER}/Sep 5, 2026.md": SAMPLE_JOURNAL},
        lookback_days="nope",
    )
    assert result["errors"]
    assert "lookback_days" in result["errors"][0]
    assert uploaded == []
    assert CURSOR_REDIS_KEY not in cursor_store
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Idempotent write / upgrade (reuse sync helpers)
# ---------------------------------------------------------------------------


def test_rerun_skips_existing_html_comment_block():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    note = _note(
        note_id="not_alreadyThere1",
        title="Older meeting",
        web_url="https://notes.granola.ai/d/old",
    )
    result, uploaded, _, _, cursor_store = _run_backfill(
        [_list_page([note])],
        contents_by_path={sep_path: JOURNAL_WITH_SUMMARY_BLOCK},
        now=datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc),
    )
    assert result["selected"] == 1
    assert result["skipped"] == 1
    assert result["inserted"] == 0
    assert result["files_written"] == 0
    assert uploaded == []
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T18:00:00Z"


def test_backfill_upgrades_legacy_title_only_bullet():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    note = _note(
        note_id="not_alreadyThere1",
        title="Older meeting",
        web_url="https://notes.granola.ai/d/old",
    )
    result, uploaded, _, _, _ = _run_backfill(
        [_list_page([note])],
        contents_by_path={sep_path: JOURNAL_WITH_TRANSCRIPT_NOTES},
        now=datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc),
        details_by_id={
            "not_alreadyThere1": {"summary_markdown": "Church reflection body"},
        },
    )
    assert result["inserted"] == 1
    assert result["skipped"] == 0
    content = uploaded[0]["content"]
    assert "- [Older meeting](https://notes.granola.ai/d/old) granola:not_alreadyThere1" not in content
    assert "#### [Older meeting](https://notes.granola.ai/d/old)" in content
    assert "<!-- granola:not_alreadyThere1 -->" in content
    assert "Church reflection body" in content


def test_since_filters_by_journal_date_not_list_cursor():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    aug_path = f"{JOURNAL_FOLDER}/Aug 1, 2026.md"
    old = _note(
        note_id="not_oldMeeting0001",
        title="August meeting",
        created_at="2026-08-01T18:00:00Z",
        web_url="https://notes.granola.ai/d/aug",
    )
    recent = _note()
    result, uploaded, _, mock_get, _ = _run_backfill(
        [_list_page([old, recent])],
        contents_by_path={sep_path: SAMPLE_JOURNAL, aug_path: AUG_JOURNAL},
        now=datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc),
        since="2026-09-01",
    )
    assert "updated_after" not in _list_call_params(mock_get)
    assert result["since"] == "2026-09-01"
    assert result["selected"] == 1
    assert result["inserted"] == 1
    assert [item["path"] for item in uploaded] == [sep_path]
    assert "not_1d3tmYTlCICgjy" in uploaded[0]["content"]
    assert "not_oldMeeting0001" not in uploaded[0]["content"]


def test_missing_journal_is_skipped_not_created():
    note = _note(created_at="2026-08-01T18:00:00Z")
    result, uploaded, _, _, cursor_store = _run_backfill(
        [_list_page([note])],
        missing_paths={f"{JOURNAL_FOLDER}/Aug 1, 2026.md"},
        now=datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc),
    )
    assert result["selected"] == 1
    assert result["skipped_missing_journal"] == 1
    assert result["files_written"] == 0
    assert result["inserted"] == 0
    assert uploaded == []
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T18:00:00Z"


def test_api_failure_does_not_advance_cursor():
    stored = {CURSOR_REDIS_KEY: "2026-09-06T17:00:00Z"}
    result, uploaded, _, _, cursor_store = _run_backfill(
        [_list_page([_note()])],
        contents_by_path={f"{JOURNAL_FOLDER}/Sep 5, 2026.md": SAMPLE_JOURNAL},
        cursor_store=stored,
        list_error=RuntimeError("granola down"),
    )
    assert result["errors"]
    assert "granola down" in result["errors"][0]
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T17:00:00Z"
    assert result["cursor"] == "2026-09-06T17:00:00Z"
    assert uploaded == []


def test_write_errors_do_not_advance_cursor():
    stored = {CURSOR_REDIS_KEY: "2026-09-06T17:00:00Z"}
    result, _, _, _, cursor_store = _run_backfill(
        [_list_page([_note()])],
        contents_by_path={f"{JOURNAL_FOLDER}/Sep 5, 2026.md": SAMPLE_JOURNAL},
        cursor_store=stored,
        write_error=RuntimeError("dropbox down"),
    )
    assert result["errors"]
    assert cursor_store[CURSOR_REDIS_KEY] == "2026-09-06T17:00:00Z"
    assert result["cursor"] == "2026-09-06T17:00:00Z"


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def test_backfill_job_is_registered_with_2099_trigger():
    from scheduler import SCHEDULED_JOBS

    job = next(j for j in SCHEDULED_JOBS if j["id"] == "backfill_granola_notes")
    assert job["name"] == "Backfill Granola Notes (manual)"
    assert "2099" in str(job["trigger"])


def test_incremental_job_is_manual_2099_safety_net():
    from scheduler import SCHEDULED_JOBS

    job = next(j for j in SCHEDULED_JOBS if j["id"] == "sync_granola_notes")
    trigger = str(job["trigger"])
    assert "2099" in trigger
    assert "*/15" not in trigger


def test_trigger_backfill_passes_query_params():
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            "/scheduler/jobs/backfill_granola_notes/run",
            params={
                "updated_after": "2026-01-01T00:00:00Z",
                "lookback_days": 7,
                "since": "2024-08-13",
            },
        )
    assert response.status_code == 200
    assert response.json()["job_id"] == "backfill_granola_notes"
    assert response.json()["updated_after"] == "2026-01-01T00:00:00Z"
    assert response.json()["lookback_days"] == 7
    assert response.json()["since"] == "2024-08-13"
    mock_run.assert_called_once_with(
        "backfill_granola_notes",
        updated_after="2026-01-01T00:00:00Z",
        lookback_days=7,
        since="2024-08-13",
    )


def test_trigger_backfill_with_no_params_clears_previous_kwargs():
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post("/scheduler/jobs/backfill_granola_notes/run")
    assert response.status_code == 200
    mock_run.assert_called_once_with(
        "backfill_granola_notes",
        updated_after=None,
        lookback_days=None,
        since=None,
    )


def test_trigger_other_job_does_not_forward_granola_backfill_params():
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            "/scheduler/jobs/send_arxiv_email/run",
            params={"updated_after": "2026-01-01T00:00:00Z", "lookback_days": 7},
        )
    assert response.status_code == 200
    mock_run.assert_called_once_with("send_arxiv_email")
