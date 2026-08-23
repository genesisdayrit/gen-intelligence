"""Readwise webhook and Content Buffet journal writer tests."""

import os
import sys
from contextlib import contextmanager
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
    BOOK_HIGHLIGHTS_HEADER,
    BOOKMARKED_TWEETS_HEADER,
    _ensure_tweet_page_people,
    _format_book_page_bullet,
    _format_tweet_page_bullet,
    _get_dropbox_client,
    _is_book_highlight,
    _is_tweet_book,
    _new_book_page_markdown,
    _new_tweet_page_markdown,
    _people_entry_has_handle,
    _resolve_highlight_book,
    _strip_tweet_image_embeds,
    _tweet_handle,
    _tweet_people_wikilink,
    _tweet_quote_for_page,
    _tweet_wikilink_target,
    append_readwise_buffet,
    clear_book_cache,
    dedup_keys,
    document_dedup_keys,
    document_journal_date,
    document_page_url,
    fetch_book,
    format_document_bullet,
    format_readwise_bullet,
    get_document_journal_path,
    get_highlight_journal_path,
    get_today_journal_path,
    insert_book_highlights_bullet,
    insert_bookmarked_tweets_bullet,
    insert_content_buffet_bullet,
    is_document_event,
    is_highlight_event,
    journal_filename,
    knowledge_hub_note_stem,
    reader_document_extra_frontmatter,
    reader_knowledge_hub_note_stem,
    standalone_wikilink_bullet,
)
from services.obsidian.add_shared_link import _extract_frontmatter, _sanitize_filename

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


def _tweet_highlight(**overrides):
    data = {
        "title": "Tweets From Georgie Dorothea 🫩",
        "author": "@georgiedorothea on Twitter",
        "category": "tweets",
        "source": "twitter",
        "source_url": "https://twitter.com/georgiedorothea",
    }
    data.update(overrides)
    return _highlight_payload(**data)


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


def test_highlight_dedup_skips_old_title_dash_author_via_open_url():
    """Replay of the same highlight skips even when the journal still has Title - Author."""
    content = """### Content Buffet:
- [[Deep Work - Cal Newport]]: ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)

### Content Planning
"""
    payload = _highlight_payload(title="Deep Work", author="Cal Newport")
    bullet = format_readwise_bullet(payload)
    assert "[[Deep Work by Cal Newport]]" in bullet
    keys = dedup_keys(payload)
    assert "https://readwise.io/open/954480" in keys
    assert not any(key in {"Deep Work", "Cal Newport"} for key in keys)
    updated, action = insert_content_buffet_bullet(content, bullet, keys=keys)
    assert action == "skipped"
    assert updated == content
    assert "[[Deep Work]]:" not in updated


def test_highlight_dedup_does_not_match_bare_title_or_author():
    """A different highlight must not skip just because title/author already appear."""
    content = """### Content Buffet:
- [[Deep Work by Cal Newport]]: ["Old quote"](https://readwise.io/open/111)

### Content Planning
"""
    payload = _highlight_payload(
        id=222, title="Deep Work", author="Cal Newport", text="New quote"
    )
    bullet = format_readwise_bullet(payload)
    updated, action = insert_content_buffet_bullet(content, bullet, keys=dedup_keys(payload))
    assert action == "inserted"
    assert "New quote" in updated
    assert "Old quote" in updated


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
        '- [[Deep Work by Cal Newport]]: '
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
        '- [[Deep Work by Cal Newport]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480) — worth revisiting'
    )


def test_format_uses_payload_title_as_kh_stem():
    """Export payloads include title+author; wikilink is the Reader KH stem."""
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(title="Deep Work", author="Cal Newport")
    )
    assert line == (
        '- [[Deep Work by Cal Newport]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert "bookreview" not in line
    assert "read.readwise.io" not in line
    assert "(Book)" not in line
    assert "[[Deep Work - Cal Newport]]" not in line


def test_format_highlight_uses_title_by_author_stem():
    """Reader/book highlights wikilink the same Title by Author stem as the KH note."""
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(
            title="Zero to One",
            author="Peter Thiel, Blake Masters",
        )
    )
    assert line == (
        '- [[Zero to One by Peter Thiel, Blake Masters]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )

    line = format_readwise_bullet(
        _highlight_payload(
            title="Surely You're Joking, Mr. Feynman!",
            author="Richard P. Feynman, Ralph Leighton, Edward Hutchings, and Albert R. Hibbs",
        )
    )
    assert line == (
        "- [[Surely You're Joking, Mr. Feynman! by Richard P. Feynman, "
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
        '- [[Zero to One by Peter Thiel,   Blake Masters]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )


def test_highlight_stem_matches_reader_document_stem():
    """Document save and later highlight use the same Title by Author string."""
    clear_book_cache()
    title = "Our Black Friday sale ends soon"
    author = "The Verge"
    stem = reader_knowledge_hub_note_stem(title, author)
    assert stem == "Our Black Friday sale ends soon by The Verge"
    line = format_readwise_bullet(_highlight_payload(title=title, author=author))
    assert line == (
        f'- [[{stem}]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert line != (
        '- [[Our Black Friday sale ends soon]]: '
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
    assert " by " not in line


def test_format_author_only_when_title_missing():
    """Author without a title is a plain prefix; do not invent a wikilink."""
    clear_book_cache()
    line = format_readwise_bullet(_highlight_payload(author="Cal Newport"))
    assert line == (
        '- Cal Newport: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert "[[" not in line


def test_format_wikilink_uses_sanitized_filename_stem():
    """KH stem: _sanitize_filename then strip |, #, ^, and ]] from the wikilink."""
    clear_book_cache()
    title = "Foo|Bar #1 ^block]] extra"
    author = "Cal | Newport"
    expected_stem = reader_knowledge_hub_note_stem(title, author)
    assert expected_stem == "Foo_Bar 1 block extra by Cal _ Newport"
    line = format_readwise_bullet(
        _highlight_payload(title=title, author=author)
    )
    assert line == (
        f'- [[{expected_stem}]]: '
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
    assert line == '- [[Deep Work by Cal Newport]]: "Most Amazing Highlight Ever"'
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


def test_format_tweet_uses_handle_from_author():
    """Tweet books wikilink as Tweets from @handle, not Title by Author."""
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(
            title="Tweets From Georgie Dorothea 🫩",
            author="@georgiedorothea on Twitter",
            category="tweets",
            source="twitter",
            source_url="https://twitter.com/georgiedorothea",
        )
    )
    assert line == (
        '- [[Tweets from @georgiedorothea]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert "Tweets From Georgie" not in line
    assert "on Twitter" not in line
    assert "bookreview" not in line
    assert " by " not in line


def test_format_tweet_falls_back_to_twitter_url_handle():
    """When author has no @handle, use the last path segment of source_url."""
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(
            title="Tweets From Klaas",
            author="Klaas",
            source_url="https://twitter.com/forgebitz",
        )
    )
    assert line == (
        '- [[Tweets from @forgebitz]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert " by " not in line


def test_format_tweet_falls_back_to_x_com_url_handle():
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(
            category="tweets",
            title="Tweets From Klaas",
            author="Klaas",
            source_url="https://x.com/forgebitz",
        )
    )
    assert line == (
        '- [[Tweets from @forgebitz]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert " by " not in line


def test_format_tweet_missing_handle_falls_back_to_kh_stem():
    """Tweet book with no extractable handle uses the KH title stem."""
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(
            title="Tweets From Klaas",
            author="Klaas",
            category="tweets",
        )
    )
    assert line == (
        '- [[Tweets From Klaas]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert " by " not in line


def test_format_non_tweet_uses_title_by_author_stem():
    """Ordinary books wikilink the Reader Title by Author stem."""
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(
            title="Deep Work",
            author="Cal Newport",
            category="books",
            source="kindle",
            source_url="https://www.amazon.com/dp/example",
        )
    )
    assert line == (
        '- [[Deep Work by Cal Newport]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert "Tweets from" not in line
    assert "[[Deep Work - Cal Newport]]" not in line
    assert "read.readwise.io" not in line


@patch.dict(os.environ, {"READWISE_TOKEN": "test-readwise-token"})
@patch("services.obsidian.add_readwise_buffet.requests.get")
def test_format_tweet_from_fetched_book_fields(mock_get):
    """Book DETAIL cache must retain category/source/source_url/author/title."""
    clear_book_cache()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": 8237,
        "title": "Tweets From Georgie Dorothea 🫩",
        "author": "@georgiedorothea on Twitter",
        "category": "tweets",
        "source": "twitter",
        "highlights_url": "https://readwise.io/bookreview/8237",
        "source_url": "https://twitter.com/georgiedorothea",
    }
    mock_get.return_value = mock_response

    line = format_readwise_bullet(OFFICIAL_HIGHLIGHT)
    assert line == (
        '- [[Tweets from @georgiedorothea]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert "bookreview" not in line
    assert " by " not in line
    cached = fetch_book(8237)
    assert cached == {
        "title": "Tweets From Georgie Dorothea 🫩",
        "author": "@georgiedorothea on Twitter",
        "category": "tweets",
        "source": "twitter",
        "highlights_url": "https://readwise.io/bookreview/8237",
        "source_url": "https://twitter.com/georgiedorothea",
    }
    mock_get.assert_called_once()


def test_format_document_stays_markdown_link_even_for_twitter_source():
    """Reader documents never become tweet wikilinks."""
    line = format_document_bullet(
        _reader_payload(
            title="Tweets From Georgie Dorothea",
            author="@georgiedorothea on Twitter",
            category="tweets",
            source="twitter",
            source_url="https://twitter.com/georgiedorothea",
        )
    )
    assert line == (
        "- [Tweets From Georgie Dorothea]"
        "(https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj) "
        "— @georgiedorothea on Twitter"
    )
    assert "[[" not in line
    assert "Tweets from @" not in line


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


def test_append_creates_kh_note_and_buffet_wikilink_not_reader_markdown():
    """Parent document created → KH helper + [[stem]] buffet, no Reader URL bullet."""
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    payload = _reader_payload()
    title = payload["title"]
    stem = reader_knowledge_hub_note_stem(title, payload.get("author"))
    mock_dbx, uploaded = _mock_journal_dbx()

    with patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value={
            "success": True,
            "action": "created",
            "error": None,
            "file_path": f"_Knowledge-Hub/{stem}.md",
        },
    ) as mock_share, patch(
        "services.obsidian.add_readwise_buffet._create_youtube_link"
    ) as mock_youtube, patch(
        "services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx
    ):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert result["action"] == "created"
    mock_share.assert_called_once()
    assert mock_share.call_args.args[0] == "https://www.theverge.com/black-friday"
    assert mock_share.call_args.kwargs["title"] == stem
    assert stem == "Our Black Friday sale ends soon by The Verge"
    assert mock_share.call_args.kwargs["journal_date"] == "Nov 28, 2025"
    extras = mock_share.call_args.kwargs["extra_frontmatter"]
    assert extras["URL"] == "https://www.theverge.com/black-friday"
    assert extras["author"] == "[[The Verge]]"
    assert extras["readwise_id"] == "01kb5cap1wy21zp37bc2rjj"
    assert extras["readwise_url"] == "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj"
    assert extras["saved_at"] == "2025-11-28T14:02:02.213618+00:00"
    assert "published" not in extras
    assert "buffet_nested" not in mock_share.call_args.kwargs
    mock_youtube.assert_not_called()
    mock_dbx.files_upload.assert_not_called()
    assert uploaded == {}
    assert "read.readwise.io" not in str(result)


def test_append_youtube_document_uses_youtube_helper():
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    payload = _reader_payload(
        title="Cool Video",
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
        category="video",
        source="youtube",
    )

    with patch(
        "services.obsidian.add_readwise_buffet._create_youtube_link",
        return_value={"success": True, "action": "created", "error": None, "title": "Cool Video"},
    ) as mock_youtube, patch(
        "services.obsidian.add_readwise_buffet._create_shared_link"
    ) as mock_share:
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert result["action"] == "created"
    mock_youtube.assert_called_once()
    assert mock_youtube.call_args.args[0] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert mock_youtube.call_args.kwargs["journal_date"] == "Nov 28, 2025"
    extras = mock_youtube.call_args.kwargs["extra_frontmatter"]
    assert extras["readwise_id"] == "01kb5cap1wy21zp37bc2rjj"
    assert extras["URL"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert "buffet_nested" not in mock_youtube.call_args.kwargs
    mock_share.assert_not_called()


def test_append_child_document_does_not_call_kh_helpers():
    with patch("services.obsidian.add_readwise_buffet._create_shared_link") as mock_share, \
         patch("services.obsidian.add_readwise_buffet._create_youtube_link") as mock_youtube, \
         patch("services.obsidian.add_readwise_buffet._get_dropbox_client") as mock_client:
        result = append_readwise_buffet(_reader_payload(category="highlight"))

    assert result["action"] == "ignored"
    mock_share.assert_not_called()
    mock_youtube.assert_not_called()
    mock_client.assert_not_called()


def test_document_missing_journal_does_not_fail_kh_save():
    """Share helper still reports created when the journal file is missing."""
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    payload = _reader_payload(created_at="2019-03-15T18:00:00Z")

    with patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value={
            "success": True,
            "action": "created",
            "error": None,
            "file_path": "_Knowledge-Hub/Our Black Friday sale ends soon.md",
        },
    ) as mock_share, patch(
        "services.obsidian.add_readwise_buffet._get_dropbox_client"
    ) as mock_client:
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert result["action"] == "created"
    assert result["error"] is None
    mock_share.assert_called_once()
    assert mock_share.call_args.kwargs["journal_date"] == "Mar 15, 2019"
    mock_client.assert_not_called()


def test_same_day_document_does_not_double_buffet_wikilink():
    payload = _reader_payload()
    with patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        side_effect=[
            {"success": True, "action": "created", "error": None, "file_path": "_Knowledge-Hub/x.md"},
            {"success": True, "action": "skipped", "error": None, "file_path": "_Knowledge-Hub/x.md"},
        ],
    ) as mock_share:
        first = append_readwise_buffet(payload)
        second = append_readwise_buffet(payload)

    assert first["action"] == "created"
    assert second["action"] == "skipped"
    assert mock_share.call_count == 2
    assert mock_share.call_args_list[0] == mock_share.call_args_list[1]


def test_document_late_night_uses_3am_aware_journal_date():
    """UTC 07:30 on Aug 22 is 00:30 PT → previous journal day."""
    payload = _reader_payload(created_at="2026-08-22T07:30:00Z")
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    assert document_journal_date(payload, now=now) == "Aug 21, 2026"

    with patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value={"success": True, "action": "created", "error": None},
    ) as mock_share:
        append_readwise_buffet(payload, now=now)

    mock_share.assert_called_once()
    assert mock_share.call_args.kwargs["journal_date"] == "Aug 21, 2026"


def test_kh_failure_does_not_write_markdown_reader_fallback():
    mock_dbx, uploaded = _mock_journal_dbx()

    with patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value={"success": False, "action": None, "error": "dropbox down"},
    ), patch(
        "services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx
    ):
        result = append_readwise_buffet(_reader_payload())

    assert result["success"] is True
    assert result["error"] == "dropbox down"
    mock_dbx.files_upload.assert_not_called()
    assert uploaded == {}


def test_kh_exception_does_not_crash_or_write_markdown_fallback():
    mock_dbx, uploaded = _mock_journal_dbx()

    with patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        side_effect=RuntimeError("boom"),
    ), patch(
        "services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx
    ):
        result = append_readwise_buffet(_reader_payload())

    assert result["success"] is True
    assert result["action"] == "kh_error"
    mock_dbx.files_upload.assert_not_called()
    assert uploaded == {}


def test_junk_title_skips_kh_and_writes_markdown_fallback():
    mock_dbx, uploaded = _mock_journal_dbx()
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    payload = _reader_payload(title="|#^]]")

    with patch("services.obsidian.add_readwise_buffet._create_shared_link") as mock_share, \
         patch("services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx), \
         patch("services.obsidian.add_readwise_buffet._find_folder_by_suffix", side_effect=[
             "/obsidian/personal/01_daily",
             "/obsidian/personal/01_daily/_journal",
         ]):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert result["action"] == "replaced"
    mock_share.assert_not_called()
    assert "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj" in uploaded["content"]
    assert "[[" not in uploaded["content"]


def test_parent_document_create_mirrors_page_url_to_raindrop():
    """Successful KH create bookmarks document_page_url + title (not a highlight URL)."""
    payload = _reader_payload()
    with patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value={
            "success": True,
            "action": "created",
            "error": None,
            "file_path": "_Knowledge-Hub/Our Black Friday sale ends soon.md",
        },
    ), patch(
        "services.obsidian.add_readwise_buffet.create_bookmark",
        return_value={"success": True, "bookmark_id": "rd-1", "error": None},
    ) as mock_bookmark:
        result = append_readwise_buffet(payload)

    assert result["success"] is True
    assert result["action"] == "created"
    mock_bookmark.assert_called_once_with(
        "https://www.theverge.com/black-friday",
        payload["title"],
        "A sale.",
    )
    assert mock_bookmark.call_args.args[0] != "https://readwise.io/open/954480"
    assert "readwise.io/open" not in mock_bookmark.call_args.args[0]


def test_same_day_document_skip_still_attempts_raindrop():
    payload = _reader_payload()
    with patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value={
            "success": True,
            "action": "skipped",
            "error": None,
            "file_path": "_Knowledge-Hub/x.md",
        },
    ), patch(
        "services.obsidian.add_readwise_buffet.create_bookmark",
        return_value={"success": False, "bookmark_id": None, "error": "duplicate url"},
    ) as mock_bookmark:
        result = append_readwise_buffet(payload)

    assert result["success"] is True
    assert result["action"] == "skipped"
    mock_bookmark.assert_called_once_with(
        "https://www.theverge.com/black-friday",
        payload["title"],
        "A sale.",
    )


def test_highlight_does_not_mirror_to_raindrop():
    mock_dbx, _uploaded = _mock_journal_dbx()
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with patch(
        "services.obsidian.add_readwise_buffet.create_bookmark"
    ) as mock_bookmark, patch(
        "services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx
    ), patch(
        "services.obsidian.add_readwise_buffet._find_folder_by_suffix",
        side_effect=[
            "/obsidian/personal/01_daily",
            "/obsidian/personal/01_daily/_journal",
        ],
    ):
        result = append_readwise_buffet(_highlight_payload(), now=now)

    assert result["success"] is True
    assert result["action"] in {"inserted", "replaced"}
    mock_bookmark.assert_not_called()


def test_child_document_does_not_mirror_to_raindrop():
    with patch("services.obsidian.add_readwise_buffet.create_bookmark") as mock_bookmark, \
         patch("services.obsidian.add_readwise_buffet._create_shared_link") as mock_share:
        result = append_readwise_buffet(_reader_payload(category="highlight"))

    assert result["action"] == "ignored"
    mock_bookmark.assert_not_called()
    mock_share.assert_not_called()


def test_raindrop_error_does_not_fail_or_undo_kh_write():
    with patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value={
            "success": True,
            "action": "created",
            "error": None,
            "file_path": "_Knowledge-Hub/Our Black Friday sale ends soon.md",
        },
    ) as mock_share, patch(
        "services.obsidian.add_readwise_buffet.create_bookmark",
        side_effect=RuntimeError("raindrop down"),
    ):
        result = append_readwise_buffet(_reader_payload())

    assert result["success"] is True
    assert result["action"] == "created"
    assert result["error"] is None
    assert result["file_path"] == "_Knowledge-Hub/Our Black Friday sale ends soon.md"
    mock_share.assert_called_once()


def test_kh_failure_does_not_mirror_to_raindrop():
    with patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value={"success": False, "action": None, "error": "dropbox down"},
    ), patch(
        "services.obsidian.add_readwise_buffet.create_bookmark"
    ) as mock_bookmark:
        result = append_readwise_buffet(_reader_payload())

    assert result["success"] is True
    assert result["error"] == "dropbox down"
    mock_bookmark.assert_not_called()


def test_document_page_url_prefers_http_source_url():
    assert document_page_url(_reader_payload()) == "https://www.theverge.com/black-friday"
    official = document_page_url(OFFICIAL_DOCUMENT)
    assert official == "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj"
    assert not official.startswith("mailto:")


def test_knowledge_hub_note_stem_matches_shared_link_filename():
    title = 'What is AI? A "deep" look / part 1'
    assert knowledge_hub_note_stem(title) == _sanitize_filename(title)
    assert knowledge_hub_note_stem(None) is None
    assert knowledge_hub_note_stem("|#^]]") is None


def test_reader_knowledge_hub_note_stem_adds_author():
    assert reader_knowledge_hub_note_stem("Deep Work", "Cal Newport") == (
        "Deep Work by Cal Newport"
    )
    assert reader_knowledge_hub_note_stem("Deep Work", None) == "Deep Work"
    assert reader_knowledge_hub_note_stem("Deep Work", "") == "Deep Work"
    assert reader_knowledge_hub_note_stem(None, "Cal Newport") is None
    assert reader_knowledge_hub_note_stem("|#^]]", "Cal Newport") is None
    creator_payload = _reader_payload(author=None, creator="Casey Newton")
    del creator_payload["author"]
    assert reader_knowledge_hub_note_stem(
        creator_payload["title"],
        creator_payload["creator"],
    ) == "Our Black Friday sale ends soon by Casey Newton"


def test_format_highlight_special_filename_chars_match_kh_stem():
    clear_book_cache()
    title = 'What is AI? A "deep" look / part 1'
    stem = reader_knowledge_hub_note_stem(title, "Someone")
    line = format_readwise_bullet(_highlight_payload(title=title, author="Someone"))
    assert line == (
        f'- [[{stem}]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert stem == _sanitize_filename(f'{title} by Someone')
    assert "read.readwise.io" not in line
    assert "readwise.io/bookreview" not in line


# ---------------------------------------------------------------------------
# Locked buffet shape: standalone wikilink + separate highlight lines
# ---------------------------------------------------------------------------


def _buffet_lines(content: str) -> list[str]:
    section = content.split("### Content Buffet:")[1].split("### Content Planning")[0]
    return [line for line in section.splitlines() if line.strip()]


def test_standalone_wikilink_is_title_only_no_quote_or_readwise_url():
    prepared = standalone_wikilink_bullet("Deep Work")
    assert prepared is not None
    bullet, keys = prepared
    assert bullet == "- [[Deep Work]]"
    assert keys == ["- [[Deep Work]]"]
    assert ":" not in bullet
    assert "readwise" not in bullet
    assert '"' not in bullet


def test_highlight_appends_separate_line_without_removing_standalone():
    """Document line stays; highlight.created appends a second line."""
    content = """### Content Buffet:
- [[Deep Work]]

### Content Planning
"""
    payload = _highlight_payload(title="Deep Work", author="Cal Newport")
    bullet = format_readwise_bullet(payload)
    assert bullet == (
        '- [[Deep Work by Cal Newport]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    updated, action = insert_content_buffet_bullet(content, bullet, keys=dedup_keys(payload))
    assert action == "inserted"
    assert _buffet_lines(updated) == [
        "- [[Deep Work]]",
        '- [[Deep Work by Cal Newport]]: ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)',
    ]


def test_standalone_wikilink_does_not_collapse_into_existing_highlight_line():
    """Highlight-first: later document save still writes the standalone line."""
    content = """### Content Buffet:
- [[Deep Work]]: ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)

### Content Planning
"""
    prepared = standalone_wikilink_bullet("Deep Work")
    assert prepared is not None
    bullet, keys = prepared
    updated, action = insert_content_buffet_bullet(
        content, bullet, keys, exact_line=True
    )
    assert action == "inserted"
    assert _buffet_lines(updated) == [
        '- [[Deep Work]]: ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)',
        "- [[Deep Work]]",
    ]


def test_highlight_dedup_does_not_skip_because_standalone_wikilink_exists():
    content = """### Content Buffet:
- [[Deep Work]]

### Content Planning
"""
    payload = _highlight_payload(title="Deep Work")
    updated, action = insert_content_buffet_bullet(
        content, format_readwise_bullet(payload), keys=dedup_keys(payload)
    )
    assert action == "inserted"
    assert "https://readwise.io/open/954480" in updated
    assert _buffet_lines(updated)[0] == "- [[Deep Work]]"


def test_standalone_wikilink_dedups_only_exact_line():
    content = """### Content Buffet:
- [[Deep Work]]

### Content Planning
"""
    prepared = standalone_wikilink_bullet("Deep Work")
    assert prepared is not None
    updated, action = insert_content_buffet_bullet(
        content, prepared[0], prepared[1], exact_line=True
    )
    assert action == "skipped"
    assert updated == content


def test_locked_tweet_highlight_still_uses_handle_wikilink():
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(
            title="Tweets From Georgie Dorothea 🫩",
            author="@georgiedorothea on Twitter",
            category="tweets",
            source="twitter",
            source_url="https://twitter.com/georgiedorothea",
        )
    )
    assert line == (
        '- [[Tweets from @georgiedorothea]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )
    assert line != "- [[Tweets from @georgiedorothea]]"


def test_format_live_interneth0f_keeps_tco_and_open_link():
    """Aug 23 journal: two empty-alt twimg images after t.co, stripped by hand."""
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(
            id=1047167879,
            text=(
                "New York is now the #1 market for tech talent, dethroning "
                "San Francisco's 13-year reign (via: CNBC) https://t.co/L3eHRbgQyG "
                "![](https://pbs.twimg.com/media/HQYkftsXEAAsKdm.jpg) "
                "![](https://pbs.twimg.com/media/HQYkgifXEAAfKbH.jpg)"
            ),
            title="Tweets From InternetH0F",
            author="@InternetH0F on Twitter",
            category="tweets",
            source="twitter",
            source_url="https://twitter.com/InternetH0F",
        )
    )
    assert line == (
        '- [[Tweets from @InternetH0F]]: '
        '["New York is now the #1 market for tech talent, dethroning '
        "San Francisco's 13-year reign (via: CNBC) https://t.co/L3eHRbgQyG"
        '"](https://readwise.io/open/1047167879)'
    )
    assert "![]" not in line
    assert "pbs.twimg.com" not in line


def test_format_live_flower_alicee_keeps_tco_and_open_link():
    """Aug 23 journal: two empty-alt twimg images after t.co, stripped by hand."""
    clear_book_cache()
    line = format_readwise_bullet(
        _highlight_payload(
            id=1047167880,
            text=(
                "...but then when https://t.co/p3GyToJO6M "
                "![](https://pbs.twimg.com/media/HQWvI2ZbIAA4WBd.jpg) "
                "![](https://pbs.twimg.com/media/HQWvI2ZaoAAdqfb.jpg)"
            ),
            title="Tweets From flower_alicee",
            author="@flower_alicee on Twitter",
            category="tweets",
            source="twitter",
            source_url="https://twitter.com/flower_alicee",
        )
    )
    assert line == (
        '- [[Tweets from @flower_alicee]]: '
        '["...but then when https://t.co/p3GyToJO6M"]'
        "(https://readwise.io/open/1047167880)"
    )
    assert "![]" not in line
    assert "pbs.twimg.com" not in line


def test_format_tweet_strips_markdown_image_to_text_only():
    clear_book_cache()
    line = format_readwise_bullet(
        _tweet_highlight(text="Quote text ![](https://pbs.twimg.com/media/foo.jpg)")
    )
    assert line == (
        '- [[Tweets from @georgiedorothea]]: '
        '["Quote text"](https://readwise.io/open/954480)'
    )
    assert "pbs.twimg.com" not in line
    assert "![]" not in line


def test_format_tweet_strips_markdown_image_on_own_line():
    clear_book_cache()
    line = format_readwise_bullet(
        _tweet_highlight(
            text="Quote text\n\n![alt](https://pbs.twimg.com/media/foo.jpg)"
        )
    )
    assert line == (
        '- [[Tweets from @georgiedorothea]]: '
        '["Quote text"](https://readwise.io/open/954480)'
    )
    assert "pbs.twimg.com" not in line
    assert "![" not in line


def test_format_tweet_strips_html_img():
    clear_book_cache()
    line = format_readwise_bullet(
        _tweet_highlight(
            text='Quote text <img src="https://pbs.twimg.com/media/foo.jpg" alt="pic">'
        )
    )
    assert line == (
        '- [[Tweets from @georgiedorothea]]: '
        '["Quote text"](https://readwise.io/open/954480)'
    )
    assert "<img" not in line
    assert "pbs.twimg.com" not in line


def test_format_tweet_strips_bare_twimg_url():
    clear_book_cache()
    line = format_readwise_bullet(
        _tweet_highlight(
            text="Quote text https://pbs.twimg.com/media/foo.jpg pic.twitter.com/abc123"
        )
    )
    assert line == (
        '- [[Tweets from @georgiedorothea]]: '
        '["Quote text"](https://readwise.io/open/954480)'
    )
    assert "pbs.twimg.com" not in line
    assert "pic.twitter.com" not in line


def test_format_tweet_strips_bare_video_twimg_url():
    clear_book_cache()
    line = format_readwise_bullet(
        _tweet_highlight(
            text="Quote text https://video.twimg.com/ext_tw_video/1/pu/vid/foo.mp4"
        )
    )
    assert line == (
        '- [[Tweets from @georgiedorothea]]: '
        '["Quote text"](https://readwise.io/open/954480)'
    )
    assert "video.twimg.com" not in line


def test_format_non_tweet_unrelated_url_is_unchanged():
    clear_book_cache()
    text = "See https://example.com/diagram.png for the chart"
    line = format_readwise_bullet(
        _highlight_payload(
            text=text,
            title="Deep Work",
            author="Cal Newport",
            category="books",
            source="kindle",
        )
    )
    assert line == (
        '- [[Deep Work by Cal Newport]]: '
        f'["{text}"](https://readwise.io/open/954480)'
    )
    assert "https://example.com/diagram.png" in line


def test_format_non_tweet_markdown_image_is_unchanged():
    """Article/book quotes keep image markup; stripping is tweet-only."""
    clear_book_cache()
    text = "A figure ![](https://example.com/chart.png) in the book"
    line = format_readwise_bullet(
        _highlight_payload(
            text=text,
            title="Deep Work",
            author="Cal Newport",
            category="books",
        )
    )
    assert line == (
        '- [[Deep Work by Cal Newport]]: '
        f'["{text}"](https://readwise.io/open/954480)'
    )
    assert "![](https://example.com/chart.png)" in line


def test_format_tweet_image_only_is_skipped():
    clear_book_cache()
    line = format_readwise_bullet(
        _tweet_highlight(text="![](https://pbs.twimg.com/media/foo.jpg)")
    )
    assert line is None


def test_append_tweet_image_only_does_not_write():
    """Empty-after-strip skips the journal line, same as empty highlight text."""
    clear_book_cache()
    with patch("services.obsidian.add_readwise_buffet._get_dropbox_client") as mock_client:
        result = append_readwise_buffet(
            _tweet_highlight(text="![](https://pbs.twimg.com/media/foo.jpg)")
        )
    assert result["success"] is True
    assert result["action"] == "ignored"
    mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# Reader document metadata (KH YAML only; journal buffet is standalone)
# ---------------------------------------------------------------------------


def test_reader_extra_frontmatter_from_payload():
    extras = reader_document_extra_frontmatter(_reader_payload())
    assert extras == {
        "URL": "https://www.theverge.com/black-friday",
        "author": "[[The Verge]]",
        "readwise_id": "01kb5cap1wy21zp37bc2rjj",
        "readwise_url": "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj",
        "saved_at": "2025-11-28T14:02:02.213618+00:00",
    }
    assert "published" not in extras


def test_reader_extra_omits_missing_published_and_author():
    payload = _reader_payload(author=None, creator=None)
    del payload["author"]
    extras = reader_document_extra_frontmatter(payload)
    assert "author" not in extras
    assert "published" not in extras


def test_reader_extra_uses_published_date_and_creator():
    payload = _reader_payload(
        author=None,
        creator="Casey Newton",
        published_date="2025-11-20T08:15:00+00:00",
    )
    extras = reader_document_extra_frontmatter(payload)
    assert extras["author"] == "[[Casey Newton]]"
    assert extras["published"] == "2025-11-20"


def test_reader_extra_mailto_source_omits_public_url():
    extras = reader_document_extra_frontmatter(OFFICIAL_DOCUMENT)
    assert "URL" not in extras
    assert extras["readwise_url"] == "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj"


def test_standalone_wikilink_with_nested_dedups_on_first_line_only():
    prepared = standalone_wikilink_bullet(
        "Deep Work",
        nested_lines=[
            "  - [source](https://example.com/deep-work)",
            "  - Cal Newport",
        ],
    )
    assert prepared is not None
    bullet, keys = prepared
    assert bullet.startswith("- [[Deep Work]]\n")
    assert keys == ["- [[Deep Work]]"]
    assert '"' not in bullet
    content = """### Content Buffet:
- [[Deep Work]]
  - [source](https://example.com/deep-work)
  - Cal Newport

### Content Planning
"""
    updated, action = insert_content_buffet_bullet(
        content, bullet, keys, exact_line=True
    )
    assert action == "skipped"
    assert updated == content


def test_highlight_append_does_not_add_another_metadata_block():
    """Highlight.created appends a quote line; journal stays standalone + quote."""
    content = """### Content Buffet:
- [[Deep Work]]

### Content Planning
"""
    payload = _highlight_payload(title="Deep Work", author="Cal Newport")
    bullet = format_readwise_bullet(payload)
    updated, action = insert_content_buffet_bullet(content, bullet, keys=dedup_keys(payload))
    assert action == "inserted"
    lines = _buffet_lines(updated)
    assert lines == [
        "- [[Deep Work]]",
        '- [[Deep Work by Cal Newport]]: ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)',
    ]
    assert "readwise.io" not in "\n".join(lines[:-1])
    assert "published:" not in "\n".join(lines)
    assert "saved:" not in "\n".join(lines)
    assert not any(key in {"Cal Newport", "https://calnewport.com/deep-work"} for key in dedup_keys(payload))


# ---------------------------------------------------------------------------
# Tweets from @handle Knowledge Hub page
# ---------------------------------------------------------------------------

KH_FOLDER = "/obsidian/personal/01_knowledge-hub"
TWEET_PAGE_PATH = f"{KH_FOLDER}/Tweets from @georgiedorothea.md"
JOURNAL_NOV_PATH = "/obsidian/personal/01_daily/_journal/Nov 27, 2025.md"


def _tweet_highlight(**overrides):
    data = _highlight_payload(
        title="Tweets From Georgie Dorothea 🫩",
        author="@georgiedorothea on Twitter",
        category="tweets",
        source="twitter",
        source_url="https://twitter.com/georgiedorothea",
    )
    data.update(overrides)
    return data


def _mock_vault_dbx(files_by_path=None):
    store = dict(files_by_path or {})
    uploaded = []
    mock_dbx = MagicMock()

    def download(path):
        if path not in store:
            raise FileNotFoundError(f"not found: {path}")
        response = MagicMock()
        response.content = store[path].encode("utf-8")
        return None, response

    def upload(data, path, mode=None):
        text = data.decode("utf-8")
        store[path] = text
        uploaded.append({"path": path, "content": text})

    def list_folder(path, recursive=False):
        result = MagicMock()
        result.has_more = False
        prefix = path.rstrip("/") + "/"
        entries = []
        for file_path in store:
            if not file_path.startswith(prefix):
                continue
            name = file_path[len(prefix) :]
            if "/" in name:
                continue
            entry = MagicMock()
            entry.name = name
            entry.path_lower = file_path
            entry.path_display = file_path
            entries.append(entry)
        result.entries = entries
        return result

    mock_dbx.files_download.side_effect = download
    mock_dbx.files_upload.side_effect = upload
    mock_dbx.files_list_folder.side_effect = list_folder
    return mock_dbx, uploaded, store


def _folder_by_suffix(_dbx, _parent, suffix):
    return {
        "_Daily": "/obsidian/personal/01_daily",
        "_Journal": "/obsidian/personal/01_daily/_journal",
        "_Knowledge-Hub": KH_FOLDER,
    }[suffix]


@contextmanager
def _journal_and_hub(mock_dbx):
    with patch(
        "services.obsidian.add_readwise_buffet._get_dropbox_client",
        return_value=mock_dbx,
    ), patch(
        "services.obsidian.add_readwise_buffet._find_folder_by_suffix",
        side_effect=_folder_by_suffix,
    ), patch(
        "services.obsidian.add_readwise_buffet._resolve_knowledge_hub_folder",
        return_value=KH_FOLDER,
    ):
        yield


def test_insert_bookmarked_tweets_appends_missing_heading():
    content = """---
title: "Tweets from @georgiedorothea"
---

# Tweets from @georgiedorothea

## People
- [[Someone]]

Kept body text.
"""
    bullet = '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    updated, action = insert_bookmarked_tweets_bullet(
        content, bullet, keys=["https://readwise.io/open/954480"]
    )
    assert action == "inserted"
    assert "## People" in updated
    assert "- [[Someone]]" in updated
    assert "Kept body text." in updated
    assert BOOKMARKED_TWEETS_HEADER in updated
    assert updated.index("Kept body text.") < updated.index(BOOKMARKED_TWEETS_HEADER)
    assert bullet in updated
    assert "[[Tweets from @georgiedorothea]]" not in updated


def test_insert_bookmarked_tweets_dedups_open_url_not_handle():
    content = f"""# Tweets from @georgiedorothea

{BOOKMARKED_TWEETS_HEADER}
- ["Old quote"](https://readwise.io/open/111)
"""
    same_id = '- ["Same id new wording"](https://readwise.io/open/111)'
    updated, action = insert_bookmarked_tweets_bullet(
        content, same_id, keys=["https://readwise.io/open/111"]
    )
    assert action == "skipped"
    assert updated == content

    other = '- ["New quote"](https://readwise.io/open/222)'
    updated, action = insert_bookmarked_tweets_bullet(
        content, other, keys=["https://readwise.io/open/222"]
    )
    assert action == "inserted"
    assert "Old quote" in updated
    assert "New quote" in updated
    assert updated.count("georgiedorothea") == 1


def test_format_tweet_page_bullet_omits_wikilink_and_keeps_note():
    clear_book_cache()
    payload = _tweet_highlight(note="worth revisiting")
    book = _resolve_highlight_book(payload)
    journal = format_readwise_bullet(payload)
    page = _format_tweet_page_bullet(payload, book)
    assert journal == (
        '- [[Tweets from @georgiedorothea]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480) — worth revisiting'
    )
    assert page == (
        '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480) — worth revisiting'
    )
    assert "[[Tweets from @georgiedorothea]]" not in page
    assert _tweet_wikilink_target(book) == "Tweets from @georgiedorothea"


def test_strip_tweet_image_embeds_matches_journal_rules():
    assert _strip_tweet_image_embeds(
        "New York is now the #1 market for tech talent, dethroning "
        "San Francisco's 13-year reign (via: CNBC) https://t.co/L3eHRbgQyG "
        "![](https://pbs.twimg.com/media/HQYkftsXEAAsKdm.jpg) "
        "![](https://pbs.twimg.com/media/HQYkgifXEAAfKbH.jpg)"
    ) == (
        "New York is now the #1 market for tech talent, dethroning "
        "San Francisco's 13-year reign (via: CNBC) https://t.co/L3eHRbgQyG"
    )
    assert (
        _strip_tweet_image_embeds(
            'Quote text <img src="https://pbs.twimg.com/media/foo.jpg" alt="pic">'
        )
        == "Quote text"
    )
    assert (
        _strip_tweet_image_embeds(
            "Quote text https://pbs.twimg.com/media/foo.jpg pic.twitter.com/abc123"
        )
        == "Quote text"
    )
    assert (
        _strip_tweet_image_embeds(
            "Quote text https://video.twimg.com/ext_tw_video/1/pu/vid/foo.mp4"
        )
        == "Quote text"
    )
    assert _strip_tweet_image_embeds("![](https://pbs.twimg.com/media/foo.jpg)") is None
    assert _tweet_quote_for_page("Quote text ![](https://pbs.twimg.com/media/foo.jpg)") == (
        "Quote text"
    )


def test_format_tweet_page_bullet_strips_images_keeps_quote_and_open_id():
    clear_book_cache()
    payload = _tweet_highlight(
        text=(
            "New York is now the #1 market for tech talent, dethroning "
            "San Francisco's 13-year reign (via: CNBC) https://t.co/L3eHRbgQyG "
            "![](https://pbs.twimg.com/media/HQYkftsXEAAsKdm.jpg) "
            "![](https://pbs.twimg.com/media/HQYkgifXEAAfKbH.jpg)"
        )
    )
    book = _resolve_highlight_book(payload)
    page = _format_tweet_page_bullet(payload, book)
    assert page == (
        '- ["New York is now the #1 market for tech talent, dethroning '
        "San Francisco's 13-year reign (via: CNBC) https://t.co/L3eHRbgQyG"
        '"](https://readwise.io/open/954480)'
    )
    assert "![]" not in page
    assert "<img" not in page
    assert "pbs.twimg.com" not in page
    assert "pic.twitter.com" not in page
    assert "video.twimg.com" not in page
    assert "https://readwise.io/open/954480" in page


def test_format_tweet_page_bullet_strips_html_and_bare_twimg():
    clear_book_cache()
    book = _resolve_highlight_book(_tweet_highlight())
    html = _format_tweet_page_bullet(
        _tweet_highlight(text='Quote text <img src="https://pbs.twimg.com/media/foo.jpg">'),
        book,
    )
    assert html == '- ["Quote text"](https://readwise.io/open/954480)'
    bare = _format_tweet_page_bullet(
        _tweet_highlight(
            text="Quote text https://pbs.twimg.com/media/foo.jpg pic.twitter.com/abc"
        ),
        book,
    )
    assert bare == '- ["Quote text"](https://readwise.io/open/954480)'
    video = _format_tweet_page_bullet(
        _tweet_highlight(
            text="Quote text https://video.twimg.com/ext_tw_video/1/pu/vid/foo.mp4"
        ),
        book,
    )
    assert video == '- ["Quote text"](https://readwise.io/open/954480)'


def test_format_tweet_page_bullet_image_only_is_none():
    clear_book_cache()
    book = _resolve_highlight_book(_tweet_highlight())
    assert (
        _format_tweet_page_bullet(
            _tweet_highlight(text="![](https://pbs.twimg.com/media/foo.jpg)"),
            book,
        )
        is None
    )


def test_format_tweet_page_bullet_skips_missing_handle_or_text():
    clear_book_cache()
    no_handle = _resolve_highlight_book(
        _highlight_payload(title="Tweets From Klaas", author="Klaas", category="tweets")
    )
    assert _tweet_wikilink_target(no_handle) is None
    assert _format_tweet_page_bullet(
        _highlight_payload(title="Tweets From Klaas", author="Klaas", category="tweets"),
        no_handle,
    ) is None

    book = _resolve_highlight_book(_tweet_highlight())
    assert _format_tweet_page_bullet(_tweet_highlight(text=""), book) is None
    assert _format_tweet_page_bullet(_tweet_highlight(text="   "), book) is None
    article = _resolve_highlight_book(
        _highlight_payload(title="Deep Work", author="Cal Newport", category="books")
    )
    assert _format_tweet_page_bullet(
        _highlight_payload(title="Deep Work", author="Cal Newport", category="books"),
        article,
    ) is None


def test_new_tweet_page_markdown_is_minimal():
    markdown = _new_tweet_page_markdown(
        "Tweets from @georgiedorothea",
        '- ["quote"](https://readwise.io/open/954480)',
        "georgiedorothea",
    )
    assert markdown.startswith("---\ntitle: \"Tweets from @georgiedorothea\"\n")
    assert '  - "[[@georgiedorothea]]"' in markdown
    assert "# Tweets from @georgiedorothea" in markdown
    assert BOOKMARKED_TWEETS_HEADER in markdown
    assert '- ["quote"](https://readwise.io/open/954480)' in markdown
    frontmatter, _body = _extract_frontmatter(markdown)
    assert frontmatter["People"] == ["[[@georgiedorothea]]"]
    assert "Journal:" not in markdown
    assert "[[Tweets from @georgiedorothea]]" not in markdown
    assert "_People/" not in markdown


def test_tweet_highlight_creates_handle_page_with_bookmarked_section():
    clear_book_cache()
    mock_dbx, uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(_tweet_highlight(), now=now)

    assert result["success"] is True
    assert result["action"] == "replaced"
    journal = store[JOURNAL_NOV_PATH]
    assert (
        '- [[Tweets from @georgiedorothea]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    ) in journal
    page = store[TWEET_PAGE_PATH]
    assert 'title: "Tweets from @georgiedorothea"' in page
    assert "# Tweets from @georgiedorothea" in page
    assert BOOKMARKED_TWEETS_HEADER in page
    assert (
        '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
        in page
    )
    assert "- [[Tweets from @georgiedorothea]]:" not in page
    frontmatter, _body = _extract_frontmatter(page)
    assert frontmatter["People"] == ["[[@georgiedorothea]]"]
    assert not any("_People/" in item["path"] for item in uploaded)
    assert any(item["path"] == TWEET_PAGE_PATH for item in uploaded)


def test_second_tweet_for_handle_appends_and_same_open_id_is_skipped():
    clear_book_cache()
    mock_dbx, _uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        first = append_readwise_buffet(_tweet_highlight(), now=now)
        assert first["success"] is True
        assert store[TWEET_PAGE_PATH].count("readwise.io/open/") == 1

        second = append_readwise_buffet(
            _tweet_highlight(id=954481, text="Another banger"), now=now
        )
        assert second["success"] is True
        page = store[TWEET_PAGE_PATH]
        assert "Most Amazing Highlight Ever" in page
        assert "Another banger" in page
        assert page.index("Most Amazing Highlight Ever") < page.index("Another banger")
        assert page.count("https://readwise.io/open/954480") == 1
        assert page.count("https://readwise.io/open/954481") == 1

        before = page
        third = append_readwise_buffet(_tweet_highlight(), now=now)
        assert third["success"] is True
        assert store[TWEET_PAGE_PATH] == before
        assert store[TWEET_PAGE_PATH].count("https://readwise.io/open/954480") == 1


def test_missing_bookmarked_tweets_heading_is_created_body_kept():
    clear_book_cache()
    existing = """---
title: "Tweets from @georgiedorothea"
---

# Tweets from @georgiedorothea

## People
- [[Georgie Dorothea]]

A paragraph that must survive.
"""
    mock_dbx, _uploaded, store = _mock_vault_dbx({
        JOURNAL_NOV_PATH: SAMPLE_JOURNAL,
        TWEET_PAGE_PATH: existing,
    })
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(_tweet_highlight(), now=now)

    assert result["success"] is True
    page = store[TWEET_PAGE_PATH]
    assert "## People" in page
    assert "- [[Georgie Dorothea]]" in page
    assert "A paragraph that must survive." in page
    assert BOOKMARKED_TWEETS_HEADER in page
    assert page.index("A paragraph that must survive.") < page.index(BOOKMARKED_TWEETS_HEADER)
    assert '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)' in page
    frontmatter, body = _extract_frontmatter(page)
    assert "[[@georgiedorothea]]" in frontmatter["People"]
    assert "- [[Georgie Dorothea]]" in body
    assert "A paragraph that must survive." in body


def test_reader_document_created_does_not_write_tweet_page():
    """reader.*_document.created must not create Tweets from @handle pages."""
    with patch(
        "services.obsidian.add_readwise_buffet._append_tweet_pages_after_journal"
    ) as mock_tweets, patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value={
            "success": True,
            "action": "created",
            "error": None,
            "file_path": "_Knowledge-Hub/Tweets From Georgie.md",
        },
    ), patch(
        "services.obsidian.add_readwise_buffet.create_bookmark",
        return_value={"success": True, "bookmark_id": "rd-1", "error": None},
    ):
        result = append_readwise_buffet(
            _reader_payload(
                title="Tweets From Georgie Dorothea",
                author="@georgiedorothea on Twitter",
                category="tweets",
                source="twitter",
                source_url="https://twitter.com/georgiedorothea",
            )
        )

    assert result["success"] is True
    assert result["action"] == "created"
    mock_tweets.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "A long essay", "author": "The Verge", "category": "articles", "source": "reader"},
        {"title": "Weekly notes", "author": "Casey Newton", "category": "articles"},
        {"title": "Invoice.pdf", "author": None, "category": "pdfs"},
    ],
)
def test_non_tweet_highlights_never_touch_bookmarked_tweets_or_hub(overrides):
    """Articles and other non-book highlights stay journal-only."""
    clear_book_cache()
    payload = _highlight_payload(**overrides)
    book = _resolve_highlight_book(payload)
    assert _is_tweet_book(book) is False
    assert _tweet_handle(book) is None
    assert _format_tweet_page_bullet(payload, book) is None

    mock_dbx, uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx), patch(
        "services.obsidian.add_readwise_buffet._resolve_knowledge_hub_folder"
    ) as mock_hub, patch(
        "services.obsidian.add_readwise_buffet._append_tweet_page"
    ) as mock_page:
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert result["action"] == "replaced"
    mock_hub.assert_not_called()
    mock_page.assert_not_called()
    assert TWEET_PAGE_PATH not in store
    assert not any("Tweets from" in item["path"] for item in uploaded)
    assert not any(BOOKMARKED_TWEETS_HEADER in item["content"] for item in uploaded)
    assert "### Content Buffet:" in store[JOURNAL_NOV_PATH]
    assert "Most Amazing Highlight Ever" in store[JOURNAL_NOV_PATH]


def test_non_tweet_highlight_does_not_create_tweet_page_or_section():
    clear_book_cache()
    mock_dbx, uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(
            _highlight_payload(title="Deep Work", author="Cal Newport", category="books"),
            now=now,
        )

    assert result["success"] is True
    assert result["action"] == "replaced"
    assert TWEET_PAGE_PATH not in store
    assert not any("Tweets from" in item["path"] for item in uploaded)
    assert not any(BOOKMARKED_TWEETS_HEADER in item["content"] for item in uploaded)
    assert "[[Deep Work" in store[JOURNAL_NOV_PATH]
    assert "Tweets from" not in store[JOURNAL_NOV_PATH]


def test_journal_buffet_line_still_uses_tweets_from_handle_wikilink():
    clear_book_cache()
    mock_dbx, _uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        append_readwise_buffet(_tweet_highlight(note="clip this"), now=now)

    journal_line = [
        ln for ln in store[JOURNAL_NOV_PATH].splitlines() if "Most Amazing" in ln
    ][0]
    assert journal_line == (
        '- [[Tweets from @georgiedorothea]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480) — clip this'
    )
    page_line = [
        ln for ln in store[TWEET_PAGE_PATH].splitlines() if "Most Amazing" in ln
    ][0]
    assert page_line == (
        '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480) — clip this'
    )


def test_missing_journal_does_not_create_tweet_page():
    clear_book_cache()
    mock_dbx, uploaded, store = _mock_vault_dbx()
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(_tweet_highlight(), now=now)

    assert result["action"] == "skipped_missing_journal"
    mock_dbx.files_upload.assert_not_called()
    assert uploaded == []
    assert TWEET_PAGE_PATH not in store


def test_tweet_page_failure_does_not_undo_journal_write():
    clear_book_cache()
    mock_dbx, uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with patch(
        "services.obsidian.add_readwise_buffet._get_dropbox_client",
        return_value=mock_dbx,
    ), patch(
        "services.obsidian.add_readwise_buffet._find_folder_by_suffix",
        side_effect=_folder_by_suffix,
    ), patch(
        "services.obsidian.add_readwise_buffet._resolve_knowledge_hub_folder",
        side_effect=RuntimeError("kh down"),
    ):
        result = append_readwise_buffet(_tweet_highlight(), now=now)

    assert result["success"] is True
    assert result["action"] == "replaced"
    assert result["error"] is None
    assert "[[Tweets from @georgiedorothea]]:" in store[JOURNAL_NOV_PATH]
    assert TWEET_PAGE_PATH not in store
    assert all(item["path"] == JOURNAL_NOV_PATH for item in uploaded)


def test_tweet_without_handle_does_not_create_handle_page():
    clear_book_cache()
    mock_dbx, uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(
            _highlight_payload(
                title="Tweets From Klaas",
                author="Klaas",
                category="tweets",
            ),
            now=now,
        )

    assert result["success"] is True
    assert "[[Tweets From Klaas]]:" in store[JOURNAL_NOV_PATH]
    assert not any("Tweets from @" in item["path"] for item in uploaded)
    assert TWEET_PAGE_PATH not in store


def test_non_tweet_does_not_write_bookmarked_section_on_book_note():
    clear_book_cache()
    book_path = f"{KH_FOLDER}/Deep Work.md"
    existing = """---
title: Deep Work
---

# Deep Work

Body stays.
"""
    mock_dbx, uploaded, store = _mock_vault_dbx({
        JOURNAL_NOV_PATH: SAMPLE_JOURNAL,
        book_path: existing,
    })
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        append_readwise_buffet(
            _highlight_payload(title="Deep Work", author="Cal Newport", category="books"),
            now=now,
        )

    assert store[book_path] == existing
    assert BOOKMARKED_TWEETS_HEADER not in store[book_path]
    assert not any(item["path"] == book_path for item in uploaded)


def test_existing_tweet_page_found_case_insensitive_filename():
    clear_book_cache()
    alt_path = f"{KH_FOLDER}/tweets from @georgiedorothea.md"
    existing = """# Tweets from @georgiedorothea

Some intro.

### Bookmarked Tweets
- ["old"](https://readwise.io/open/1)
"""
    mock_dbx, uploaded, store = _mock_vault_dbx({
        JOURNAL_NOV_PATH: SAMPLE_JOURNAL,
        alt_path: existing,
    })
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(_tweet_highlight(), now=now)

    assert result["success"] is True
    assert alt_path in store
    assert TWEET_PAGE_PATH not in store
    page = store[alt_path]
    assert "Some intro." in page
    assert "old" in page
    assert '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)' in page
    frontmatter, body = _extract_frontmatter(page)
    assert frontmatter["People"] == ["[[@georgiedorothea]]"]
    assert "Some intro." in body
    assert any(item["path"] == alt_path for item in uploaded)


# Aug 23, 2026 journal — empty-alt twimg images after t.co, stripped by hand.
LIVE_INTERNET_HOF_TEXT = (
    "New York is now the #1 market for tech talent, dethroning "
    "San Francisco's 13-year reign (via: CNBC) https://t.co/L3eHRbgQyG "
    "![](https://pbs.twimg.com/media/HQYkftsXEAAsKdm.jpg) "
    "![](https://pbs.twimg.com/media/HQYkgifXEAAfKbH.jpg)"
)
LIVE_FLOWER_ALICEE_TEXT = (
    "...but then when https://t.co/p3GyToJO6M "
    "![](https://pbs.twimg.com/media/HQWvI2ZbIAA4WBd.jpg) "
    "![](https://pbs.twimg.com/media/HQWvI2ZaoAAdqfb.jpg)"
)


def test_bookmarked_tweets_live_interneth0f_keeps_tco_and_open_id():
    """Today's journal: two space-separated empty-alt ![](pbs.twimg.com/media/...) after t.co."""
    clear_book_cache()
    page_path = f"{KH_FOLDER}/Tweets from @InternetH0F.md"
    mock_dbx, _uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(
            _highlight_payload(
                id=1047167879,
                text=LIVE_INTERNET_HOF_TEXT,
                title="Tweets From InternetH0F",
                author="@InternetH0F on Twitter",
                category="tweets",
                source="twitter",
                source_url="https://twitter.com/InternetH0F",
            ),
            now=now,
        )

    assert result["success"] is True
    page = store[page_path]
    frontmatter, _body = _extract_frontmatter(page)
    assert frontmatter["People"] == ["[[@InternetH0F]]"]
    assert "[[@InternetH0F]]" in page
    assert "[[@interneth0f]]" not in page
    line = [ln for ln in page.splitlines() if "New York is now" in ln][0]
    assert line == (
        '- ["New York is now the #1 market for tech talent, dethroning '
        "San Francisco's 13-year reign (via: CNBC) https://t.co/L3eHRbgQyG"
        '"](https://readwise.io/open/1047167879)'
    )
    assert line.startswith("- [\"")
    assert "https://t.co/L3eHRbgQyG" in line
    assert "https://readwise.io/open/1047167879" in line
    assert "![]" not in page
    assert "pbs.twimg.com" not in page
    assert "HQYkftsXEAAsKdm" not in page
    assert "HQYkgifXEAAfKbH" not in page


def test_bookmarked_tweets_live_flower_alicee_keeps_tco_and_open_id():
    """Today's journal: two space-separated empty-alt ![](pbs.twimg.com/media/...) after t.co."""
    clear_book_cache()
    page_path = f"{KH_FOLDER}/Tweets from @flower_alicee.md"
    mock_dbx, _uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(
            _highlight_payload(
                id=1047167880,
                text=LIVE_FLOWER_ALICEE_TEXT,
                title="Tweets From flower_alicee",
                author="@flower_alicee on Twitter",
                category="tweets",
                source="twitter",
                source_url="https://twitter.com/flower_alicee",
            ),
            now=now,
        )

    assert result["success"] is True
    page = store[page_path]
    frontmatter, _body = _extract_frontmatter(page)
    assert frontmatter["People"] == ["[[@flower_alicee]]"]
    assert "[[@flower_alicee]]" in page
    assert "[[@Flower_alicee]]" not in page
    line = [ln for ln in page.splitlines() if "but then when" in ln][0]
    assert line == (
        '- ["...but then when https://t.co/p3GyToJO6M"]'
        "(https://readwise.io/open/1047167880)"
    )
    assert "https://t.co/p3GyToJO6M" in line
    assert "https://readwise.io/open/1047167880" in line
    assert "![]" not in page
    assert "pbs.twimg.com" not in page


def test_tweet_page_write_strips_images_keeps_quote_and_open_id():
    clear_book_cache()
    mock_dbx, _uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(
            _tweet_highlight(
                text=(
                    "Quote text https://t.co/abc "
                    "![](https://pbs.twimg.com/media/foo.jpg) "
                    "<img src='https://pbs.twimg.com/media/bar.jpg'>"
                )
            ),
            now=now,
        )

    assert result["success"] is True
    page = store[TWEET_PAGE_PATH]
    line = [ln for ln in page.splitlines() if "Quote text" in ln][0]
    assert line == '- ["Quote text https://t.co/abc"](https://readwise.io/open/954480)'
    assert "![]" not in page
    assert "<img" not in page
    assert "pbs.twimg.com" not in page
    assert "https://readwise.io/open/954480" in page


def test_tweet_page_image_only_does_not_create_page():
    clear_book_cache()
    mock_dbx, uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(
            _tweet_highlight(text="![](https://pbs.twimg.com/media/foo.jpg)"),
            now=now,
        )

    assert result["success"] is True
    assert TWEET_PAGE_PATH not in store
    assert not any(item["path"] == TWEET_PAGE_PATH for item in uploaded)
    assert not any(BOOKMARKED_TWEETS_HEADER in item["content"] for item in uploaded)


def test_tweet_people_wikilink_keeps_at_and_readwise_casing():
    assert _tweet_people_wikilink("InternetH0F") == "[[@InternetH0F]]"
    assert _tweet_people_wikilink("flower_alicee") == "[[@flower_alicee]]"
    assert _tweet_people_wikilink("flower_alicee") != "[[@Flower_alicee]]"
    assert "@" in _tweet_people_wikilink("InternetH0F")
    assert _people_entry_has_handle("[[@InternetH0F]]", "InternetH0F")
    assert _people_entry_has_handle("@InternetH0F", "interneth0f")
    assert _people_entry_has_handle("InternetH0F", "interneth0f")
    assert not _people_entry_has_handle("[[Georgie Dorothea]]", "georgiedorothea")


def test_new_tweet_page_yaml_people_is_share_link_wikilink_list():
    markdown = _new_tweet_page_markdown(
        "Tweets from @InternetH0F",
        '- ["quote"](https://readwise.io/open/1)',
        "InternetH0F",
    )
    frontmatter, _body = _extract_frontmatter(markdown)
    assert frontmatter["People"] == ["[[@InternetH0F]]"]
    assert '  - "[[@InternetH0F]]"' in markdown


def test_existing_tweet_page_missing_people_gets_link_on_next_bookmark():
    clear_book_cache()
    existing = """---
title: "Tweets from @georgiedorothea"
---

# Tweets from @georgiedorothea

Kept body.

### Bookmarked Tweets
- ["old"](https://readwise.io/open/1)
"""
    mock_dbx, uploaded, store = _mock_vault_dbx({
        JOURNAL_NOV_PATH: SAMPLE_JOURNAL,
        TWEET_PAGE_PATH: existing,
    })
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(_tweet_highlight(), now=now)

    assert result["success"] is True
    page = store[TWEET_PAGE_PATH]
    frontmatter, body = _extract_frontmatter(page)
    assert "[[@georgiedorothea]]" in frontmatter["People"]
    assert page.count("[[@georgiedorothea]]") == 1
    assert "Kept body." in body
    assert "old" in body
    assert '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)' in page
    assert not any("_People/" in item["path"] for item in uploaded)


def test_existing_tweet_page_people_not_duplicated():
    clear_book_cache()
    existing = """---
title: "Tweets from @InternetH0F"
People:
  - "[[Someone Else]]"
  - "[[@InternetH0F]]"
---

# Tweets from @InternetH0F

Body stays.

### Bookmarked Tweets
- ["old"](https://readwise.io/open/1)
"""
    page_path = f"{KH_FOLDER}/Tweets from @InternetH0F.md"
    mock_dbx, _uploaded, store = _mock_vault_dbx({
        JOURNAL_NOV_PATH: SAMPLE_JOURNAL,
        page_path: existing,
    })
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(
            _highlight_payload(
                id=1047167879,
                text=LIVE_INTERNET_HOF_TEXT,
                title="Tweets From InternetH0F",
                author="@InternetH0F on Twitter",
                category="tweets",
                source="twitter",
                source_url="https://twitter.com/InternetH0F",
            ),
            now=now,
        )

    assert result["success"] is True
    page = store[page_path]
    frontmatter, body = _extract_frontmatter(page)
    assert frontmatter["People"] == ["[[Someone Else]]", "[[@InternetH0F]]"]
    assert page.count("[[@InternetH0F]]") == 1
    assert "Body stays." in body
    assert "old" in body


@pytest.mark.parametrize(
    "people_yaml",
    [
        '  - "[[@InternetH0F]]"',
        "  - '@InternetH0F'",
        "  - InternetH0F",
        "  - interneth0f",
    ],
)
def test_existing_people_handle_variants_are_not_duplicated(people_yaml):
    content = f"""---
title: "Tweets from @InternetH0F"
People:
{people_yaml}
---

# Tweets from @InternetH0F
"""
    updated = _ensure_tweet_page_people(content, "InternetH0F")
    assert updated == content


def test_duplicate_bookmark_still_backfills_missing_people():
    clear_book_cache()
    existing = """---
title: "Tweets from @georgiedorothea"
---

# Tweets from @georgiedorothea

Kept.

### Bookmarked Tweets
- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)
"""
    mock_dbx, uploaded, store = _mock_vault_dbx({
        JOURNAL_NOV_PATH: SAMPLE_JOURNAL,
        TWEET_PAGE_PATH: existing,
    })
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(_tweet_highlight(), now=now)

    assert result["success"] is True
    page = store[TWEET_PAGE_PATH]
    frontmatter, body = _extract_frontmatter(page)
    assert frontmatter["People"] == ["[[@georgiedorothea]]"]
    assert page.count("https://readwise.io/open/954480") == 1
    assert "Kept." in body
    assert any(item["path"] == TWEET_PAGE_PATH for item in uploaded)

# ---------------------------------------------------------------------------
# Title by Author Knowledge Hub book page
# ---------------------------------------------------------------------------

BOOK_PAGE_PATH = f"{KH_FOLDER}/Deep Work by Cal Newport.md"


def _book_highlight(**overrides):
    data = {
        "title": "Deep Work",
        "author": "Cal Newport",
        "category": "books",
        "source": "kindle",
        "source_url": "https://www.amazon.com/dp/example",
    }
    data.update(overrides)
    return _highlight_payload(**data)


def test_is_book_highlight_requires_books_category_and_not_tweet():
    clear_book_cache()
    book = _resolve_highlight_book(_book_highlight())
    assert _is_book_highlight(book) is True
    article = _resolve_highlight_book(
        _highlight_payload(title="A long essay", author="The Verge", category="articles")
    )
    assert _is_book_highlight(article) is False
    tweet = _resolve_highlight_book(_tweet_highlight())
    assert _is_book_highlight(tweet) is False
    tweet_books = _resolve_highlight_book(
        _highlight_payload(
            title="Tweets From Georgie Dorothea",
            author="@georgiedorothea on Twitter",
            category="books",
            source="twitter",
            source_url="https://twitter.com/georgiedorothea",
        )
    )
    assert _is_tweet_book(tweet_books) is True
    assert _is_book_highlight(tweet_books) is False


def test_format_book_page_bullet_omits_wikilink_and_keeps_note():
    clear_book_cache()
    payload = _book_highlight(note="worth revisiting")
    book = _resolve_highlight_book(payload)
    journal = format_readwise_bullet(payload)
    page = _format_book_page_bullet(payload, book)
    assert journal == (
        '- [[Deep Work by Cal Newport]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480) — worth revisiting'
    )
    assert page == (
        '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480) — worth revisiting'
    )
    assert "[[Deep Work by Cal Newport]]" not in page
    assert _format_book_page_bullet(_tweet_highlight(), _resolve_highlight_book(_tweet_highlight())) is None


def test_insert_book_highlights_appends_missing_heading():
    content = """---
title: "Deep Work by Cal Newport"
---

# Deep Work by Cal Newport

## Notes
Kept body text.
"""
    bullet = '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    updated, action = insert_book_highlights_bullet(
        content, bullet, keys=["https://readwise.io/open/954480"]
    )
    assert action == "inserted"
    assert "## Notes" in updated
    assert "Kept body text." in updated
    assert BOOK_HIGHLIGHTS_HEADER in updated
    assert updated.index("Kept body text.") < updated.index(BOOK_HIGHLIGHTS_HEADER)
    assert bullet in updated
    assert "[[Deep Work by Cal Newport]]:" not in updated


def test_insert_book_highlights_dedups_open_url_only():
    content = f"""# Deep Work by Cal Newport

{BOOK_HIGHLIGHTS_HEADER}
- ["Old quote"](https://readwise.io/open/111)
"""
    same_id = '- ["Same id new wording"](https://readwise.io/open/111)'
    updated, action = insert_book_highlights_bullet(
        content, same_id, keys=["https://readwise.io/open/111"]
    )
    assert action == "skipped"
    assert updated == content

    other = '- ["New quote"](https://readwise.io/open/222)'
    updated, action = insert_book_highlights_bullet(
        content, other, keys=["https://readwise.io/open/222"]
    )
    assert action == "inserted"
    assert "Old quote" in updated
    assert "New quote" in updated


def test_new_book_page_markdown_has_author_people_and_metadata():
    markdown = _new_book_page_markdown(
        "Deep Work by Cal Newport",
        '- ["quote"](https://readwise.io/open/954480)',
        {
            "author": "[[Cal Newport]]",
            "URL": "https://www.amazon.com/dp/example",
            "readwise_id": "8237",
            "category": "books",
            "source": "kindle",
        },
        ["[[Cal Newport]]"],
    )
    assert markdown.startswith("---\ntitle: \"Deep Work by Cal Newport\"\n")
    assert 'author: "[[Cal Newport]]"' in markdown
    assert '  - "[[Cal Newport]]"' in markdown
    frontmatter, _body = _extract_frontmatter(markdown)
    assert frontmatter["author"] == "[[Cal Newport]]"
    assert frontmatter["People"] == ["[[Cal Newport]]"]
    assert frontmatter["URL"] == "https://www.amazon.com/dp/example"
    assert str(frontmatter["readwise_id"]) == "8237"
    assert frontmatter["category"] == "books"
    assert frontmatter["source"] == "kindle"
    assert BOOK_HIGHLIGHTS_HEADER in markdown
    assert '- ["quote"](https://readwise.io/open/954480)' in markdown
    assert "[[Deep Work by Cal Newport]]:" not in markdown
    assert "[[@" not in markdown


def test_book_highlight_creates_title_by_author_page():
    clear_book_cache()
    mock_dbx, uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(_book_highlight(), now=now)

    assert result["success"] is True
    assert result["action"] == "replaced"
    journal = store[JOURNAL_NOV_PATH]
    assert (
        '- [[Deep Work by Cal Newport]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    ) in journal
    page = store[BOOK_PAGE_PATH]
    frontmatter, body = _extract_frontmatter(page)
    assert frontmatter["author"] == "[[Cal Newport]]"
    assert frontmatter["People"] == ["[[Cal Newport]]"]
    assert frontmatter["URL"] == "https://www.amazon.com/dp/example"
    assert str(frontmatter["readwise_id"]) == "8237"
    assert frontmatter["category"] == "books"
    assert frontmatter["source"] == "kindle"
    assert BOOK_HIGHLIGHTS_HEADER in page
    assert (
        '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
        in page
    )
    assert "- [[Deep Work by Cal Newport]]:" not in page
    assert "[[@Cal" not in page
    assert BOOKMARKED_TWEETS_HEADER not in page
    assert any(item["path"] == BOOK_PAGE_PATH for item in uploaded)


def test_second_book_highlight_appends_and_same_open_id_is_skipped():
    clear_book_cache()
    mock_dbx, _uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        first = append_readwise_buffet(_book_highlight(), now=now)
        assert first["success"] is True
        assert store[BOOK_PAGE_PATH].count("readwise.io/open/") == 1

        second = append_readwise_buffet(
            _book_highlight(id=954481, text="Another deep sentence"), now=now
        )
        assert second["success"] is True
        page = store[BOOK_PAGE_PATH]
        assert "Most Amazing Highlight Ever" in page
        assert "Another deep sentence" in page
        assert page.index("Most Amazing Highlight Ever") < page.index("Another deep sentence")
        assert page.count("https://readwise.io/open/954480") == 1
        assert page.count("https://readwise.io/open/954481") == 1

        before = page
        third = append_readwise_buffet(_book_highlight(), now=now)
        assert third["success"] is True
        assert store[BOOK_PAGE_PATH] == before
        assert store[BOOK_PAGE_PATH].count("https://readwise.io/open/954480") == 1


def test_two_authors_become_two_distinct_people_and_author_links():
    clear_book_cache()
    mock_dbx, _uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    payload = _book_highlight(
        title="Increasing Returns",
        author="Alice Smith, Alice Smith, Bob Jones",
    )
    stem = reader_knowledge_hub_note_stem("Increasing Returns", "Alice Smith, Alice Smith, Bob Jones")
    page_path = f"{KH_FOLDER}/{stem}.md"
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    page = store[page_path]
    frontmatter, _body = _extract_frontmatter(page)
    assert frontmatter["author"] == "[[Alice Smith]], [[Bob Jones]]"
    assert frontmatter["People"] == ["[[Alice Smith]]", "[[Bob Jones]]"]
    assert page.count("[[Alice Smith]]") == 2  # author key + People
    assert page.count("[[Bob Jones]]") == 2
    assert (
        '- [[Increasing Returns by Alice Smith, Alice Smith, Bob Jones]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    ) in store[JOURNAL_NOV_PATH]


def test_tweet_highlight_does_not_write_book_page():
    clear_book_cache()
    mock_dbx, uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(_tweet_highlight(), now=now)

    assert result["success"] is True
    assert BOOK_PAGE_PATH not in store
    assert not any("Deep Work" in item["path"] for item in uploaded)
    assert not any(BOOK_HIGHLIGHTS_HEADER in item["content"] for item in uploaded)
    assert BOOK_HIGHLIGHTS_HEADER not in store[TWEET_PAGE_PATH]
    journal_line = [
        ln for ln in store[JOURNAL_NOV_PATH].splitlines() if "Most Amazing" in ln
    ][0]
    assert journal_line == (
        '- [[Tweets from @georgiedorothea]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480)'
    )


def test_article_highlight_does_not_write_book_highlights():
    clear_book_cache()
    mock_dbx, uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx), patch(
        "services.obsidian.add_readwise_buffet._append_book_page"
    ) as mock_page:
        result = append_readwise_buffet(
            _highlight_payload(
                title="A long essay",
                author="The Verge",
                category="articles",
            ),
            now=now,
        )

    assert result["success"] is True
    mock_page.assert_not_called()
    assert not any(BOOK_HIGHLIGHTS_HEADER in item["content"] for item in uploaded)
    assert "[[A long essay by The Verge]]:" in store[JOURNAL_NOV_PATH]


def test_journal_buffet_line_unchanged_for_book_highlight():
    clear_book_cache()
    mock_dbx, _uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        append_readwise_buffet(_book_highlight(note="clip this"), now=now)

    journal_line = [
        ln for ln in store[JOURNAL_NOV_PATH].splitlines() if "Most Amazing" in ln
    ][0]
    assert journal_line == (
        '- [[Deep Work by Cal Newport]]: '
        '["Most Amazing Highlight Ever"](https://readwise.io/open/954480) — clip this'
    )
    page_line = [
        ln for ln in store[BOOK_PAGE_PATH].splitlines() if "Most Amazing" in ln
    ][0]
    assert page_line == (
        '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480) — clip this'
    )


def test_reader_document_created_does_not_write_book_page():
    with patch(
        "services.obsidian.add_readwise_buffet._append_book_pages_after_journal"
    ) as mock_books, patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value={
            "success": True,
            "action": "created",
            "error": None,
            "file_path": "_Knowledge-Hub/Deep Work by Cal Newport.md",
        },
    ), patch(
        "services.obsidian.add_readwise_buffet.create_bookmark",
        return_value={"success": True, "bookmark_id": "rd-1", "error": None},
    ):
        result = append_readwise_buffet(
            _reader_payload(
                title="Deep Work",
                author="Cal Newport",
                category="books",
                source="kindle",
                source_url="https://www.amazon.com/dp/example",
            )
        )

    assert result["success"] is True
    assert result["action"] == "created"
    mock_books.assert_not_called()


def test_missing_book_highlights_heading_is_created_body_kept():
    clear_book_cache()
    existing = """---
title: "Deep Work by Cal Newport"
author: "Someone Else"
People:
  - "[[Existing Person]]"
---

# Deep Work by Cal Newport

A paragraph that must survive.
"""
    mock_dbx, _uploaded, store = _mock_vault_dbx({
        JOURNAL_NOV_PATH: SAMPLE_JOURNAL,
        BOOK_PAGE_PATH: existing,
    })
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(_book_highlight(), now=now)

    assert result["success"] is True
    page = store[BOOK_PAGE_PATH]
    frontmatter, body = _extract_frontmatter(page)
    assert frontmatter["author"] == "Someone Else"
    assert "[[Cal Newport]]" in frontmatter["People"]
    assert "[[Existing Person]]" in frontmatter["People"]
    assert "A paragraph that must survive." in body
    assert BOOK_HIGHLIGHTS_HEADER in page
    assert page.index("A paragraph that must survive.") < page.index(BOOK_HIGHLIGHTS_HEADER)
    assert '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)' in page


def test_existing_book_people_not_duplicated():
    clear_book_cache()
    existing = """---
title: "Deep Work by Cal Newport"
author: "[[Cal Newport]]"
People:
  - "[[Cal Newport]]"
  - "[[Someone Else]]"
---

# Deep Work by Cal Newport

### Book highlights
- ["old"](https://readwise.io/open/1)
"""
    mock_dbx, _uploaded, store = _mock_vault_dbx({
        JOURNAL_NOV_PATH: SAMPLE_JOURNAL,
        BOOK_PAGE_PATH: existing,
    })
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(_book_highlight(), now=now)

    assert result["success"] is True
    page = store[BOOK_PAGE_PATH]
    frontmatter, body = _extract_frontmatter(page)
    assert frontmatter["People"] == ["[[Cal Newport]]", "[[Someone Else]]"]
    assert page.count("[[Cal Newport]]") == 2  # author + People
    assert "old" in body


def test_book_page_failure_does_not_undo_journal_write():
    clear_book_cache()
    mock_dbx, uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with patch(
        "services.obsidian.add_readwise_buffet._get_dropbox_client",
        return_value=mock_dbx,
    ), patch(
        "services.obsidian.add_readwise_buffet._find_folder_by_suffix",
        side_effect=_folder_by_suffix,
    ), patch(
        "services.obsidian.add_readwise_buffet._resolve_knowledge_hub_folder",
        side_effect=RuntimeError("kh down"),
    ):
        result = append_readwise_buffet(_book_highlight(), now=now)

    assert result["success"] is True
    assert result["action"] == "replaced"
    assert result["error"] is None
    assert "[[Deep Work by Cal Newport]]:" in store[JOURNAL_NOV_PATH]
    assert BOOK_PAGE_PATH not in store
    assert all(item["path"] == JOURNAL_NOV_PATH for item in uploaded)


def test_title_only_book_page_when_author_missing():
    clear_book_cache()
    mock_dbx, _uploaded, store = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(
            _book_highlight(author=None, title="Deep Work"),
            now=now,
        )

    assert result["success"] is True
    page_path = f"{KH_FOLDER}/Deep Work.md"
    page = store[page_path]
    frontmatter, _body = _extract_frontmatter(page)
    assert "author" not in frontmatter or not frontmatter.get("author")
    assert not frontmatter.get("People")
    assert BOOK_HIGHLIGHTS_HEADER in page
    assert '- ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)' in page
    assert "[[Deep Work]]:" in store[JOURNAL_NOV_PATH]
