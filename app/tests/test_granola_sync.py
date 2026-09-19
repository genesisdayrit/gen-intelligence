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
    format_granola_block,
    format_granola_bullet,
    granola_html_comment,
    insert_transcript_notes_bullet,
    note_effective_date,
    note_summary_body,
    seed_updated_after,
    sync_granola_notes,
    write_notes_by_journal,
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

JOURNAL_WITH_H3_IN_SUMMARY = """---
date: 2026-09-06
---

# Sep 6, 2026

### Transcript Notes

#### [Church reflection](https://notes.granola.ai/d/church)
<!-- granola:not_church0000001 -->

### Church and Spiritual Practice

The body of the church reflection that must stay with this note.
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


def _run_sync(
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
    assert second["selected"] == 1
    assert second["inserted"] == 0
    assert second["skipped"] == 1


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
    content = uploaded[0]["content"]
    assert (
        "#### [Quarterly yoghurt budget review]"
        "(https://notes.granola.ai/d/f3e45e0f-24cc-480b-9a6c-8b1f5e3d7a2c)"
    ) in content
    assert "<!-- granola:not_1d3tmYTlCICgjy -->" in content
    assert DEFAULT_SUMMARY_MARKDOWN in content
    assert "- [Quarterly yoghurt budget review]" not in content


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
    note = _note(
        note_id="not_alreadyThere1",
        title="Older meeting",
        web_url="https://notes.granola.ai/d/old",
    )
    result, uploaded, _, _, _ = _run_sync(
        [_list_page([note])],
        contents_by_path={sep_path: JOURNAL_WITH_SUMMARY_BLOCK},
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
    assert content.rstrip().endswith("- Spent **$100,000** on yoghurt")
    assert "<!-- granola:not_1d3tmYTlCICgjy -->" in content


def test_existing_mid_note_section_is_not_moved():
    updated, action = insert_transcript_notes_bullet(
        JOURNAL_WITH_TRANSCRIPT_NOTES,
        format_granola_block(_note(summary_markdown=DEFAULT_SUMMARY_MARKDOWN)),
        ["granola:not_1d3tmYTlCICgjy", "not_1d3tmYTlCICgjy"],
    )
    assert action == "inserted"
    heading_idx = updated.index(TRANSCRIPT_NOTES_HEADER)
    planning_idx = updated.index("### Content Planning")
    assert heading_idx < planning_idx
    assert "<!-- granola:not_1d3tmYTlCICgjy -->" in updated
    assert updated.count(TRANSCRIPT_NOTES_HEADER) == 1
    assert "\n---\n" in updated[heading_idx:planning_idx]


def test_h3_in_existing_summary_does_not_split_or_orphan_note():
    """Sep 6 failure: ``###`` inside summary_markdown must not end the section.

    Inserting another note must append after the full first note (title,
    HTML comment, ``### Foo`` heading, and body) — not between the comment
    and ``### Foo``, and must not orphan the first note's body below.
    """
    later = format_granola_block(
        _note(
            note_id="not_later00000002",
            title="Later meeting",
            web_url="https://notes.granola.ai/d/later",
            summary_markdown="Later summary",
        )
    )
    updated, action = insert_transcript_notes_bullet(
        JOURNAL_WITH_H3_IN_SUMMARY,
        later,
        ["granola:not_later00000002", "not_later00000002"],
    )
    assert action == "inserted"
    section = updated[updated.index(TRANSCRIPT_NOTES_HEADER) :]
    church_title = "#### [Church reflection](https://notes.granola.ai/d/church)"
    church_marker = "<!-- granola:not_church0000001 -->"
    church_h3 = "### Church and Spiritual Practice"
    church_body = "The body of the church reflection that must stay with this note."
    later_title = "#### [Later meeting](https://notes.granola.ai/d/later)"
    later_marker = "<!-- granola:not_later00000002 -->"
    assert section.index(church_title) < section.index(church_marker)
    assert section.index(church_marker) < section.index(church_h3)
    assert section.index(church_h3) < section.index(church_body)
    assert section.index(church_body) < section.index("\n---\n")
    assert section.index(church_body) < section.index(later_title)
    assert section.index(later_title) < section.index(later_marker)
    assert "Later summary" in section[section.index(later_marker) :]
    assert church_body not in section[section.index(later_title) :]
    skipped, skip_action = insert_transcript_notes_bullet(
        updated,
        later,
        ["granola:not_later00000002", "not_later00000002"],
    )
    assert skip_action == "skipped"
    assert skipped == updated


def test_insert_after_note_whose_summary_starts_with_h3_appends_at_end():
    """Same split/orphan case starting from an empty Transcript Notes write."""
    church = format_granola_block(
        _note(
            note_id="not_church0000001",
            title="Church reflection",
            web_url="https://notes.granola.ai/d/church",
            summary_markdown=(
                "### Church and Spiritual Practice\n"
                "\n"
                "The body of the church reflection that must stay with this note."
            ),
        )
    )
    journal, action = insert_transcript_notes_bullet(
        SAMPLE_JOURNAL,
        church,
        ["granola:not_church0000001", "not_church0000001"],
    )
    assert action == "inserted"

    later = format_granola_block(
        _note(
            note_id="not_later00000002",
            title="Later meeting",
            web_url="https://notes.granola.ai/d/later",
            summary_markdown="# Hash heading in later note\n\nLater summary",
        )
    )
    updated, action = insert_transcript_notes_bullet(
        journal,
        later,
        ["granola:not_later00000002", "not_later00000002"],
    )
    assert action == "inserted"
    section = updated[updated.index(TRANSCRIPT_NOTES_HEADER) :]
    church_body = "The body of the church reflection that must stay with this note."
    later_title = "#### [Later meeting](https://notes.granola.ai/d/later)"
    assert "### Church and Spiritual Practice" in section
    assert "# Hash heading in later note" in section
    assert section.index(church_body) < section.index(later_title)
    assert church_body not in section[section.index(later_title) :]
    planning_or_buffet = updated.index("### Content Buffet:")
    assert updated.index(TRANSCRIPT_NOTES_HEADER) > planning_or_buffet


def test_h3_in_summary_does_not_swallow_following_journal_sibling():
    """Mid-note Transcript Notes still ends at ``### Content Planning``."""
    journal = (
        JOURNAL_WITH_H3_IN_SUMMARY.rstrip()
        + "\n\n### Content Planning\n- plan something\n"
    )
    later = format_granola_block(
        _note(
            note_id="not_later00000002",
            title="Later meeting",
            web_url="https://notes.granola.ai/d/later",
            summary_markdown="Later summary",
        )
    )
    updated, action = insert_transcript_notes_bullet(
        journal,
        later,
        ["granola:not_later00000002", "not_later00000002"],
    )
    assert action == "inserted"
    heading_idx = updated.index(TRANSCRIPT_NOTES_HEADER)
    later_idx = updated.index("#### [Later meeting](https://notes.granola.ai/d/later)")
    planning_idx = updated.index("### Content Planning")
    church_body_idx = updated.index(
        "The body of the church reflection that must stay with this note."
    )
    assert heading_idx < church_body_idx < later_idx < planning_idx
    assert updated[planning_idx:].startswith("### Content Planning\n- plan something")


def test_transcript_notes_stops_at_music_of_the_day():
    """Music of the Day is a journal sibling so Granola does not swallow it."""
    journal = (
        JOURNAL_WITH_H3_IN_SUMMARY.rstrip()
        + "\n\n### Music of the Day\n- [Song](https://open.spotify.com/track/abc) — Artist\n"
    )
    later = format_granola_block(
        _note(
            note_id="not_later00000002",
            title="Later meeting",
            web_url="https://notes.granola.ai/d/later",
            summary_markdown="Later summary",
        )
    )
    updated, action = insert_transcript_notes_bullet(
        journal,
        later,
        ["granola:not_later00000002", "not_later00000002"],
    )
    assert action == "inserted"
    music_idx = updated.index("### Music of the Day")
    later_idx = updated.index("#### [Later meeting](https://notes.granola.ai/d/later)")
    assert later_idx < music_idx
    assert updated[music_idx:].startswith(
        "### Music of the Day\n- [Song](https://open.spotify.com/track/abc) — Artist"
    )


# ---------------------------------------------------------------------------
# Summary block format / hydrate / upgrade
# ---------------------------------------------------------------------------


def test_format_granola_block_locked_shape():
    note = _note(summary_markdown=DEFAULT_SUMMARY_MARKDOWN)
    expected = (
        "#### [Quarterly yoghurt budget review]"
        "(https://notes.granola.ai/d/f3e45e0f-24cc-480b-9a6c-8b1f5e3d7a2c)\n"
        "<!-- granola:not_1d3tmYTlCICgjy -->\n"
        "\n"
        f"{DEFAULT_SUMMARY_MARKDOWN}"
    )
    assert format_granola_block(note) == expected
    assert format_granola_bullet(note) == expected
    assert granola_html_comment("not_1d3tmYTlCICgjy") == "<!-- granola:not_1d3tmYTlCICgjy -->"


def test_format_granola_block_falls_back_to_summary_text():
    note = _note(summary_markdown="", summary_text="Plain yoghurt takeaway")
    block = format_granola_block(note)
    assert "<!-- granola:not_1d3tmYTlCICgjy -->" in block
    assert "Plain yoghurt takeaway" in block
    assert note_summary_body(note) == "Plain yoghurt takeaway"


def test_format_granola_block_does_not_include_transcript_or_private_notes():
    note = _note(
        summary_markdown="Short summary only",
        summary_text="also short",
        private_notes_markdown="secret private aside",
        transcript=[{"text": "I am the full transcript and I am very long"}],
    )
    block = format_granola_block(note)
    assert "Short summary only" in block
    assert "full transcript" not in block
    assert "secret private aside" not in block


def test_hydrate_always_gets_note_detail_even_when_list_has_url():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    listed = _note()
    assert "summary_markdown" not in listed
    result, uploaded, _, mock_get, _ = _run_sync(
        [_list_page([listed])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        now=datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc),
        details_by_id={
            listed["id"]: {"summary_markdown": "## Church\n\n- Pray and reflect"},
        },
    )
    assert result["inserted"] == 1
    detail_urls = [
        (call.args[0] if call.args else "")
        for call in mock_get.call_args_list
    ]
    assert any(listed["id"] in url for url in detail_urls)
    content = uploaded[0]["content"]
    assert (
        "#### [Quarterly yoghurt budget review]"
        "(https://notes.granola.ai/d/f3e45e0f-24cc-480b-9a6c-8b1f5e3d7a2c)\n"
        "<!-- granola:not_1d3tmYTlCICgjy -->\n"
        "\n"
        "## Church\n"
        "\n"
        "- Pray and reflect"
    ) in content
    assert "- [Quarterly yoghurt budget review]" not in content


def test_title_only_bullet_is_upgraded_to_summary_block():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    note = _note(
        note_id="not_alreadyThere1",
        title="Older meeting",
        web_url="https://notes.granola.ai/d/old",
    )
    result, uploaded, _, _, _ = _run_sync(
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
    assert content.index("### Transcript Notes") < content.index("### Content Planning")


def test_second_note_is_separated_by_horizontal_rule():
    first = format_granola_block(_note(
        note_id="not_first00000001",
        title="First meeting",
        web_url="https://notes.granola.ai/d/first",
        summary_markdown="First summary",
    ))
    journal, action = insert_transcript_notes_bullet(
        SAMPLE_JOURNAL,
        first,
        ["granola:not_first00000001", "not_first00000001"],
    )
    assert action == "inserted"
    assert journal.count("\n---\n") == 1  # frontmatter only

    second = format_granola_block(_note(
        note_id="not_second0000002",
        title="Second meeting",
        web_url="https://notes.granola.ai/d/second",
        summary_markdown="Second summary",
    ))
    updated, action = insert_transcript_notes_bullet(
        journal,
        second,
        ["granola:not_second0000002", "not_second0000002"],
    )
    assert action == "inserted"
    section = updated[updated.index(TRANSCRIPT_NOTES_HEADER):]
    assert section.startswith(
        "### Transcript Notes\n"
        "\n"
        "#### [First meeting](https://notes.granola.ai/d/first)\n"
        "<!-- granola:not_first00000001 -->\n"
        "\n"
        "First summary\n"
        "\n"
        "---\n"
        "\n"
        "#### [Second meeting](https://notes.granola.ai/d/second)\n"
        "<!-- granola:not_second0000002 -->\n"
        "\n"
        "Second summary"
    )
    skipped, skip_action = insert_transcript_notes_bullet(
        updated,
        second,
        ["granola:not_second0000002", "not_second0000002"],
    )
    assert skip_action == "skipped"
    assert skipped == updated


# ---------------------------------------------------------------------------
# Replace-on-edit (note.edited)
# ---------------------------------------------------------------------------


def _three_note_journal():
    first = format_granola_block(_note(
        note_id="not_first00000001",
        title="First meeting",
        web_url="https://notes.granola.ai/d/first",
        summary_markdown="First summary",
    ))
    journal, _ = insert_transcript_notes_bullet(
        SAMPLE_JOURNAL,
        first,
        ["granola:not_first00000001", "not_first00000001"],
    )
    second = format_granola_block(_note(
        note_id="not_second0000002",
        title="Second meeting",
        web_url="https://notes.granola.ai/d/second",
        summary_markdown="Second summary",
    ))
    journal, _ = insert_transcript_notes_bullet(
        journal,
        second,
        ["granola:not_second0000002", "not_second0000002"],
    )
    third = format_granola_block(_note(
        note_id="not_third00000003",
        title="Third meeting",
        web_url="https://notes.granola.ai/d/third",
        summary_markdown="Third summary",
    ))
    journal, _ = insert_transcript_notes_bullet(
        journal,
        third,
        ["granola:not_third00000003", "not_third00000003"],
    )
    return journal


def test_replace_existing_swaps_block_in_place_and_keeps_neighbors():
    journal = _three_note_journal()
    edited = format_granola_block(_note(
        note_id="not_second0000002",
        title="Second meeting (edited)",
        web_url="https://notes.granola.ai/d/second",
        summary_markdown="### Updated heading\n\nEdited second summary",
    ))
    skipped, skip_action = insert_transcript_notes_bullet(
        journal,
        edited,
        ["granola:not_second0000002", "not_second0000002"],
    )
    assert skip_action == "skipped"
    assert skipped == journal

    updated, action = insert_transcript_notes_bullet(
        journal,
        edited,
        ["granola:not_second0000002", "not_second0000002"],
        replace_existing=True,
    )
    assert action == "replaced"
    section = updated[updated.index(TRANSCRIPT_NOTES_HEADER):]
    first_title = "#### [First meeting](https://notes.granola.ai/d/first)"
    second_title = "#### [Second meeting (edited)](https://notes.granola.ai/d/second)"
    third_title = "#### [Third meeting](https://notes.granola.ai/d/third)"
    assert section.index(first_title) < section.index(second_title)
    assert section.index(second_title) < section.index(third_title)
    assert "Edited second summary" in section
    assert "Second summary" not in section
    assert "First summary" in section
    assert "Third summary" in section
    assert "### Updated heading" in section
    assert updated.count("<!-- granola:not_second0000002 -->") == 1
    assert updated.count(TRANSCRIPT_NOTES_HEADER) == 1
    assert updated.index("### Content Buffet:") < updated.index(TRANSCRIPT_NOTES_HEADER)


def test_replace_existing_inserts_when_block_is_missing():
    edited = format_granola_block(_note(
        note_id="not_latewrite0001",
        title="Late write",
        web_url="https://notes.granola.ai/d/late",
        summary_markdown="Arrived via edit",
    ))
    updated, action = insert_transcript_notes_bullet(
        SAMPLE_JOURNAL,
        edited,
        ["granola:not_latewrite0001", "not_latewrite0001"],
        replace_existing=True,
    )
    assert action == "inserted"
    assert "#### [Late write](https://notes.granola.ai/d/late)" in updated
    assert "<!-- granola:not_latewrite0001 -->" in updated
    assert "Arrived via edit" in updated


def test_replace_existing_first_and_last_keep_separator_shape():
    journal = _three_note_journal()
    first_edit = format_granola_block(_note(
        note_id="not_first00000001",
        title="First meeting",
        web_url="https://notes.granola.ai/d/first",
        summary_markdown="First summary rewritten",
    ))
    updated, action = insert_transcript_notes_bullet(
        journal,
        first_edit,
        ["granola:not_first00000001", "not_first00000001"],
        replace_existing=True,
    )
    assert action == "replaced"
    section = updated[updated.index(TRANSCRIPT_NOTES_HEADER):]
    assert section.startswith(
        "### Transcript Notes\n"
        "\n"
        "#### [First meeting](https://notes.granola.ai/d/first)\n"
        "<!-- granola:not_first00000001 -->\n"
        "\n"
        "First summary rewritten\n"
        "\n"
        "---\n"
        "\n"
        "#### [Second meeting](https://notes.granola.ai/d/second)\n"
    )
    assert "First summary\n" not in section

    last_edit = format_granola_block(_note(
        note_id="not_third00000003",
        title="Third meeting",
        web_url="https://notes.granola.ai/d/third",
        summary_markdown="Third summary rewritten",
    ))
    updated, action = insert_transcript_notes_bullet(
        updated,
        last_edit,
        ["granola:not_third00000003", "not_third00000003"],
        replace_existing=True,
    )
    assert action == "replaced"
    section = updated[updated.index(TRANSCRIPT_NOTES_HEADER):]
    assert "Third summary rewritten" in section
    assert section.index("Second summary") < section.index("Third summary rewritten")
    assert "Third summary\n" not in section


def test_replace_existing_does_not_split_on_hr_or_h3_in_summary():
    """``---`` / ``###`` inside the edited note must not steal the next note."""
    church = format_granola_block(_note(
        note_id="not_church0000001",
        title="Church reflection",
        web_url="https://notes.granola.ai/d/church",
        summary_markdown=(
            "### Church and Spiritual Practice\n"
            "\n"
            "Old body\n"
            "\n"
            "---\n"
            "\n"
            "Still the same note"
        ),
    ))
    journal, _ = insert_transcript_notes_bullet(
        SAMPLE_JOURNAL,
        church,
        ["granola:not_church0000001", "not_church0000001"],
    )
    later = format_granola_block(_note(
        note_id="not_later00000002",
        title="Later meeting",
        web_url="https://notes.granola.ai/d/later",
        summary_markdown="Later summary",
    ))
    journal, _ = insert_transcript_notes_bullet(
        journal,
        later,
        ["granola:not_later00000002", "not_later00000002"],
    )
    edited = format_granola_block(_note(
        note_id="not_church0000001",
        title="Church reflection",
        web_url="https://notes.granola.ai/d/church",
        summary_markdown=(
            "### Church and Spiritual Practice\n"
            "\n"
            "New body after edit\n"
            "\n"
            "---\n"
            "\n"
            "Still the same note"
        ),
    ))
    updated, action = insert_transcript_notes_bullet(
        journal,
        edited,
        ["granola:not_church0000001", "not_church0000001"],
        replace_existing=True,
    )
    assert action == "replaced"
    section = updated[updated.index(TRANSCRIPT_NOTES_HEADER):]
    later_title = "#### [Later meeting](https://notes.granola.ai/d/later)"
    assert "New body after edit" in section
    assert "Old body" not in section
    assert section.index("Still the same note") < section.index(later_title)
    assert "Later summary" in section[section.index(later_title):]


def test_write_notes_by_journal_replace_existing_uploads_new_summary():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    original = JOURNAL_WITH_SUMMARY_BLOCK
    mock_dbx, uploaded, _store = _mock_dropbox({sep_path: original})
    note = _note(
        note_id="not_alreadyThere1",
        title="Older meeting",
        web_url="https://notes.granola.ai/d/old",
        created_at="2026-09-05T20:00:00Z",
        summary_markdown="Edited summary after granola regenerate",
    )
    with patch("services.granola.sync._get_dropbox_client", return_value=mock_dbx), patch(
        "services.granola.sync._resolve_journal_folder",
        return_value=JOURNAL_FOLDER,
    ):
        skipped = write_notes_by_journal([note])
        replaced = write_notes_by_journal([note], replace_existing=True)

    assert skipped["skipped"] == 1
    assert skipped["inserted"] == 0
    assert skipped["replaced"] == 0
    assert skipped["files_written"] == 0
    assert replaced["replaced"] == 1
    assert replaced["inserted"] == 0
    assert replaced["skipped"] == 0
    assert replaced["files_written"] == 1
    content = uploaded[0]["content"]
    assert "Edited summary after granola regenerate" in content
    assert "Already synced summary." not in content
    assert "<!-- granola:not_alreadyThere1 -->" in content
    assert "### Content Planning" in content
    planning_idx = content.index("### Content Planning")
    assert content.index("Edited summary after granola regenerate") < planning_idx


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


def test_granola_job_is_registered_as_manual_2099_safety_net():
    from scheduler import SCHEDULED_JOBS

    job = next(j for j in SCHEDULED_JOBS if j["id"] == "sync_granola_notes")
    assert job["name"] == "Sync Granola Notes to Daily Journal (manual)"
    trigger = str(job["trigger"])
    assert "2099" in trigger
    assert "*/15" not in trigger


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


def _rev_conflict_api_error():
    import dropbox

    reason = dropbox.files.WriteError.conflict(dropbox.files.WriteConflictError.file)
    failed = dropbox.files.UploadWriteFailed(reason=reason, upload_session_id="sess")
    error = dropbox.files.UploadError.path(failed)
    return dropbox.exceptions.ApiError("req", error, "", "")


def test_granola_journal_write_uses_update_mode_not_overwrite():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    result, uploaded, _, _, _ = _run_sync(
        [_list_page([_note()])],
        contents_by_path={sep_path: SAMPLE_JOURNAL},
        now=datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc),
    )
    assert result["files_written"] == 1
    assert result["deferred"] == 0
    mode = uploaded[0]["mode"]
    assert mode.is_update()
    assert mode.get_update() == "aaaaaaaaaaaaaaaa"
    assert not mode.is_overwrite()
    assert uploaded[0]["autorename"] is False


def test_granola_rev_conflict_retries_once_then_enqueues():
    sep_path = f"{JOURNAL_FOLDER}/Sep 5, 2026.md"
    mock_dbx, _uploaded, _store = _mock_dropbox({sep_path: SAMPLE_JOURNAL})
    mock_dbx.files_upload.side_effect = _rev_conflict_api_error()
    note = _note(summary_markdown=DEFAULT_SUMMARY_MARKDOWN)

    with (
        patch("services.granola.sync._get_dropbox_client", return_value=mock_dbx),
        patch("services.granola.sync._resolve_journal_folder", return_value=JOURNAL_FOLDER),
        patch("services.obsidian.utils.dropbox_rev_safe.record_deferred_write") as mock_enqueue,
    ):
        result = write_notes_by_journal([note], now=datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc))

    assert result["deferred"] == 1
    assert result["files_written"] == 0
    assert mock_dbx.files_upload.call_count == 2
    for call in mock_dbx.files_upload.call_args_list:
        assert call.kwargs["mode"].is_update()
        assert not call.kwargs["mode"].is_overwrite()
    mock_enqueue.assert_called()
    assert mock_enqueue.call_args.kwargs["source"] == "granola"
    assert mock_enqueue.call_args.kwargs["kind"] == "journal_note"
