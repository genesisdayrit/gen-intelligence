"""Knowledge Hub processors append a wikilink to today's journal Content Buffet."""

import os
import sys
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import dropbox
import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")


@pytest.fixture(autouse=True)
def _force_la_timezone(monkeypatch):
    monkeypatch.setenv("SYSTEM_TIMEZONE", "America/Los_Angeles")
    monkeypatch.setenv("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")


from services.obsidian.add_readwise_buffet import (  # noqa: E402
    _wikilink_from_note_stem,
    append_readwise_buffet,
    append_wikilink_to_journal_buffet,
    format_readwise_bullet,
    insert_content_buffet_bullet,
    journal_filename,
    reader_knowledge_hub_note_stem,
    youtube_knowledge_hub_note_stem,
)
from services.obsidian.add_shared_link import add_shared_link  # noqa: E402
from services.obsidian.add_youtube_link import (  # noqa: E402
    add_youtube_link,
    apply_youtube_extra_frontmatter,
)
from services.obsidian import add_shared_link as shared_mod  # noqa: E402
from services.obsidian.utils.date_helpers import get_effective_date  # noqa: E402

LA = pytz.timezone("America/Los_Angeles")
FIXED_LOCAL = LA.localize(datetime(2026, 8, 22, 15, 0))
FIXED_UTC = FIXED_LOCAL.astimezone(timezone.utc)
JOURNAL_DATE = journal_filename(get_effective_date(FIXED_LOCAL)).removesuffix(".md")
assert JOURNAL_DATE == "Aug 22, 2026"

KH_PATH = "/obsidian/personal/_knowledge-hub"
JOURNAL_FOLDER = "/obsidian/personal/01_daily/_journal"
YOUTUBE_STEM = youtube_knowledge_hub_note_stem("Cool Video", "A Channel")
assert YOUTUBE_STEM == "Cool Video by A Channel"
JOURNAL_PATH = f"{JOURNAL_FOLDER}/{JOURNAL_DATE}.md"

SAMPLE_JOURNAL = """---
date: 2026-08-22
---

### Content Buffet:
- 

### Content Planning
- plan something
"""

JOURNAL_WITH_ITEM = """---
date: 2026-08-22
---

### Content Buffet:
- [[Already There]]

### Content Planning
- plan something
"""


def _existing_kh(*, journal_dates: list[str], title: str = "My Article") -> str:
    journal_yaml = "\n".join(f'  - "[[{d}]]"' for d in journal_dates)
    return f"""---
Journal:
{journal_yaml}
created time: 2026-01-01T00:00:00+00:00
modified time: 2026-01-01T00:00:00+00:00
key words:
People:
URL: https://example.com/article
author:
Notes+Ideas:
Experiences:
Tags:
---

## {title}
"""


def _not_found_api_error() -> dropbox.exceptions.ApiError:
    error = MagicMock()
    error.is_path.return_value = True
    error.get_path.return_value.is_not_found.return_value = True
    return dropbox.exceptions.ApiError("req", error, "", "")


def _mock_dbx(
    *,
    kh_exists=False,
    kh_content=None,
    journal_content=SAMPLE_JOURNAL,
    journal_missing=False,
    existing_paths=None,
):
    uploads: list[dict] = []
    mock_dbx = MagicMock()

    def get_metadata(path):
        if existing_paths is not None:
            if path in existing_paths or any(u["path"] == path for u in uploads):
                return MagicMock()
            raise _not_found_api_error()
        if KH_PATH in path:
            if kh_exists:
                return MagicMock()
            raise _not_found_api_error()
        raise _not_found_api_error()

    def download(path):
        response = MagicMock()
        if JOURNAL_FOLDER in path:
            if journal_missing:
                raise FileNotFoundError(f"Journal not found: {path}")
            response.content = journal_content.encode("utf-8")
            return None, response
        for upload in reversed(uploads):
            if upload["path"] == path:
                response.content = upload["content"].encode("utf-8")
                return None, response
        response.content = (kh_content or "").encode("utf-8")
        return None, response

    def capture_upload(data, path, mode=None):
        uploads.append({"content": data.decode("utf-8"), "path": path, "mode": mode})

    def list_folder(path, **_kwargs):
        result = MagicMock()
        result.has_more = False
        prefix = path.rstrip("/") + "/"
        seen: set[str] = set()
        entries = []
        candidates = list(existing_paths or [])
        for upload in uploads:
            candidates.append(upload["path"])
        for file_path in candidates:
            if not file_path.startswith(prefix):
                continue
            name = file_path[len(prefix) :]
            if "/" in name or name in seen:
                continue
            seen.add(name)
            entry = MagicMock()
            entry.name = name
            entry.path_display = file_path
            entry.path_lower = file_path
            entries.append(entry)
        result.entries = entries
        return result

    mock_dbx.files_get_metadata.side_effect = get_metadata
    mock_dbx.files_download.side_effect = download
    mock_dbx.files_upload.side_effect = capture_upload
    mock_dbx.files_list_folder.side_effect = list_folder
    return mock_dbx, uploads


def _configure_frozen_datetime(mock_dt):
    mock_dt.now.return_value = FIXED_UTC


def _shared_patches(mock_dbx, **web):
    web_content = {
        "title": web.get("title", "My Article"),
        "author": web.get("author"),
        "body_text": web.get("body_text"),
    }
    return [
        patch("services.obsidian.add_shared_link._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_shared_link._find_knowledge_hub_path", return_value=KH_PATH),
        patch("services.obsidian.add_shared_link.fetch_web_content", return_value=web_content),
        patch("services.obsidian.add_shared_link._extract_people_from_article", return_value=[]),
        patch("services.obsidian.add_readwise_buffet._resolve_journal_folder", return_value=JOURNAL_FOLDER),
        patch.object(shared_mod, "timezone_str", "America/Los_Angeles"),
    ]


@contextmanager
def _patched(patches, datetime_target=None):
    with ExitStack() as stack:
        mock_dt = None
        if datetime_target:
            mock_dt = stack.enter_context(patch(datetime_target))
            _configure_frozen_datetime(mock_dt)
        for p in patches:
            stack.enter_context(p)
        yield mock_dt


def _youtube_patches(mock_dbx, title="Cool Video", transcript=None):
    return [
        patch("services.obsidian.add_youtube_link._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_youtube_link._find_knowledge_hub_path", return_value=KH_PATH),
        patch(
            "services.obsidian.add_youtube_link.fetch_youtube_metadata",
            return_value={"title": title, "author_name": "A Channel", "description": None},
        ),
        patch("services.obsidian.add_youtube_link._extract_people", return_value=[]),
        patch("services.obsidian.add_youtube_link._fetch_transcript", return_value=transcript),
        patch("services.obsidian.add_readwise_buffet._resolve_journal_folder", return_value=JOURNAL_FOLDER),
    ]


def _kh_upload(uploads: list[dict]) -> dict:
    matches = [u for u in uploads if KH_PATH in u["path"]]
    assert matches, f"expected Knowledge Hub upload, got {uploads}"
    return matches[-1]


def _journal_upload(uploads: list[dict]) -> dict | None:
    matches = [u for u in uploads if JOURNAL_FOLDER in u["path"]]
    return matches[-1] if matches else None


def _journal_date_from_kh(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if "[[" in stripped and "]]" in stripped:
            start = stripped.index("[[") + 2
            end = stripped.index("]]", start)
            return stripped[start:end]
    raise AssertionError(f"no Journal wikilink in KH content:\n{content}")


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_wikilink_from_note_stem_strips_illegal_chars_only():
    assert _wikilink_from_note_stem("Foo | Bar # Baz^]]") == "Foo  Bar  Baz"
    assert _wikilink_from_note_stem("Keep  double  spaces") == "Keep  double  spaces"
    assert _wikilink_from_note_stem("|||") is None


def test_append_wikilink_replaces_placeholder():
    mock_dbx, uploads = _mock_dbx()
    with patch("services.obsidian.add_readwise_buffet._resolve_journal_folder", return_value=JOURNAL_FOLDER):
        result = append_wikilink_to_journal_buffet("My Article", JOURNAL_DATE, dbx=mock_dbx)

    assert result["success"] is True
    assert result["action"] == "replaced"
    assert result["file_path"] == JOURNAL_PATH
    assert uploads[0]["path"] == JOURNAL_PATH
    assert "- [[My Article]]" in uploads[0]["content"]
    section = uploads[0]["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    assert section.count("- [[") == 1


def test_append_wikilink_dedups_existing_title():
    mock_dbx, uploads = _mock_dbx(journal_content=JOURNAL_WITH_ITEM.replace("Already There", "My Article"))
    with patch("services.obsidian.add_readwise_buffet._resolve_journal_folder", return_value=JOURNAL_FOLDER):
        result = append_wikilink_to_journal_buffet("My Article", JOURNAL_DATE, dbx=mock_dbx)

    assert result["action"] == "skipped"
    assert uploads == []


def test_append_wikilink_does_not_collapse_into_highlight_quote_line():
    """Standalone - [[Title]] is a different line from a highlight quote."""
    journal = """---
date: 2026-08-22
---

### Content Buffet:
- [[My Article]]: ["a quote"](https://readwise.io/open/954480)

### Content Planning
- plan something
"""
    mock_dbx, uploads = _mock_dbx(journal_content=journal)
    with patch("services.obsidian.add_readwise_buffet._resolve_journal_folder", return_value=JOURNAL_FOLDER):
        result = append_wikilink_to_journal_buffet("My Article", JOURNAL_DATE, dbx=mock_dbx)

    assert result["action"] == "inserted"
    section = uploads[0]["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    lines = [line for line in section.splitlines() if line.strip()]
    assert lines == [
        '- [[My Article]]: ["a quote"](https://readwise.io/open/954480)',
        "- [[My Article]]",
    ]


def test_append_wikilink_missing_journal_does_not_create():
    mock_dbx, uploads = _mock_dbx(journal_missing=True)
    with patch("services.obsidian.add_readwise_buffet._resolve_journal_folder", return_value=JOURNAL_FOLDER):
        result = append_wikilink_to_journal_buffet("My Article", JOURNAL_DATE, dbx=mock_dbx)

    assert result["success"] is True
    assert result["action"] == "skipped_missing_journal"
    assert uploads == []
    mock_dbx.files_upload.assert_not_called()


# ---------------------------------------------------------------------------
# add_shared_link
# ---------------------------------------------------------------------------


def test_shared_link_create_appends_buffet_wikilink():
    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    with _patched(_shared_patches(mock_dbx, title="My Article"), "services.obsidian.add_shared_link.datetime"):
        result = add_shared_link("https://example.com/article", title="My Article")

    assert result["success"] is True
    assert result["action"] == "created"

    kh = _kh_upload(uploads)
    journal = _journal_upload(uploads)
    assert journal is not None
    kh_date = _journal_date_from_kh(kh["content"])
    assert kh_date == JOURNAL_DATE
    assert journal["path"] == JOURNAL_PATH
    assert journal["path"].endswith(f"{kh_date}.md")
    assert "- [[My Article]]" in journal["content"]
    assert "readwise_id" not in kh["content"]
    assert "readwise_url" not in kh["content"]
    assert "saved_at:" not in kh["content"]
    section = journal["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    assert "[source]" not in section
    assert "[readwise]" not in section


def test_shared_link_update_appends_buffet_wikilink():
    mock_dbx, uploads = _mock_dbx(
        kh_exists=True,
        kh_content=_existing_kh(journal_dates=["Jan 1, 2026"]),
    )
    with _patched(_shared_patches(mock_dbx, title="My Article"), "services.obsidian.add_shared_link.datetime"):
        result = add_shared_link("https://example.com/article", title="My Article")

    assert result["success"] is True
    assert result["action"] == "updated"
    kh = _kh_upload(uploads)
    assert f"[[{JOURNAL_DATE}]]" in kh["content"]
    journal = _journal_upload(uploads)
    assert journal is not None
    assert "- [[My Article]]" in journal["content"]
    assert journal["path"].endswith(f"{JOURNAL_DATE}.md")


def test_shared_link_already_linked_today_does_not_double():
    mock_dbx, uploads = _mock_dbx(
        kh_exists=True,
        kh_content=_existing_kh(journal_dates=[JOURNAL_DATE]),
        journal_content=JOURNAL_WITH_ITEM,
    )
    with _patched(_shared_patches(mock_dbx, title="My Article"), "services.obsidian.add_shared_link.datetime"):
        result = add_shared_link("https://example.com/article", title="My Article")

    assert result["success"] is True
    assert result["action"] == "skipped"
    assert uploads == []
    mock_dbx.files_upload.assert_not_called()


def test_shared_link_missing_journal_does_not_fail_kh_write():
    mock_dbx, uploads = _mock_dbx(kh_exists=False, journal_missing=True)
    with _patched(_shared_patches(mock_dbx, title="My Article"), "services.obsidian.add_shared_link.datetime"):
        result = add_shared_link("https://example.com/article", title="My Article")

    assert result["success"] is True
    assert result["action"] == "created"
    assert result["error"] is None
    kh = _kh_upload(uploads)
    assert f"[[{JOURNAL_DATE}]]" in kh["content"]
    assert _journal_upload(uploads) is None


def test_shared_link_wikilink_uses_sanitized_filename_stem():
    title = 'What is AI? A "deep" look / part 1'
    expected_stem = shared_mod._sanitize_filename(title)
    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    with _patched(_shared_patches(mock_dbx, title=title), "services.obsidian.add_shared_link.datetime"):
        result = add_shared_link("https://example.com/article", title=title)

    assert result["success"] is True
    kh = _kh_upload(uploads)
    assert kh["path"].endswith(f"{expected_stem}.md")
    journal = _journal_upload(uploads)
    assert journal is not None
    assert f"- [[{expected_stem}]]" in journal["content"]


# ---------------------------------------------------------------------------
# add_youtube_link
# ---------------------------------------------------------------------------


def test_youtube_link_create_appends_buffet_wikilink():
    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    with _patched(_youtube_patches(mock_dbx, title="Cool Video"), "services.obsidian.add_youtube_link.datetime"):
        result = add_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    assert result["success"] is True
    assert result["action"] == "created"
    kh = _kh_upload(uploads)
    journal = _journal_upload(uploads)
    assert journal is not None
    kh_date = _journal_date_from_kh(kh["content"])
    assert kh_date == JOURNAL_DATE
    assert journal["path"] == f"{JOURNAL_FOLDER}/{kh_date}.md"
    section = journal["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    assert f"- [[{YOUTUBE_STEM}]]" in section
    assert "](http" not in section
    assert "youtube.com" not in section


def test_youtube_link_update_appends_buffet_wikilink():
    mock_dbx, uploads = _mock_dbx(
        kh_exists=True,
        kh_content=_existing_kh(journal_dates=["Jan 1, 2026"], title="Cool Video"),
    )
    with _patched(_youtube_patches(mock_dbx, title="Cool Video"), "services.obsidian.add_youtube_link.datetime"):
        result = add_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    assert result["success"] is True
    assert result["action"] == "updated"
    journal = _journal_upload(uploads)
    assert journal is not None
    section = journal["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    assert f"- [[{YOUTUBE_STEM}]]" in section
    assert "](http" not in section


def test_youtube_link_already_linked_today_does_not_double():
    mock_dbx, uploads = _mock_dbx(
        kh_exists=True,
        kh_content=_existing_kh(journal_dates=[JOURNAL_DATE], title="Cool Video"),
    )
    with _patched(_youtube_patches(mock_dbx, title="Cool Video"), "services.obsidian.add_youtube_link.datetime"):
        result = add_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    assert result["success"] is True
    assert result["action"] == "skipped"
    assert uploads == []


def test_youtube_link_wikilink_uses_reader_stem_helper():
    title = 'Watch this: "AI" / part 1?'
    expected_stem = youtube_knowledge_hub_note_stem(title, "A Channel")
    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    with _patched(_youtube_patches(mock_dbx, title=title), "services.obsidian.add_youtube_link.datetime"):
        result = add_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    assert result["success"] is True
    assert result["stem"] == expected_stem
    journal = _journal_upload(uploads)
    assert journal is not None
    section = journal["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    assert f"- [[{expected_stem}]]" in section
    assert "](http" not in section
    assert title not in section


def test_youtube_link_missing_journal_does_not_fail_kh_write():
    mock_dbx, uploads = _mock_dbx(kh_exists=False, journal_missing=True)
    with _patched(_youtube_patches(mock_dbx, title="Cool Video"), "services.obsidian.add_youtube_link.datetime"):
        result = add_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    assert result["success"] is True
    assert result["action"] == "created"
    assert result["error"] is None
    assert _kh_upload(uploads) is not None
    assert _journal_upload(uploads) is None


def test_shared_link_explicit_journal_date_not_today():
    """Passing journal_date writes that day even when 'now' is a different date."""
    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    with _patched(_shared_patches(mock_dbx, title="My Article"), "services.obsidian.add_shared_link.datetime"):
        result = add_shared_link(
            "https://example.com/article",
            title="My Article",
            journal_date="Nov 28, 2025",
        )

    assert result["success"] is True
    assert result["action"] == "created"
    kh = _kh_upload(uploads)
    journal = _journal_upload(uploads)
    assert _journal_date_from_kh(kh["content"]) == "Nov 28, 2025"
    assert journal is not None
    assert journal["path"] == f"{JOURNAL_FOLDER}/Nov 28, 2025.md"
    assert "- [[My Article]]" in journal["content"]
    assert f"[[{JOURNAL_DATE}]]" not in kh["content"]


def test_youtube_link_explicit_journal_date_not_today():
    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    with _patched(_youtube_patches(mock_dbx, title="Cool Video"), "services.obsidian.add_youtube_link.datetime"):
        result = add_youtube_link(
            "https://www.youtube.com/watch?v=abcdefghijk",
            journal_date="Nov 28, 2025",
        )

    assert result["success"] is True
    kh = _kh_upload(uploads)
    journal = _journal_upload(uploads)
    assert _journal_date_from_kh(kh["content"]) == "Nov 28, 2025"
    assert journal is not None
    assert journal["path"] == f"{JOURNAL_FOLDER}/Nov 28, 2025.md"
    assert f"- [[{YOUTUBE_STEM}]]" in journal["content"]


def _reader_document_payload(**overrides):
    data = {
        "id": "01kb5cap1wy21zp37bc2rjj",
        "url": "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj",
        "title": "Our Black Friday sale ends soon",
        "author": "The Verge",
        "source_url": "https://www.theverge.com/black-friday",
        "category": "article",
        "parent_id": None,
        "created_at": "2025-11-28T14:02:02.213618+00:00",
        "saved_at": "2025-11-28T14:02:02.173000+00:00",
        "event_type": "reader.any_document.created",
    }
    data.update(overrides)
    return data


def test_reader_document_writes_kh_note_and_buffet_wikilink():
    """Real share helper: KH file + - [[Title by Author]], no nested buffet metadata."""
    payload = _reader_document_payload()
    stem = reader_knowledge_hub_note_stem(payload["title"], payload["author"])
    assert stem == "Our Black Friday sale ends soon by The Verge"
    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    now = LA.localize(datetime(2026, 8, 22, 15, 0))

    with _patched(_shared_patches(mock_dbx, title=payload["title"]), "services.obsidian.add_shared_link.datetime"):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert result["action"] == "created"
    kh = _kh_upload(uploads)
    journal = _journal_upload(uploads)
    assert kh["path"].endswith("Our Black Friday sale ends soon by The Verge.md")
    assert kh["path"].endswith(f"{stem}.md")
    assert _journal_date_from_kh(kh["content"]) == "Nov 28, 2025"
    assert journal is not None
    assert journal["path"] == f"{JOURNAL_FOLDER}/Nov 28, 2025.md"
    assert "URL: https://www.theverge.com/black-friday" in kh["content"]
    assert 'author: "[[The Verge]]"' in kh["content"]
    assert "readwise_id: 01kb5cap1wy21zp37bc2rjj" in kh["content"]
    assert "readwise_url: https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj" in kh["content"]
    assert "saved_at: 2025-11-28T14:02:02.213618+00:00" in kh["content"]
    assert "published:" not in kh["content"]
    section = journal["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    lines = [line for line in section.splitlines() if line.strip()]
    assert lines == [f"- [[{stem}]]"]
    assert "readwise.io" not in section
    assert "published:" not in section
    assert "saved:" not in section
    assert '["' not in section


def test_reader_document_missing_journal_does_not_fail_kh_save():
    payload = _reader_document_payload()
    mock_dbx, uploads = _mock_dbx(kh_exists=False, journal_missing=True)
    now = LA.localize(datetime(2026, 8, 22, 15, 0))

    with _patched(_shared_patches(mock_dbx, title=payload["title"]), "services.obsidian.add_shared_link.datetime"):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert result["action"] == "created"
    assert result["error"] is None
    kh = _kh_upload(uploads)
    assert "readwise_id: 01kb5cap1wy21zp37bc2rjj" in kh["content"]
    assert "readwise_url: https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj" in kh["content"]
    assert _journal_upload(uploads) is None


def test_reader_document_omits_missing_published_and_author():
    payload = _reader_document_payload(author=None)
    del payload["author"]
    stem = reader_knowledge_hub_note_stem(payload["title"], None)
    assert stem == payload["title"]
    assert " by " not in stem
    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    now = LA.localize(datetime(2026, 8, 22, 15, 0))

    with _patched(_shared_patches(mock_dbx, title=payload["title"]), "services.obsidian.add_shared_link.datetime"):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    kh = _kh_upload(uploads)
    journal = _journal_upload(uploads)
    assert "author: The Verge" not in kh["content"]
    assert "published:" not in kh["content"]
    assert "readwise_id: 01kb5cap1wy21zp37bc2rjj" in kh["content"]
    section = journal["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    lines = [line for line in section.splitlines() if line.strip()]
    assert lines == [f"- [[{stem}]]"]
    assert "readwise.io" not in section
    assert "published:" not in section
    assert "saved:" not in section
    assert "  - The Verge" not in lines


def test_reader_document_uses_creator_for_title_by_author_stem():
    payload = _reader_document_payload(author=None, creator="Casey Newton")
    del payload["author"]
    stem = reader_knowledge_hub_note_stem(payload["title"], payload["creator"])
    assert stem == "Our Black Friday sale ends soon by Casey Newton"
    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    now = LA.localize(datetime(2026, 8, 22, 15, 0))

    with _patched(_shared_patches(mock_dbx, title=payload["title"]), "services.obsidian.add_shared_link.datetime"):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    kh = _kh_upload(uploads)
    journal = _journal_upload(uploads)
    assert kh["path"].endswith(f"{stem}.md")
    assert 'author: "[[Casey Newton]]"' in kh["content"]
    section = journal["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    lines = [line for line in section.splitlines() if line.strip()]
    assert lines == [f"- [[{stem}]]"]
    assert "readwise.io" not in section
    assert "published:" not in section
    assert "saved:" not in section


def test_reader_document_update_fills_empty_extras_keeps_people_body():
    payload = _reader_document_payload()
    existing = f"""---
Journal:
  - "[[Jan 1, 2026]]"
created time: 2026-01-01T00:00:00+00:00
modified time: 2026-01-01T00:00:00+00:00
key words:
People:
  - "[[Casey Newton]]"
URL: https://www.theverge.com/black-friday
author:
Notes+Ideas:
Experiences:
Tags:
---

## {payload["title"]}

Keep this body
"""
    mock_dbx, uploads = _mock_dbx(kh_exists=True, kh_content=existing)
    now = LA.localize(datetime(2026, 8, 22, 15, 0))

    with _patched(_shared_patches(mock_dbx, title=payload["title"]), "services.obsidian.add_shared_link.datetime"):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert result["action"] == "updated"
    kh = _kh_upload(uploads)
    assert "[[Casey Newton]]" in kh["content"]
    assert "Keep this body" in kh["content"]
    assert "[[The Verge]]" in kh["content"]
    assert "01kb5cap1wy21zp37bc2rjj" in kh["content"]
    assert "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj" in kh["content"]


def test_reader_highlight_append_does_not_add_another_metadata_block():
    payload = _reader_document_payload()
    stem = reader_knowledge_hub_note_stem(payload["title"], payload["author"])
    journal = f"""---
date: 2025-11-28
---

### Content Buffet:
- [[{stem}]]

### Content Planning
- plan something
"""
    highlight = {
        "id": 954480,
        "text": "Most Amazing Highlight Ever",
        "title": payload["title"],
        "author": "The Verge",
        "highlighted_at": "2025-11-28T18:00:00Z",
        "event_type": "readwise.highlight.created",
        "book_id": 8237,
    }
    bullet = format_readwise_bullet(highlight)
    updated, action = insert_content_buffet_bullet(
        journal, bullet, keys=[f"https://readwise.io/open/{highlight['id']}"]
    )
    assert action == "inserted"
    section = updated.split("### Content Buffet:")[1].split("### Content Planning")[0]
    lines = [line for line in section.splitlines() if line.strip()]
    assert lines == [
        f"- [[{stem}]]",
        f'- [[{stem}]]: ["Most Amazing Highlight Ever"](https://readwise.io/open/954480)',
    ]
    assert "published:" not in section
    assert "saved:" not in section


def test_shared_link_without_extras_does_not_get_readwise_id():
    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    with _patched(_shared_patches(mock_dbx, title="My Article"), "services.obsidian.add_shared_link.datetime"):
        result = add_shared_link("https://example.com/article", title="My Article")

    assert result["success"] is True
    kh = _kh_upload(uploads)
    journal = _journal_upload(uploads)
    assert "readwise_id" not in kh["content"]
    assert "readwise_url" not in kh["content"]
    assert "saved_at:" not in kh["content"]
    section = journal["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    lines = [line for line in section.splitlines() if line.strip()]
    assert lines == ["- [[My Article]]"]
    assert kh["path"].endswith("My Article.md")
    assert " by " not in kh["path"]
    assert " by " not in section


def test_reader_document_same_day_does_not_double_buffet_wikilink():
    payload = _reader_document_payload()
    stem = reader_knowledge_hub_note_stem(payload["title"], payload["author"])
    mock_dbx, uploads = _mock_dbx(
        kh_exists=True,
        kh_content=_existing_kh(journal_dates=["Nov 28, 2025"], title=payload["title"]),
        journal_content=JOURNAL_WITH_ITEM.replace("Already There", stem),
    )
    now = LA.localize(datetime(2026, 8, 22, 15, 0))

    with _patched(_shared_patches(mock_dbx, title=payload["title"]), "services.obsidian.add_shared_link.datetime"):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert result["action"] == "skipped"
    assert uploads == []
    mock_dbx.files_upload.assert_not_called()


# ---------------------------------------------------------------------------
# YouTube share → Reader save YAML + webhook stem collision
# ---------------------------------------------------------------------------


def _reader_youtube_payload(**overrides):
    data = {
        "title": "Cool Video",
        "author": "A Channel",
        "source_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "category": "video",
    }
    data.update(overrides)
    return _reader_document_payload(**data)


def test_reader_youtube_webhook_uses_reader_title_by_author_stem():
    """Fresh YouTube Reader docs use the shared Title by Author stem."""
    payload = _reader_youtube_payload()
    reader_stem = reader_knowledge_hub_note_stem(payload["title"], payload["author"])
    assert reader_stem == YOUTUBE_STEM

    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    now = LA.localize(datetime(2026, 8, 22, 15, 0))

    with _patched(
        _youtube_patches(mock_dbx, title="Cool Video"),
        "services.obsidian.add_youtube_link.datetime",
    ):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    kh = _kh_upload(uploads)
    assert kh["path"].endswith(f"{YOUTUBE_STEM}.md")
    journal = _journal_upload(uploads)
    assert journal is not None
    section = journal["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    assert f"- [[{YOUTUBE_STEM}]]" in section
    assert "readwise_id: 01kb5cap1wy21zp37bc2rjj" in kh["content"]
    assert "readwise_url: https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj" in kh["content"]


def test_reader_youtube_webhook_does_not_create_second_note_when_stem_drifts():
    """Existing title-only note is updated in place; no second Title by Author file."""
    payload = _reader_youtube_payload(title="Cool Video (Reader)")
    drifted = "Cool Video (Reader) by A Channel"
    assert reader_knowledge_hub_note_stem(payload["title"], payload["author"]) == drifted
    old_path = f"{KH_PATH}/Cool Video.md"
    existing = _existing_kh(
        journal_dates=["Nov 28, 2025"],
        title="Cool Video",
    ).replace("URL: https://example.com/article", "URL: https://www.youtube.com/watch?v=abcdefghijk")
    mock_dbx, uploads = _mock_dbx(
        existing_paths=[old_path],
        kh_content=existing,
        journal_content=JOURNAL_WITH_ITEM.replace("Already There", "Cool Video"),
    )
    now = LA.localize(datetime(2026, 8, 22, 15, 0))

    with _patched(
        _youtube_patches(mock_dbx, title="Cool Video"),
        "services.obsidian.add_youtube_link.datetime",
    ):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    kh = _kh_upload(uploads)
    assert kh["path"] == old_path
    assert not any(u["path"].endswith(f"{drifted}.md") for u in uploads)
    assert "readwise_id: 01kb5cap1wy21zp37bc2rjj" in kh["content"]
    assert "readwise_url: https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj" in kh["content"]


def test_reader_youtube_same_day_fills_extras_on_existing_stem():
    """Webhook after iOS share hits the shared stem and only fill-if-empty extras."""
    payload = _reader_youtube_payload()
    mock_dbx, uploads = _mock_dbx(
        kh_exists=True,
        kh_content=_existing_kh(journal_dates=["Nov 28, 2025"], title="Cool Video"),
        journal_content=JOURNAL_WITH_ITEM.replace("Already There", YOUTUBE_STEM),
    )
    now = LA.localize(datetime(2026, 8, 22, 15, 0))

    with _patched(
        _youtube_patches(mock_dbx, title="Cool Video"),
        "services.obsidian.add_youtube_link.datetime",
    ):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert result["action"] == "skipped"
    kh = _kh_upload(uploads)
    assert kh["path"].endswith(f"{YOUTUBE_STEM}.md")
    assert "readwise_id: 01kb5cap1wy21zp37bc2rjj" in kh["content"]
    assert "readwise_url: https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj" in kh["content"]
    assert _journal_upload(uploads) is None


def test_apply_youtube_extra_frontmatter_writes_readwise_url():
    existing = _existing_kh(journal_dates=[JOURNAL_DATE], title="Cool Video")
    mock_dbx, uploads = _mock_dbx(kh_exists=True, kh_content=existing)
    file_path = f"{KH_PATH}/Cool Video.md"

    with patch(
        "services.obsidian.add_youtube_link._get_dropbox_client",
        return_value=mock_dbx,
    ):
        result = apply_youtube_extra_frontmatter(
            file_path,
            {
                "readwise_id": "01readerdoc",
                "readwise_url": "https://read.readwise.io/read/01readerdoc",
            },
        )

    assert result["success"] is True
    assert result["action"] == "updated"
    kh = _kh_upload(uploads)
    assert "readwise_id: 01readerdoc" in kh["content"]
    assert "readwise_url: https://read.readwise.io/read/01readerdoc" in kh["content"]
    assert "- [[Cool Video by" not in kh["content"]


def _save_result(*, status_code=201, success=True, error=None):
    return {
        "success": success,
        "id": None if not success else "01readerdoc",
        "url": None if not success else "https://read.readwise.io/new/read/01readerdoc",
        "error": error,
        "status_code": status_code,
    }


def test_process_youtube_share_saves_video_and_writes_readwise_url():
    """Successful YouTube share POSTs category=video and names KH from Reader list."""
    from main import _process_youtube_link

    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    listed = {"id": "01readerdoc", "title": "Cool Video", "author": "A Channel"}
    with _patched(
        _youtube_patches(mock_dbx, title="Cool Video", transcript="auto captions from a talk"),
        "services.obsidian.add_youtube_link.datetime",
    ), patch(
        "main.save_document",
        return_value=_save_result(status_code=201),
    ) as mock_save, patch("main.get_document", return_value=listed), patch(
        "main._mirror_to_raindrop"
    ):
        _process_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    mock_save.assert_called_once_with(
        "https://www.youtube.com/watch?v=abcdefghijk",
        category="video",
        saved_using="gen-intelligence",
    )
    kh = _kh_upload(uploads)
    assert kh["path"].endswith(f"{YOUTUBE_STEM}.md")
    assert "readwise_url: https://read.readwise.io/read/01readerdoc" in kh["content"]
    assert "readwise_id: 01readerdoc" in kh["content"]
    journal = _journal_upload(uploads)
    assert journal is not None
    assert f"- [[{YOUTUBE_STEM}]]" in journal["content"]


def test_process_youtube_share_200_still_writes_readwise_url():
    from main import _process_youtube_link

    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    listed = {"id": "01readerdoc", "title": "Cool Video", "author": "A Channel"}
    with _patched(
        _youtube_patches(mock_dbx, title="Cool Video", transcript="auto captions from a talk"),
        "services.obsidian.add_youtube_link.datetime",
    ), patch(
        "main.save_document",
        return_value=_save_result(status_code=200),
    ), patch("main.get_document", return_value=listed), patch("main._mirror_to_raindrop"):
        _process_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    kh = _kh_upload(uploads)
    assert "readwise_url: https://read.readwise.io/read/01readerdoc" in kh["content"]
    assert kh["path"].endswith(f"{YOUTUBE_STEM}.md")


def test_process_youtube_share_save_failure_falls_back_to_youtube_stem():
    from main import _process_youtube_link

    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    with _patched(
        _youtube_patches(mock_dbx, title="Cool Video", transcript="auto captions from a talk"),
        "services.obsidian.add_youtube_link.datetime",
    ), patch(
        "main.save_document",
        return_value=_save_result(success=False, status_code=500, error="boom"),
    ) as mock_save, patch("main.get_document") as mock_get, patch(
        "main._mirror_to_raindrop"
    ) as mock_raindrop:
        _process_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    mock_save.assert_called_once()
    mock_get.assert_not_called()
    mock_raindrop.assert_called_once()
    kh = _kh_upload(uploads)
    assert kh["path"].endswith(f"{YOUTUBE_STEM}.md")
    assert "readwise_url" not in kh["content"]
    journal = _journal_upload(uploads)
    assert journal is not None
    assert f"- [[{YOUTUBE_STEM}]]" in journal["content"]


def test_process_youtube_share_no_transcript_skips_reader_and_writes_kh():
    """No usable transcript: skip Reader, still write KH + buffet from YouTube title."""
    from main import _process_youtube_link

    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    with _patched(
        _youtube_patches(mock_dbx, title="Cool Video", transcript=None),
        "services.obsidian.add_youtube_link.datetime",
    ), patch("main.save_document") as mock_save, patch("main.get_document") as mock_get, patch(
        "main._mirror_to_raindrop"
    ) as mock_raindrop:
        _process_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    mock_save.assert_not_called()
    mock_get.assert_not_called()
    mock_raindrop.assert_called_once()
    kh = _kh_upload(uploads)
    assert kh["path"].endswith(f"{YOUTUBE_STEM}.md")
    assert "readwise_id" not in kh["content"]
    assert "readwise_url" not in kh["content"]
    journal = _journal_upload(uploads)
    assert journal is not None
    assert f"- [[{YOUTUBE_STEM}]]" in journal["content"]


def test_process_youtube_share_fetches_transcript_once():
    from main import _process_youtube_link

    mock_dbx, uploads = _mock_dbx(kh_exists=False)
    listed = {"id": "01readerdoc", "title": "Cool Video", "author": "A Channel"}
    with _patched(
        [
            patch("services.obsidian.add_youtube_link._get_dropbox_client", return_value=mock_dbx),
            patch("services.obsidian.add_youtube_link._find_knowledge_hub_path", return_value=KH_PATH),
            patch(
                "services.obsidian.add_youtube_link.fetch_youtube_metadata",
                return_value={"title": "Cool Video", "author_name": "A Channel", "description": None},
            ),
            patch("services.obsidian.add_youtube_link._extract_people", return_value=[]),
            patch("services.obsidian.add_readwise_buffet._resolve_journal_folder", return_value=JOURNAL_FOLDER),
        ],
        "services.obsidian.add_youtube_link.datetime",
    ), patch(
        "services.obsidian.add_youtube_link._fetch_transcript",
        return_value="auto captions from a talk",
    ) as mock_fetch, patch(
        "main.save_document",
        return_value=_save_result(status_code=201),
    ), patch("main.get_document", return_value=listed), patch("main._mirror_to_raindrop"):
        _process_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    assert mock_fetch.call_count == 1
    kh = _kh_upload(uploads)
    assert "readwise_id: 01readerdoc" in kh["content"]
