"""Readwise highlight backfill job tests."""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ.setdefault("READWISE_TOKEN", "test-readwise-token")
os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")


@pytest.fixture(autouse=True)
def _force_la_timezone(monkeypatch):
    monkeypatch.setenv("SYSTEM_TIMEZONE", "America/Los_Angeles")
    monkeypatch.setenv("READWISE_TOKEN", "test-readwise-token")


from fastapi.testclient import TestClient

from main import app
from services.obsidian.add_readwise_buffet import clear_book_cache
from services.readwise.backfill import (
    DEFAULT_SINCE,
    backfill_readwise_highlights,
    highlight_effective_date,
    select_highlights,
)
from services.readwise.export import (
    EXPORT_URL,
    highlight_from_export,
    iter_export_highlights,
)

client = TestClient(app)
LA = pytz.timezone("America/Los_Angeles")

SAMPLE_JOURNAL = """---
date: 2026-08-22
---

### Content Buffet:
- 

### Content Planning
- plan something
"""

JOURNAL_FOLDER = "/obsidian/personal/01_daily/_journal"


def _book(user_book_id=8237, title="Deep Work", author="Cal Newport", highlights=None, **overrides):
    book = {
        "user_book_id": user_book_id,
        "is_deleted": False,
        "title": title,
        "readable_title": title,
        "author": author,
        "readwise_url": f"https://readwise.io/bookreview/{user_book_id}",
        "highlights": highlights or [],
    }
    book.update(overrides)
    return book


def _hl(
    highlight_id=954480,
    text="Most Amazing Highlight Ever",
    highlighted_at="2025-11-27T18:55:56.719036Z",
    book_id=8237,
    **overrides,
):
    highlight = {
        "id": highlight_id,
        "is_deleted": False,
        "text": text,
        "note": None,
        "highlighted_at": highlighted_at,
        "created_at": highlighted_at,
        "updated_at": highlighted_at,
        "url": None,
        "book_id": book_id,
        "is_discard": False,
        "readwise_url": f"https://readwise.io/open/{highlight_id}",
    }
    highlight.update(overrides)
    return highlight


def _export_page(results, next_page_cursor=None):
    return {"count": len(results), "nextPageCursor": next_page_cursor, "results": results}


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


def _run_backfill(pages, contents_by_path=None, missing_paths=None, **kwargs):
    clear_book_cache()
    mock_dbx, uploaded, store = _mock_dropbox(contents_by_path, missing_paths)
    page_iter = iter(pages)

    def fake_get(url, params=None, headers=None, timeout=None):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = next(page_iter)
        response.raise_for_status = MagicMock()
        return response

    with patch("services.readwise.export.requests.get", side_effect=fake_get) as mock_get, \
         patch("services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx), \
         patch("services.obsidian.add_readwise_buffet._find_folder_by_suffix", side_effect=[
             "/obsidian/personal/01_daily",
             JOURNAL_FOLDER,
         ]):
        result = backfill_readwise_highlights(**kwargs)
    return result, uploaded, store, mock_get


# ---------------------------------------------------------------------------
# Cutoff filtering
# ---------------------------------------------------------------------------


def test_default_since_is_streak_start():
    assert DEFAULT_SINCE == "2024-08-13"


def test_cutoff_skips_highlights_before_since():
    before = highlight_from_export(
        _book(),
        _hl(highlight_id=1, highlighted_at="2024-08-12T20:00:00Z"),
    )
    on_or_after = highlight_from_export(
        _book(),
        _hl(highlight_id=2, highlighted_at="2024-08-13T20:00:00Z"),
    )
    selected = select_highlights([before, on_or_after], "2024-08-13")
    assert [p["id"] for p in selected] == [2]


def test_cutoff_uses_3am_pt_rollover():
    """2024-08-13T09:59Z is 2:59am PT → journal date Aug 12, skipped."""
    before_rollover = highlight_from_export(
        _book(),
        _hl(highlight_id=1, highlighted_at="2024-08-13T09:59:00Z"),
    )
    at_rollover = highlight_from_export(
        _book(),
        _hl(highlight_id=2, highlighted_at="2024-08-13T10:00:00Z"),
    )
    assert highlight_effective_date(before_rollover).isoformat() == "2024-08-12"
    assert highlight_effective_date(at_rollover).isoformat() == "2024-08-13"
    selected = select_highlights([before_rollover, at_rollover], "2024-08-13")
    assert [p["id"] for p in selected] == [2]


def test_backfill_cutoff_does_not_write_pre_since_highlight():
    pages = [
        _export_page([
            _book(highlights=[
                _hl(highlight_id=1, highlighted_at="2024-01-01T18:00:00Z"),
                _hl(highlight_id=2, highlighted_at="2025-11-27T18:55:56.719036Z"),
            ]),
        ]),
    ]
    nov_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    result, uploaded, _, _ = _run_backfill(
        pages,
        contents_by_path={nov_path: SAMPLE_JOURNAL},
        since="2024-08-13",
    )
    assert result["selected"] == 1
    assert result["replaced"] == 1
    assert result["files_written"] == 1
    assert uploaded[0]["path"] == nov_path
    assert "Jan 1, 2024" not in "".join(u["path"] for u in uploaded)


# ---------------------------------------------------------------------------
# Missing journal / empty text / dedup
# ---------------------------------------------------------------------------


def test_missing_journal_is_skipped_not_written_to_today():
    pages = [
        _export_page([
            _book(highlights=[_hl(highlighted_at="2019-03-15T18:00:00Z")]),
        ]),
    ]
    today = LA.localize(datetime(2026, 8, 22, 15, 0))
    result, uploaded, _, _ = _run_backfill(
        pages,
        missing_paths={f"{JOURNAL_FOLDER}/Mar 15, 2019.md"},
        since="2019-01-01",
        now=today,
    )
    assert result["selected"] == 1
    assert result["skipped_missing_journal"] == 1
    assert result["files_written"] == 0
    assert uploaded == []
    assert result["inserted"] == 0


def test_empty_text_does_not_write():
    pages = [
        _export_page([
            _book(highlights=[
                _hl(highlight_id=1, text="  ", highlighted_at="2025-11-27T18:55:56.719036Z"),
                _hl(highlight_id=2, text="", highlighted_at="2025-11-27T18:55:56.719036Z"),
            ]),
        ]),
    ]
    nov_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    result, uploaded, _, mock_get = _run_backfill(
        pages,
        contents_by_path={nov_path: SAMPLE_JOURNAL},
    )
    assert result["selected"] == 0
    assert result["files_written"] == 0
    assert uploaded == []
    mock_get.assert_called()


def test_deleted_and_discarded_highlights_are_not_selected():
    pages = [
        _export_page([
            _book(highlights=[
                _hl(highlight_id=1, is_deleted=True),
                _hl(highlight_id=2, is_discard=True),
            ]),
            _book(user_book_id=99, title="Gone", is_deleted=True, highlights=[_hl(highlight_id=3, book_id=99)]),
        ]),
    ]
    nov_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    result, uploaded, _, _ = _run_backfill(
        pages,
        contents_by_path={nov_path: SAMPLE_JOURNAL},
    )
    assert result["selected"] == 0
    assert uploaded == []


def test_dedup_on_second_run():
    pages = [
        _export_page([
            _book(highlights=[_hl()]),
        ]),
    ]
    nov_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    first, uploaded_first, store, _ = _run_backfill(
        pages,
        contents_by_path={nov_path: SAMPLE_JOURNAL},
    )
    assert first["replaced"] == 1
    assert first["files_written"] == 1
    assert '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)' in uploaded_first[0]["content"]
    assert "[[Deep Work by Cal Newport]]:" in uploaded_first[0]["content"]
    assert "bookreview" not in uploaded_first[0]["content"]

    second, uploaded_second, _, _ = _run_backfill(
        [_export_page([_book(highlights=[_hl()])])],
        contents_by_path=store,
    )
    assert second["selected"] == 1
    assert second["skipped"] == 1
    assert second["inserted"] == 0
    assert second["replaced"] == 0
    assert second["files_written"] == 0
    assert uploaded_second == []


def test_short_highlight_ids_do_not_collide_with_dates():
    """Bare id '2' must not match the '2' in 2026-08-22."""
    pages = [
        _export_page([
            _book(highlights=[
                _hl(highlight_id=1, text="First quote"),
                _hl(highlight_id=2, text="Second quote"),
            ]),
        ]),
    ]
    nov_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    result, uploaded, _, _ = _run_backfill(
        pages,
        contents_by_path={nov_path: SAMPLE_JOURNAL},
    )
    assert result["selected"] == 2
    assert result["skipped"] == 0
    assert "First quote" in uploaded[0]["content"]
    assert "Second quote" in uploaded[0]["content"]


def test_batch_writes_one_upload_per_journal_file():
    pages = [
        _export_page([
            _book(highlights=[
                _hl(highlight_id=111001, text="First quote"),
                _hl(highlight_id=111002, text="Second quote"),
            ]),
        ]),
    ]
    nov_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    result, uploaded, _, _ = _run_backfill(
        pages,
        contents_by_path={nov_path: SAMPLE_JOURNAL},
    )
    assert result["selected"] == 2
    assert result["replaced"] == 1
    assert result["inserted"] == 1
    assert result["files_written"] == 1
    assert len(uploaded) == 1
    assert "First quote" in uploaded[0]["content"]
    assert "Second quote" in uploaded[0]["content"]


def test_export_title_used_without_books_api():
    """Export includes title; do not call GET /api/v2/books/{id}/ or say book 8237."""
    pages = [_export_page([_book(highlights=[_hl()])])]
    nov_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    with patch("services.obsidian.add_readwise_buffet.fetch_book") as mock_fetch:
        result, uploaded, _, _ = _run_backfill(
            pages,
            contents_by_path={nov_path: SAMPLE_JOURNAL},
        )
    assert result["files_written"] == 1
    mock_fetch.assert_not_called()
    assert "book 8237" not in uploaded[0]["content"]
    assert "[[Deep Work by Cal Newport]]:" in uploaded[0]["content"]
    assert '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)' in uploaded[0]["content"]
    assert "bookreview" not in uploaded[0]["content"]
    assert "(Book)" not in uploaded[0]["content"]


def test_missing_title_writes_quote_only():
    pages = [_export_page([_book(title="", readable_title="", author="", highlights=[_hl()])])]
    nov_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    with patch("services.obsidian.add_readwise_buffet.fetch_book", return_value=None):
        result, uploaded, _, _ = _run_backfill(
            pages,
            contents_by_path={nov_path: SAMPLE_JOURNAL},
        )
    assert result["files_written"] == 1
    line = [ln for ln in uploaded[0]["content"].splitlines() if "Most Amazing" in ln][0]
    assert line == '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    assert "[[" not in line
    assert "book 8237" not in uploaded[0]["content"]


def test_missing_title_with_author_writes_author_prefix():
    pages = [_export_page([_book(title="", readable_title="", highlights=[_hl()])])]
    nov_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    with patch("services.obsidian.add_readwise_buffet.fetch_book") as mock_fetch:
        result, uploaded, _, _ = _run_backfill(
            pages,
            contents_by_path={nov_path: SAMPLE_JOURNAL},
        )
    assert result["files_written"] == 1
    mock_fetch.assert_not_called()
    line = [ln for ln in uploaded[0]["content"].splitlines() if "Most Amazing" in ln][0]
    assert line == '- Cal Newport: ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    assert "[[" not in line


def test_export_carries_author_onto_payload():
    payload = highlight_from_export(_book(), _hl())
    assert payload["title"] == "Deep Work"
    assert payload["author"] == "Cal Newport"


def test_export_threads_tweet_book_fields():
    """Export books already have category/source/source_url — keep them."""
    payload = highlight_from_export(
        _book(
            title="Tweets From Georgie Dorothea 🫩",
            author="@georgiedorothea on Twitter",
            category="tweets",
            source="twitter",
            source_url="https://twitter.com/georgiedorothea",
        ),
        _hl(),
    )
    assert payload["title"] == "Tweets From Georgie Dorothea 🫩"
    assert payload["author"] == "@georgiedorothea on Twitter"
    assert payload["category"] == "tweets"
    assert payload["source"] == "twitter"
    assert payload["source_url"] == "https://twitter.com/georgiedorothea"


def test_export_tweet_highlight_uses_handle_wikilink():
    pages = [
        _export_page([
            _book(
                title="Tweets From Georgie Dorothea 🫩",
                author="@georgiedorothea on Twitter",
                category="tweets",
                source="twitter",
                source_url="https://twitter.com/georgiedorothea",
                highlights=[_hl()],
            ),
        ]),
    ]
    nov_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    with patch("services.obsidian.add_readwise_buffet.fetch_book") as mock_fetch:
        result, uploaded, _, _ = _run_backfill(
            pages,
            contents_by_path={nov_path: SAMPLE_JOURNAL},
        )
    assert result["files_written"] == 1
    mock_fetch.assert_not_called()
    line = [ln for ln in uploaded[0]["content"].splitlines() if "Most Amazing" in ln][0]
    assert line == (
        '- [[Tweets from @georgiedorothea]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert " by " not in line
    assert "bookreview" not in uploaded[0]["content"]


# ---------------------------------------------------------------------------
# Export pagination
# ---------------------------------------------------------------------------


def test_export_pagination_follows_next_page_cursor():
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params or {}, "headers": headers})
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        if not params or "pageCursor" not in params:
            response.json.return_value = _export_page(
                [_book(highlights=[_hl(highlight_id=111001, text="Page one")])],
                next_page_cursor="cursor-2",
            )
        else:
            response.json.return_value = _export_page(
                [_book(user_book_id=99, title="Other", highlights=[
                    _hl(highlight_id=111002, text="Page two", book_id=99),
                ])],
            )
        return response

    with patch("services.readwise.export.requests.get", side_effect=fake_get):
        payloads = list(iter_export_highlights(updated_after="2025-01-01T00:00:00Z"))

    assert [p["id"] for p in payloads] == [111001, 111002]
    assert [p["text"] for p in payloads] == ["Page one", "Page two"]
    assert len(calls) == 2
    assert calls[0]["url"] == EXPORT_URL
    assert calls[0]["params"] == {"updatedAfter": "2025-01-01T00:00:00Z"}
    assert calls[0]["headers"] == {"Authorization": "Token test-readwise-token"}
    assert calls[1]["params"] == {
        "pageCursor": "cursor-2",
        "updatedAfter": "2025-01-01T00:00:00Z",
    }


def test_backfill_walks_paginated_export():
    pages = [
        _export_page(
            [_book(highlights=[_hl(highlight_id=111001, text="Page one")])],
            next_page_cursor="cursor-2",
        ),
        _export_page(
            [_book(user_book_id=99, title="Other", highlights=[
                _hl(highlight_id=111002, text="Page two", book_id=99),
            ])],
        ),
    ]
    nov_path = f"{JOURNAL_FOLDER}/Nov 27, 2025.md"
    result, uploaded, _, mock_get = _run_backfill(
        pages,
        contents_by_path={nov_path: SAMPLE_JOURNAL},
    )
    assert mock_get.call_count == 2
    assert result["selected"] == 2
    assert result["files_written"] == 1
    assert "Page one" in uploaded[0]["content"]
    assert "Page two" in uploaded[0]["content"]


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def test_backfill_job_is_registered():
    from scheduler import SCHEDULED_JOBS

    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    assert "backfill_readwise_highlights" in job_ids


def test_trigger_backfill_passes_query_params():
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            "/scheduler/jobs/backfill_readwise_highlights/run",
            params={"since": "2025-01-01", "updated_after": "2026-01-01T00:00:00Z"},
        )
    assert response.status_code == 200
    assert response.json()["job_id"] == "backfill_readwise_highlights"
    mock_run.assert_called_once_with(
        "backfill_readwise_highlights",
        since="2025-01-01",
        updated_after="2026-01-01T00:00:00Z",
    )


def test_trigger_other_job_does_not_forward_readwise_params():
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            "/scheduler/jobs/send_arxiv_email/run",
            params={"since": "2025-01-01"},
        )
    assert response.status_code == 200
    mock_run.assert_called_once_with("send_arxiv_email")
