"""Readwise webhook and Content Buffet journal writer tests."""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
# Force PT so CI's exported SYSTEM_TIMEZONE (UTC / US/Eastern) cannot skip 3am rollover.
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")


@pytest.fixture(autouse=True)
def _force_la_timezone(monkeypatch):
    monkeypatch.setenv("SYSTEM_TIMEZONE", "America/Los_Angeles")

from fastapi.testclient import TestClient

from main import app
from services.obsidian.add_readwise_buffet import (
    _get_dropbox_client,
    append_readwise_buffet,
    clear_book_cache,
    document_dedup_keys,
    format_document_bullet,
    format_readwise_bullet,
    get_document_journal_path,
    get_highlight_journal_path,
    get_today_journal_path,
    insert_content_buffet_bullet,
    is_document_event,
    is_highlight_event,
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


# Official document payload from https://docs.readwise.io/readwise/docs/webhooks
OFFICIAL_DOCUMENT = {
    "id": "01kb5cap1wy21zp37bc2rjj",
    "url": "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj",
    "title": "Our Black Friday sale ends soon! Subscribe now",
    "author": "The Verge",
    "source": None,
    "category": "email",
    "site_name": "The Verge",
    "created_at": "2025-11-28T14:02:02.213618+00:00",
    "updated_at": "2025-11-28T14:02:10.648147+00:00",
    "summary": "The Verge is offering a limited-time Black Friday discount.",
    "content": None,
    "source_url": "mailto:reader-forwarded-email/90049694f7dbf92219bf18a1f6a",
    "notes": "",
    "saved_at": "2025-11-28T14:02:02.173000+00:00",
    "event_type": "reader.any_document.created",
    "secret": "lphrx901Iq3kzswFSth5ST",
}


def _reader_payload(**overrides):
    data = {
        "id": "01kb5cap1wy21zp37bc2rjj",
        "url": "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj",
        "title": "Our Black Friday sale ends soon",
        "author": "The Verge",
        "site_name": "The Verge",
        "source_url": "https://www.theverge.com/black-friday",
        "category": "article",
        "parent_id": None,
        "summary": "A sale.",
        "content": "Long article body that must not be written.",
        "notes": "",
        "created_at": "2025-11-28T14:02:02.213618+00:00",
        "saved_at": "2025-11-28T14:02:02.173000+00:00",
        "updated_at": "2025-11-28T14:02:10.648147+00:00",
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


@patch("main.append_readwise_buffet", return_value={"success": True, "action": "inserted"})
def test_readwise_webhook_accepts_document_event(mock_append):
    response = client.post("/readwise/webhook", json=_reader_payload())
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    mock_append.assert_called_once()


@pytest.mark.parametrize(
    "event_type",
    [
        "reader.any_document.created",
        "reader.non_feed_document.created",
        "reader.feed_document.created",
    ],
)
@patch("main.append_readwise_buffet", return_value={"success": True, "action": "inserted"})
def test_readwise_webhook_accepts_document_created_types(mock_append, event_type):
    response = client.post("/readwise/webhook", json=_reader_payload(event_type=event_type))
    assert response.status_code == 202
    mock_append.assert_called_once()


@pytest.mark.parametrize(
    "event_type",
    [
        "reader.document.archived",
        "reader.document.finished",
        "reader.document.tags_updated",
        "reader.document.moved_to_later",
        "reader.document.shortlisted",
    ],
)
@patch("main.append_readwise_buffet")
def test_readwise_webhook_ignores_non_created_reader_events(mock_append, event_type):
    response = client.post("/readwise/webhook", json=_reader_payload(event_type=event_type))
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    mock_append.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"category": "highlight"},
        {"category": "note"},
        {"parent_id": "01kb5parentdocumentid001"},
    ],
)
@patch("main.append_readwise_buffet")
def test_readwise_webhook_ignores_reader_highlight_and_note_documents(mock_append, overrides):
    """Reader models highlights/notes as Documents; do not write them as document bullets."""
    response = client.post("/readwise/webhook", json=_reader_payload(**overrides))
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


def test_document_created_at_maps_to_journal_filename():
    """Official sample 14:02+00 on Nov 28 is 6:02am PT → Nov 28, 2025.md."""
    path = get_document_journal_path(
        "/obsidian/personal/01_Daily/_Journal",
        OFFICIAL_DOCUMENT,
        now=LA.localize(datetime(2026, 8, 22, 15, 0)),
    )
    assert path == "/obsidian/personal/01_Daily/_Journal/Nov 28, 2025.md"


def test_document_3am_utc_vs_pt_edge():
    folder = "/obsidian/personal/01_Daily/_Journal"
    before = get_document_journal_path(
        folder,
        _reader_payload(created_at="2026-08-22T07:30:00Z"),
    )
    after = get_document_journal_path(
        folder,
        _reader_payload(created_at="2026-08-22T10:00:00Z"),
    )
    assert before == f"{folder}/Aug 21, 2026.md"
    assert after == f"{folder}/Aug 22, 2026.md"


def test_document_saved_at_used_when_created_at_missing():
    payload = _reader_payload(saved_at="2024-01-05T20:00:00Z")
    del payload["created_at"]
    path = get_document_journal_path(
        "/obsidian/personal/01_Daily/_Journal",
        payload,
        now=LA.localize(datetime(2026, 8, 22, 15, 0)),
    )
    assert path == "/obsidian/personal/01_Daily/_Journal/Jan 5, 2024.md"


def test_document_updated_at_used_when_created_and_saved_missing():
    payload = _reader_payload(updated_at="2024-02-10T20:00:00Z")
    del payload["created_at"]
    del payload["saved_at"]
    path = get_document_journal_path(
        "/obsidian/personal/01_Daily/_Journal",
        payload,
        now=LA.localize(datetime(2026, 8, 22, 15, 0)),
    )
    assert path == "/obsidian/personal/01_Daily/_Journal/Feb 10, 2024.md"


def test_document_missing_timestamps_falls_back_to_now():
    payload = _reader_payload()
    del payload["created_at"]
    del payload["saved_at"]
    del payload["updated_at"]
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    path = get_document_journal_path(
        "/obsidian/personal/01_Daily/_Journal",
        payload,
        now=now,
    )
    assert path == "/obsidian/personal/01_Daily/_Journal/Aug 22, 2026.md"


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


def test_append_after_tabbed_sub_bullets():
    content = """### Content Buffet:
- [Existing](https://example.com/one) — One
	- a nested note

### Content Planning
"""
    updated, action = insert_content_buffet_bullet(
        content,
        "- [New](https://example.com/two) — Two",
    )
    assert action == "inserted"
    section = updated.split("### Content Buffet:")[1].split("### Content Planning")[0]
    assert section.index("Existing") < section.index("a nested note") < section.index("New")


def test_document_dedup_skips_id_and_reader_url():
    content = """### Content Buffet:
- [Existing](https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj) — The Verge

### Content Planning
"""
    keys = document_dedup_keys(_reader_payload())
    assert "01kb5cap1wy21zp37bc2rjj" in keys
    assert "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj" in keys
    updated, action = insert_content_buffet_bullet(
        content,
        "- [Dup](https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj)",
        keys=keys,
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
    assert "[[" not in line


@patch.dict(os.environ, {"READWISE_TOKEN": "test-readwise-token"})
@patch("services.obsidian.add_readwise_buffet.requests.get")
def test_format_official_sample_attaches_book_title(mock_get):
    """Book DETAIL lookup supplies title + author; quote keeps the open permalink."""
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
        '- [[Deep Work - Cal Newport]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert "bookreview" not in line
    assert "(Book)" not in line
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
        "author": "Cal Newport",
        "highlights_url": "https://readwise.io/bookreview/8237",
    }
    mock_get.return_value = mock_response

    line = format_readwise_bullet(_highlight_payload(note="worth revisiting"))
    assert line == (
        '- [[Deep Work - Cal Newport]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480) — worth revisiting'
    )


def test_format_uses_payload_title_and_author():
    """Export payloads include title and author; both go inside the wikilink."""
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(title="Deep Work", author="Cal Newport")
    )
    assert line == (
        '- [[Deep Work - Cal Newport]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert "bookreview" not in line
    assert "(Book)" not in line
    assert "]] by " not in line


def test_format_keeps_readwise_author_string_verbatim():
    """Do not sort, rewrite First/Last, or split/rejoin the author field."""
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(
            title="Zero to One",
            author="Peter Thiel, Blake Masters",
        )
    )
    assert line == (
        '- [[Zero to One - Peter Thiel, Blake Masters]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )

    line = format_readwise_bullet(
        _highlight_payload(
            title="Surely You're Joking, Mr. Feynman!",
            author="Richard P. Feynman, Ralph Leighton, Edward Hutchings, and Albert R. Hibbs",
        )
    )
    assert line == (
        "- [[Surely You're Joking, Mr. Feynman! - Richard P. Feynman, "
        "Ralph Leighton, Edward Hutchings, and Albert R. Hibbs]]: "
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )

    line = format_readwise_bullet(
        _highlight_payload(
            title="Zero to One",
            author="  Peter Thiel,   Blake Masters  ",
        )
    )
    assert line == (
        '- [[Zero to One - Peter Thiel, Blake Masters]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )


def test_format_omits_author_from_wikilink_when_missing():
    clear_book_cache()
    line = format_readwise_bullet(_highlight_payload(title="Deep Work"))
    assert line == (
        '- [[Deep Work]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert " - " not in line


def test_format_author_only_when_title_missing():
    """Author without a title is a plain prefix; do not invent a wikilink."""
    clear_book_cache()
    line = format_readwise_bullet(_highlight_payload(author="Cal Newport"))
    assert line == (
        '- Cal Newport: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert "[[" not in line


def test_format_wikilink_strips_breaking_characters():
    """Strip |, #, ^, and ]] from the wikilink target only."""
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(title="Foo|Bar #1 ^block]] extra", author="Cal | Newport")
    )
    assert line == (
        '- [[FooBar 1 block extra - Cal Newport]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )


def test_format_skips_empty_wikilink_after_sanitize():
    """A title that sanitizes to nothing must not write empty [[]]."""
    clear_book_cache()
    line = format_readwise_bullet(_highlight_payload(title="|#^]]"))
    assert line == '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    assert "[[" not in line


def test_format_plain_quote_when_id_missing():
    clear_book_cache()
    line = format_readwise_bullet(_highlight_payload(id=None))
    assert line == '- "Most Amazing Highlight Ever"'
    assert "[[" not in line


def test_format_wikilink_title_and_unlinked_quote_when_id_missing():
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(id=None, title="Deep Work", author="Cal Newport")
    )
    assert line == '- [[Deep Work - Cal Newport]]: "Most Amazing Highlight Ever"'
    assert "bookreview" not in line
    assert "readwise.io/open" not in line


def test_format_ignores_reader_payload():
    """Highlight formatter stays highlight-only; documents use format_document_bullet."""
    assert format_readwise_bullet(_reader_payload()) is None
    assert format_readwise_bullet(OFFICIAL_DOCUMENT) is None


def test_format_document_official_sample_permalink():
    line = format_document_bullet(OFFICIAL_DOCUMENT)
    assert line == (
        "- [Our Black Friday sale ends soon! Subscribe now]"
        "(https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj) — The Verge"
    )
    assert "[[" not in line
    assert "bookreview" not in line
    assert "Black Friday discount" not in line
    assert "summary" not in line.lower()


def test_format_document_uses_payload_url_not_bookreview():
    line = format_document_bullet(_reader_payload())
    assert line.startswith(
        "- [Our Black Friday sale ends soon](https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj)"
    )
    assert "bookreview" not in line
    assert "Long article body" not in line


def test_format_document_title_missing_uses_site_name():
    line = format_document_bullet(_reader_payload(title=None, author=None, site_name="The Verge"))
    assert line == (
        "- [The Verge](https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj)"
    )


def test_format_document_title_missing_uses_source_host():
    line = format_document_bullet(
        _reader_payload(title=None, site_name=None, author=None, source_url="https://www.theverge.com/black-friday")
    )
    assert line == (
        "- [theverge.com](https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj)"
    )


def test_format_document_never_writes_id_only_line():
    line = format_document_bullet(
        _reader_payload(title=None, site_name=None, author=None, source_url=None)
    )
    assert line == (
        "- [read.readwise.io](https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj)"
    )
    assert "01kb5cap1wy21zp37bc2rjj]" not in line


def test_format_document_ignores_highlight_payload():
    assert format_document_bullet(OFFICIAL_HIGHLIGHT) is None
    assert format_document_bullet(_highlight_payload()) is None


def test_is_highlight_event_still_ignores_reader_star():
    """text+book_id on a reader payload stays a document, not a highlight."""
    payload = _reader_payload(text="quoted passage", book_id=8237)
    assert is_highlight_event(payload) is False
    assert is_document_event(payload) is True
    assert format_readwise_bullet(payload) is None


@pytest.mark.parametrize(
    "event_type, expected",
    [
        ("reader.any_document.created", True),
        ("reader.non_feed_document.created", True),
        ("reader.feed_document.created", True),
        ("reader.document.archived", False),
        ("reader.document.finished", False),
        ("reader.document.tags_updated", False),
        ("readwise.highlight.created", False),
    ],
)
def test_is_document_event_accepts_and_ignores_types(event_type, expected):
    if event_type == "readwise.highlight.created":
        payload = _highlight_payload()
    else:
        payload = _reader_payload(event_type=event_type)
    assert is_document_event(payload) is expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"category": "highlight"},
        {"category": "note"},
        {"category": "Highlight"},
        {"parent_id": "01kb5parentdocumentid001"},
        {"category": "article", "parent_id": "01kb5parentdocumentid001"},
    ],
)
def test_reader_highlight_and_note_documents_are_not_written(overrides):
    """Skip Reader child docs so they do not duplicate readwise.highlight.created lines."""
    payload = _reader_payload(**overrides)
    assert is_document_event(payload) is False
    assert format_document_bullet(payload) is None
    assert format_readwise_bullet(payload) is None
    with patch("services.obsidian.add_readwise_buffet._get_dropbox_client") as mock_client:
        result = append_readwise_buffet(payload)
    assert result["action"] == "ignored"
    mock_client.assert_not_called()


@pytest.mark.parametrize(
    "category",
    ["article", "pdf", "epub", "email", "rss", "tweet", "video"],
)
def test_parent_document_categories_still_count_as_documents(category):
    payload = _reader_payload(category=category, parent_id=None)
    assert is_document_event(payload) is True
    assert format_document_bullet(payload) is not None


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


def test_append_ignores_non_created_reader_payload_without_dropbox():
    with patch("services.obsidian.add_readwise_buffet._get_dropbox_client") as mock_client:
        result = append_readwise_buffet(_reader_payload(event_type="reader.document.archived"))
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


def _mock_journal_dbx(journal_content: str = SAMPLE_JOURNAL):
    uploaded = {}
    mock_dbx = MagicMock()
    response = MagicMock()
    response.content = journal_content.encode("utf-8")
    mock_dbx.files_download.return_value = (None, response)

    def capture_upload(data, path, mode=None):
        uploaded["content"] = data.decode("utf-8")
        uploaded["path"] = path

    mock_dbx.files_upload.side_effect = capture_upload
    return mock_dbx, uploaded


def test_append_writes_document_permalink_without_book_lookup():
    mock_dbx, uploaded = _mock_journal_dbx()
    now = LA.localize(datetime(2026, 8, 22, 15, 0))

    with patch("services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx), \
         patch("services.obsidian.add_readwise_buffet._find_folder_by_suffix", side_effect=[
             "/obsidian/personal/01_daily",
             "/obsidian/personal/01_daily/_journal",
         ]), \
         patch("services.obsidian.add_readwise_buffet.requests.get") as mock_get:
        result = append_readwise_buffet(_reader_payload(), now=now)

    assert result["success"] is True
    assert result["action"] == "replaced"
    assert uploaded["path"] == "/obsidian/personal/01_daily/_journal/Nov 28, 2025.md"
    assert (
        "- [Our Black Friday sale ends soon]"
        "(https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj)"
    ) in uploaded["content"]
    assert "[[" not in uploaded["content"]
    assert "bookreview" not in uploaded["content"]
    assert "Long article body" not in uploaded["content"]
    mock_get.assert_not_called()


def test_document_missing_journal_file_does_not_write_today():
    mock_dbx = MagicMock()
    mock_dbx.files_download.side_effect = FileNotFoundError("Journal not found")
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    payload = _reader_payload(created_at="2019-03-15T18:00:00Z")

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


def test_document_append_dedups_on_reader_url():
    existing = """---
date: 2025-11-28
---

### Content Buffet:
- [Our Black Friday sale ends soon](https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj) — The Verge

### Content Planning
- plan something
"""
    mock_dbx, uploaded = _mock_journal_dbx(existing)

    with patch("services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx), \
         patch("services.obsidian.add_readwise_buffet._find_folder_by_suffix", side_effect=[
             "/obsidian/personal/01_daily",
             "/obsidian/personal/01_daily/_journal",
         ]):
        result = append_readwise_buffet(_reader_payload())

    assert result["action"] == "skipped"
    mock_dbx.files_upload.assert_not_called()
    assert uploaded == {}
