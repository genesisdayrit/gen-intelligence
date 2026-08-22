"""Readwise webhook and Content Buffet journal writer tests."""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytz

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

from fastapi.testclient import TestClient

from main import app
from services.obsidian.add_readwise_buffet import (
    _get_dropbox_client,
    append_readwise_buffet,
    clear_book_cache,
    format_readwise_bullet,
    get_highlight_journal_path,
    get_today_journal_path,
    insert_content_buffet_bullet,
    journal_filename,
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

JOURNAL_WITHOUT_BUFFET = """---
date: 2026-08-22
---

### Morning Pages

### Content Planning
- plan something
"""


# Official highlight payload from https://docs.readwise.io/readwise/docs/webhooks#highlight
OFFICIAL_HIGHLIGHT = {
    "id": 954480,
    "text": "Most Amazing Highlight Ever",
    "note": "",
    "location": None,
    "location_type": "page",
    "highlighted_at": "2025-11-27T18:55:56.719036Z",
    "url": None,
    "color": "",
    "updated": "2025-11-27T18:55:56.867572Z",
    "book_id": 8237,
    "tags": [],
    "event_type": "readwise.highlight.created",
    "secret": "8mFG0SsX8Xe199sRG2A3",
}


def _reader_payload(**overrides):
    data = {
        "id": "01kb5cap1wy21zp37bc2rjj",
        "url": "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj",
        "title": "Our Black Friday sale ends soon",
        "author": "The Verge",
        "source_url": "https://www.theverge.com/black-friday",
        "category": "article",
        "summary": "A sale.",
        "notes": "",
        "event_type": "reader.any_document.created",
        "secret": "test-readwise-secret",
    }
    data.update(overrides)
    return data


def _highlight_payload(**overrides):
    data = {**OFFICIAL_HIGHLIGHT, "secret": "test-readwise-secret"}
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Webhook auth / ping
# ---------------------------------------------------------------------------


def test_readwise_webhook_rejects_missing_secret():
    response = client.post("/readwise/webhook", json=_reader_payload(secret=None))
    assert response.status_code == 401


def test_readwise_webhook_rejects_wrong_secret():
    response = client.post("/readwise/webhook", json=_reader_payload(secret="nope"))
    assert response.status_code == 401


def test_readwise_webhook_empty_test_ping():
    """Readwise 'Test Webhook' empty body is a 200 no-op (no Dropbox write)."""
    with patch("main.append_readwise_buffet") as mock_append:
        response = client.post("/readwise/webhook", content=b"")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_append.assert_not_called()


def test_readwise_webhook_whitespace_ping_is_noop():
    with patch("main.append_readwise_buffet") as mock_append:
        response = client.post("/readwise/webhook", content=b"  \n")
    assert response.status_code == 200
    mock_append.assert_not_called()


@patch("main.append_readwise_buffet")
def test_readwise_webhook_ignores_reader_event(mock_append):
    """Reader document events are acked and not written."""
    response = client.post("/readwise/webhook", json=_reader_payload())
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    mock_append.assert_not_called()


@patch("main.append_readwise_buffet", return_value={"success": True, "action": "inserted"})
def test_readwise_webhook_accepts_highlight_event(mock_append):
    response = client.post("/readwise/webhook", json=_highlight_payload())
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    mock_append.assert_called_once()


@patch("main.append_readwise_buffet", side_effect=RuntimeError("dropbox down"))
def test_readwise_webhook_acks_even_if_dropbox_fails(mock_append):
    response = client.post("/readwise/webhook", json=_highlight_payload())
    assert response.status_code == 202
    mock_append.assert_called_once()


# ---------------------------------------------------------------------------
# Filename + 3am rollover
# ---------------------------------------------------------------------------


def test_journal_filename_title_case_unpadded_day():
    assert journal_filename(datetime(2026, 8, 22)) == "Aug 22, 2026.md"
    assert journal_filename(datetime(2026, 8, 1)) == "Aug 1, 2026.md"
    assert journal_filename(datetime(2026, 8, 1)) != "Aug 01, 2026.md"
    assert journal_filename(datetime(2026, 8, 22)) != "aug 22, 2026.md"


def test_journal_path_rollover_before_3am_uses_previous_day():
    now = LA.localize(datetime(2026, 8, 22, 2, 59))
    path = get_today_journal_path("/obsidian/personal/01_Daily/_Journal", now)
    assert path == "/obsidian/personal/01_Daily/_Journal/Aug 21, 2026.md"


def test_journal_path_rollover_at_3am_uses_today():
    now = LA.localize(datetime(2026, 8, 22, 3, 0))
    path = get_today_journal_path("/obsidian/personal/01_Daily/_Journal", now)
    assert path == "/obsidian/personal/01_Daily/_Journal/Aug 22, 2026.md"


def test_highlighted_at_maps_to_journal_filename():
    """Official sample 18:55Z on Nov 27 is 10:55am PT → Nov 27, 2025.md."""
    path = get_highlight_journal_path(
        "/obsidian/personal/01_Daily/_Journal",
        OFFICIAL_HIGHLIGHT,
        now=LA.localize(datetime(2026, 8, 22, 15, 0)),
    )
    assert path == "/obsidian/personal/01_Daily/_Journal/Nov 27, 2025.md"


def test_3am_utc_vs_pt_edge():
    """UTC 07:30 on Aug 22 is 00:30 PT (previous journal day); 10:00Z is 03:00 PT."""
    folder = "/obsidian/personal/01_Daily/_Journal"
    before = get_highlight_journal_path(
        folder,
        _highlight_payload(highlighted_at="2026-08-22T07:30:00Z"),
    )
    after = get_highlight_journal_path(
        folder,
        _highlight_payload(highlighted_at="2026-08-22T10:00:00Z"),
    )
    assert before == f"{folder}/Aug 21, 2026.md"
    assert after == f"{folder}/Aug 22, 2026.md"


def test_missing_highlighted_at_falls_back_to_today():
    payload = _highlight_payload()
    del payload["highlighted_at"]
    del payload["updated"]
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    path = get_highlight_journal_path(
        "/obsidian/personal/01_Daily/_Journal",
        payload,
        now=now,
    )
    assert path == "/obsidian/personal/01_Daily/_Journal/Aug 22, 2026.md"


def test_created_at_used_when_highlighted_at_missing():
    payload = _highlight_payload(created_at="2024-01-05T20:00:00Z")
    del payload["highlighted_at"]
    path = get_highlight_journal_path(
        "/obsidian/personal/01_Daily/_Journal",
        payload,
        now=LA.localize(datetime(2026, 8, 22, 15, 0)),
    )
    assert path == "/obsidian/personal/01_Daily/_Journal/Jan 5, 2024.md"


# ---------------------------------------------------------------------------
# Heading insert + empty placeholder
# ---------------------------------------------------------------------------


def test_insert_heading_before_content_planning():
    updated, action = insert_content_buffet_bullet(
        JOURNAL_WITHOUT_BUFFET,
        "- [Title](https://example.com) — Author",
    )
    assert action == "inserted"
    assert "### Content Buffet:" in updated
    buffet_idx = updated.index("### Content Buffet:")
    planning_idx = updated.index("### Content Planning")
    bullet_idx = updated.index("- [Title](https://example.com) — Author")
    assert buffet_idx < bullet_idx < planning_idx


def test_replace_lone_empty_placeholder():
    updated, action = insert_content_buffet_bullet(
        SAMPLE_JOURNAL,
        "- [Title](https://example.com) — Author",
    )
    assert action == "replaced"
    assert updated.count("- [Title](https://example.com) — Author") == 1
    section = updated.split("### Content Buffet:")[1].split("### Content Planning")[0]
    assert "-" not in section.replace("- [Title](https://example.com) — Author", "")


def test_replace_lone_dash_placeholder():
    content = "### Content Buffet:\n-\n\n### Content Planning\n"
    updated, action = insert_content_buffet_bullet(content, "- item")
    assert action == "replaced"
    assert "- item" in updated
    assert updated.split("### Content Buffet:")[1].split("### Content Planning")[0].count("\n-") == 1


def test_append_after_existing_items():
    content = """### Content Buffet:
- [Existing](https://example.com/one) — One

### Content Planning
"""
    updated, action = insert_content_buffet_bullet(
        content,
        "- [New](https://example.com/two) — Two",
    )
    assert action == "inserted"
    section = updated.split("### Content Buffet:")[1].split("### Content Planning")[0]
    assert "- [Existing](https://example.com/one) — One" in section
    assert "- [New](https://example.com/two) — Two" in section
    assert section.index("Existing") < section.index("New")


def test_dedup_skips_existing_url():
    content = """### Content Buffet:
- [Existing](https://example.com/one) — One

### Content Planning
"""
    updated, action = insert_content_buffet_bullet(
        content,
        "- [Dup](https://example.com/one) — One",
        keys=["https://example.com/one"],
    )
    assert action == "skipped"
    assert updated == content


# ---------------------------------------------------------------------------
# Bullet formatting
# ---------------------------------------------------------------------------


def test_format_official_sample_without_title():
    """Official webhook sample has no title; write the linked highlight only."""
    clear_book_cache()
    line = format_readwise_bullet(OFFICIAL_HIGHLIGHT)
    assert line == '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'


@patch.dict(os.environ, {"READWISE_TOKEN": "test-readwise-token"})
@patch("services.obsidian.add_readwise_buffet.requests.get")
def test_format_official_sample_attaches_book_title(mock_get):
    """Book DETAIL lookup supplies the title; permalinks win over payload.url."""
    clear_book_cache()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": 8237,
        "title": "Deep Work",
        "author": "Cal Newport",
        "highlights_url": "https://readwise.io/bookreview/8237",
        "source_url": None,
    }
    mock_get.return_value = mock_response

    line = format_readwise_bullet(OFFICIAL_HIGHLIGHT)
    assert line == (
        '- [Deep Work](https://readwise.io/bookreview/8237): '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    mock_get.assert_called_once_with(
        "https://readwise.io/api/v2/books/8237/",
        headers={"Authorization": "Token test-readwise-token"},
        timeout=10,
    )


@patch.dict(os.environ, {"READWISE_TOKEN": "test-readwise-token"})
@patch("services.obsidian.add_readwise_buffet.requests.get")
def test_format_highlight_includes_note_after_linked_quote(mock_get):
    clear_book_cache()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": 8237,
        "title": "Deep Work",
        "highlights_url": "https://readwise.io/bookreview/8237",
    }
    mock_get.return_value = mock_response

    line = format_readwise_bullet(_highlight_payload(note="worth revisiting"))
    assert line == (
        '- [Deep Work](https://readwise.io/bookreview/8237): '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480) — worth revisiting'
    )


def test_format_plain_quote_when_id_missing():
    clear_book_cache()
    line = format_readwise_bullet(_highlight_payload(id=None))
    assert line == '- "Most Amazing Highlight Ever"'


def test_format_ignores_reader_payload():
    assert format_readwise_bullet(_reader_payload()) is None


# ---------------------------------------------------------------------------
# Dropbox client + writer I/O
# ---------------------------------------------------------------------------


@patch("services.obsidian.add_readwise_buffet.dropbox.Dropbox")
def test_dropbox_client_uses_refresh_token(mock_dropbox):
    _get_dropbox_client()
    mock_dropbox.assert_called_once_with(
        oauth2_refresh_token="test-refresh",
        app_key="test-key",
        app_secret="test-secret",
    )


def test_append_ignores_reader_payload_without_dropbox():
    with patch("services.obsidian.add_readwise_buffet._get_dropbox_client") as mock_client:
        result = append_readwise_buffet(_reader_payload())
    assert result["action"] == "ignored"
    mock_client.assert_not_called()


def test_append_replaces_placeholder_on_effective_journal_path():
    clear_book_cache()
    uploaded = {}
    mock_dbx = MagicMock()
    response = MagicMock()
    response.content = SAMPLE_JOURNAL.encode("utf-8")
    mock_dbx.files_download.return_value = (None, response)

    def capture_upload(data, path, mode=None):
        uploaded["content"] = data.decode("utf-8")
        uploaded["path"] = path

    mock_dbx.files_upload.side_effect = capture_upload
    now = LA.localize(datetime(2026, 8, 22, 2, 30))

    with patch("services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx), \
         patch("services.obsidian.add_readwise_buffet._find_folder_by_suffix", side_effect=[
             "/obsidian/personal/01_daily",
             "/obsidian/personal/01_daily/_journal",
         ]):
        result = append_readwise_buffet(_highlight_payload(), now=now)

    assert result["success"] is True
    assert result["action"] == "replaced"
    assert uploaded["path"] == "/obsidian/personal/01_daily/_journal/Nov 27, 2025.md"
    assert '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)' in uploaded["content"]
    assert uploaded["content"].index("### Content Buffet:") < uploaded["content"].index("### Content Planning")


def test_missing_journal_file_does_not_write_today():
    """A 2019 highlight with no journal file is skipped, not written to today."""
    clear_book_cache()
    mock_dbx = MagicMock()
    mock_dbx.files_download.side_effect = FileNotFoundError("Journal not found")
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    payload = _highlight_payload(highlighted_at="2019-03-15T18:00:00Z")

    with patch("services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx), \
         patch("services.obsidian.add_readwise_buffet._find_folder_by_suffix", side_effect=[
             "/obsidian/personal/01_daily",
             "/obsidian/personal/01_daily/_journal",
         ]):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert result["action"] == "skipped_missing_journal"
    assert result["file_path"] == "/obsidian/personal/01_daily/_journal/Mar 15, 2019.md"
    mock_dbx.files_upload.assert_not_called()
    assert "Aug 22, 2026" not in (result["file_path"] or "")
