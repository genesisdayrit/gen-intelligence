"""Knowledge Hub YAML author wikilink helper and write-path tests."""

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
    append_readwise_buffet,
    format_readwise_bullet,
    journal_filename,
    knowledge_hub_note_stem,
    reader_document_extra_frontmatter,
    reader_knowledge_hub_note_stem,
)
from services.obsidian.add_shared_link import (  # noqa: E402
    _merge_extra_frontmatter,
    add_shared_link,
)
from services.obsidian import add_shared_link as shared_mod  # noqa: E402
from services.obsidian.add_youtube_link import add_youtube_link  # noqa: E402
from services.obsidian.utils.author_yaml import (  # noqa: E402
    author_frontmatter_value,
    author_yaml_literal,
    is_plain_to_wikilink_author_upgrade,
    plain_author_label,
    split_author_names,
)
from services.obsidian.utils.date_helpers import get_effective_date  # noqa: E402

LA = pytz.timezone("America/Los_Angeles")
FIXED_LOCAL = LA.localize(datetime(2026, 8, 22, 15, 0))
FIXED_UTC = FIXED_LOCAL.astimezone(timezone.utc)
JOURNAL_DATE = journal_filename(get_effective_date(FIXED_LOCAL)).removesuffix(".md")
KH_PATH = "/obsidian/personal/_knowledge-hub"
JOURNAL_FOLDER = "/obsidian/personal/01_daily/_journal"

SAMPLE_JOURNAL = """---
date: 2026-08-22
---

### Content Buffet:
- 

### Content Planning
- plan something
"""


def _existing_kh(*, journal_dates: list[str], title: str, author_line: str) -> str:
    journal_yaml = "\n".join(f'  - "[[{d}]]"' for d in journal_dates)
    return f"""---
Journal:
{journal_yaml}
created time: 2026-01-01T00:00:00+00:00
modified time: 2026-01-01T00:00:00+00:00
key words:
People:
URL: https://example.com/article
author: {author_line}
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


def _mock_dbx(*, kh_exists=False, kh_content=None, journal_content=SAMPLE_JOURNAL):
    uploads: list[dict] = []
    mock_dbx = MagicMock()

    def get_metadata(path):
        if KH_PATH in path:
            if kh_exists:
                return MagicMock()
            raise _not_found_api_error()
        raise _not_found_api_error()

    def download(path):
        response = MagicMock()
        if JOURNAL_FOLDER in path:
            response.content = journal_content.encode("utf-8")
            return None, response
        response.content = (kh_content or "").encode("utf-8")
        return None, response

    def capture_upload(data, path, mode=None):
        uploads.append({"content": data.decode("utf-8"), "path": path, "mode": mode})

    mock_dbx.files_get_metadata.side_effect = get_metadata
    mock_dbx.files_download.side_effect = download
    mock_dbx.files_upload.side_effect = capture_upload
    return mock_dbx, uploads


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


def _youtube_patches(mock_dbx, title="Cool Video"):
    return [
        patch("services.obsidian.add_youtube_link._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_youtube_link._find_knowledge_hub_path", return_value=KH_PATH),
        patch(
            "services.obsidian.add_youtube_link.fetch_youtube_metadata",
            return_value={"title": title, "author_name": "A Channel", "description": None},
        ),
        patch("services.obsidian.add_youtube_link._extract_people", return_value=[]),
        patch("services.obsidian.add_youtube_link._fetch_transcript", return_value=None),
        patch("services.obsidian.add_readwise_buffet._resolve_journal_folder", return_value=JOURNAL_FOLDER),
    ]


@contextmanager
def _patched(patches, datetime_target=None):
    with ExitStack() as stack:
        if datetime_target:
            mock_dt = stack.enter_context(patch(datetime_target))
            mock_dt.now.return_value = FIXED_UTC
        for p in patches:
            stack.enter_context(p)
        yield


def _kh_upload(uploads: list[dict]) -> dict:
    matches = [u for u in uploads if KH_PATH in u["path"]]
    assert matches, f"expected Knowledge Hub upload, got {uploads}"
    return matches[-1]


def _journal_upload(uploads: list[dict]) -> dict | None:
    matches = [u for u in uploads if JOURNAL_FOLDER in u["path"]]
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_single_author_is_quoted_wikilink():
    assert author_frontmatter_value("W. Brian Arthur") == "[[W. Brian Arthur]]"
    assert author_yaml_literal("W. Brian Arthur") == '"[[W. Brian Arthur]]"'


def test_two_authors_joined_with_and():
    assert author_frontmatter_value("Jane Doe and John Smith") == (
        "[[Jane Doe]], [[John Smith]]"
    )
    assert author_yaml_literal("Jane Doe and John Smith") == (
        '"[[Jane Doe]], [[John Smith]]"'
    )


def test_two_full_names_split_on_comma():
    assert split_author_names("Alice Smith, Bob Jones") == ["Alice Smith", "Bob Jones"]
    assert author_frontmatter_value("Alice Smith, Bob Jones") == (
        "[[Alice Smith]], [[Bob Jones]]"
    )


def test_zuckerberg_role_suffix_stays_one_wikilink():
    raw = "Mark Zuckerberg, Founder and CEO, Meta"
    assert split_author_names(raw) == [raw]
    assert author_frontmatter_value(raw) == f"[[{raw}]]"
    assert "[[Founder]]" not in author_frontmatter_value(raw)
    assert "[[Meta]]" not in author_frontmatter_value(raw)


def test_tweet_handle_author_stays_plain():
    raw = "@georgiedorothea on Twitter"
    assert author_frontmatter_value(raw) == raw
    assert author_frontmatter_value(raw, is_tweet=True) == raw
    assert "[[" not in author_yaml_literal(raw)


def test_tweet_book_author_left_alone_when_flagged():
    assert author_frontmatter_value("Klaas", is_tweet=True) == "Klaas"
    assert "[[" not in author_frontmatter_value("Klaas", is_tweet=True)


def test_plain_author_label_never_includes_brackets():
    assert plain_author_label("W. Brian Arthur") == "W. Brian Arthur"
    assert plain_author_label("[[W. Brian Arthur]]") == "W. Brian Arthur"
    assert plain_author_label("[[Alice Smith]], [[Bob Jones]]") == "Alice Smith, Bob Jones"
    assert "[[" not in (plain_author_label("[[Jane Doe]]") or "")


def test_reader_stem_title_by_author_stays_plain():
    stem = reader_knowledge_hub_note_stem("Increasing Returns", "W. Brian Arthur")
    assert stem == "Increasing Returns by W. Brian Arthur"
    assert "[[" not in stem
    assert reader_knowledge_hub_note_stem(
        "Increasing Returns", "[[W. Brian Arthur]]"
    ) == "Increasing Returns by W. Brian Arthur"


def test_highlight_bullet_does_not_wikilink_author():
    line = format_readwise_bullet(
        {
            "id": 954480,
            "text": "A quote",
            "title": "Increasing Returns",
            "author": "W. Brian Arthur",
            "event_type": "readwise.highlight.created",
            "book_id": 8237,
        }
    )
    assert line == (
        '- [[Increasing Returns by W. Brian Arthur]]: '
        '"A quote" ([Link](https://readwise.io/open/954480))'
    )
    assert "by [[W. Brian Arthur]]" not in line
    assert "[[W. Brian Arthur]]" not in line


def test_junk_and_empty_author_skipped():
    assert author_frontmatter_value("") is None
    assert author_frontmatter_value("   ") is None
    assert author_frontmatter_value("|||") is None
    assert author_yaml_literal(None) == ""


def test_idempotent_when_already_wikilinked():
    assert author_frontmatter_value("[[W. Brian Arthur]]") == "[[W. Brian Arthur]]"
    assert author_frontmatter_value("[[Alice Smith]], [[Bob Jones]]") == (
        "[[Alice Smith]], [[Bob Jones]]"
    )


def test_plain_to_wikilink_upgrade_same_authors_only():
    assert is_plain_to_wikilink_author_upgrade(
        "W. Brian Arthur", "[[W. Brian Arthur]]"
    )
    assert is_plain_to_wikilink_author_upgrade(
        "Alice Smith, Bob Jones", "[[Alice Smith]], [[Bob Jones]]"
    )
    assert not is_plain_to_wikilink_author_upgrade(
        "Casey Newton", "[[The Verge]]"
    )
    assert not is_plain_to_wikilink_author_upgrade(
        "[[W. Brian Arthur]]", "[[W. Brian Arthur]]"
    )


def test_merge_does_not_clobber_different_existing_author():
    frontmatter = {"author": "Casey Newton"}
    changed = _merge_extra_frontmatter(frontmatter, {"author": "The Verge"})
    assert changed is False
    assert frontmatter["author"] == "Casey Newton"


def test_merge_upgrades_plain_text_same_author():
    frontmatter = {"author": "W. Brian Arthur"}
    changed = _merge_extra_frontmatter(frontmatter, {"author": "W. Brian Arthur"})
    assert changed is True
    assert frontmatter["author"] == "[[W. Brian Arthur]]"


def test_reader_extra_wikilinks_author_and_leaves_tweet_plain():
    extras = reader_document_extra_frontmatter(
        {
            "id": "01kb5cap1wy21zp37bc2rjj",
            "author": "W. Brian Arthur",
            "source_url": "https://example.com/essay",
            "category": "article",
        }
    )
    assert extras["author"] == "[[W. Brian Arthur]]"

    tweet_extras = reader_document_extra_frontmatter(
        {
            "id": "tweet-1",
            "author": "@georgiedorothea on Twitter",
            "category": "tweets",
            "source": "twitter",
            "source_url": "https://twitter.com/georgiedorothea",
        }
    )
    assert tweet_extras["author"] == "@georgiedorothea on Twitter"


# ---------------------------------------------------------------------------
# Write-path tests
# ---------------------------------------------------------------------------


def test_shared_link_writes_quoted_author_wikilink():
    mock_dbx, uploads = _mock_dbx()
    with _patched(
        _shared_patches(mock_dbx, title="Increasing Returns", author="W. Brian Arthur"),
        "services.obsidian.add_shared_link.datetime",
    ):
        result = add_shared_link("https://example.com/essay", title="Increasing Returns")

    assert result["success"] is True
    kh = _kh_upload(uploads)
    assert 'author: "[[W. Brian Arthur]]"' in kh["content"]
    assert "[[" not in kh["path"]
    assert kh["path"].endswith("Increasing Returns.md")


def test_shared_link_filename_stem_title_by_author_stays_plain():
    """If the note title is already 'Title by Author', the file has no [[."""
    title = "Increasing Returns by W. Brian Arthur"
    mock_dbx, uploads = _mock_dbx()
    with _patched(
        _shared_patches(mock_dbx, title=title, author="W. Brian Arthur"),
        "services.obsidian.add_shared_link.datetime",
    ):
        result = add_shared_link("https://example.com/essay", title=title)

    assert result["success"] is True
    kh = _kh_upload(uploads)
    stem = knowledge_hub_note_stem(title)
    assert stem == title
    assert "[[" not in stem
    assert kh["path"].endswith(f"{stem}.md")
    assert "[[" not in kh["path"]
    assert 'author: "[[W. Brian Arthur]]"' in kh["content"]
    assert f"- [[{stem}]]" in uploads[-1]["content"] or any(
        f"- [[{stem}]]" in u["content"] for u in uploads
    )


def test_reader_extra_two_authors_do_not_enter_filename():
    extras = reader_document_extra_frontmatter(
        {
            "id": "doc-1",
            "title": "Zero to One",
            "author": "Peter Thiel, Blake Masters",
            "source_url": "https://example.com/zero",
        }
    )
    assert extras["author"] == "[[Peter Thiel]], [[Blake Masters]]"
    mock_dbx, uploads = _mock_dbx()
    with _patched(
        _shared_patches(mock_dbx, title="Zero to One", author="Peter Thiel, Blake Masters"),
        "services.obsidian.add_shared_link.datetime",
    ):
        result = add_shared_link(
            "https://example.com/zero",
            title="Zero to One",
            extra_frontmatter=extras,
        )

    assert result["success"] is True
    kh = _kh_upload(uploads)
    assert kh["path"].endswith("Zero to One.md")
    assert "[[" not in kh["path"]
    assert "by [[Peter Thiel]]" not in kh["path"]
    assert 'author: "[[Peter Thiel]], [[Blake Masters]]"' in kh["content"]


def test_fill_if_empty_does_not_clobber_different_author():
    existing = _existing_kh(
        journal_dates=["Jan 1, 2026"],
        title="My Article",
        author_line="Casey Newton",
    )
    mock_dbx, uploads = _mock_dbx(kh_exists=True, kh_content=existing)
    with _patched(
        _shared_patches(mock_dbx, title="My Article", author="The Verge"),
        "services.obsidian.add_shared_link.datetime",
    ):
        result = add_shared_link(
            "https://example.com/article",
            title="My Article",
            extra_frontmatter={"author": "The Verge"},
        )

    assert result["success"] is True
    kh = _kh_upload(uploads)
    assert "Casey Newton" in kh["content"]
    assert "[[The Verge]]" not in kh["content"]


def test_fill_if_empty_upgrades_plain_matching_author():
    existing = _existing_kh(
        journal_dates=["Jan 1, 2026"],
        title="Increasing Returns",
        author_line="W. Brian Arthur",
    )
    mock_dbx, uploads = _mock_dbx(kh_exists=True, kh_content=existing)
    with _patched(
        _shared_patches(mock_dbx, title="Increasing Returns", author="W. Brian Arthur"),
        "services.obsidian.add_shared_link.datetime",
    ):
        result = add_shared_link(
            "https://example.com/essay",
            title="Increasing Returns",
            extra_frontmatter={"author": "W. Brian Arthur"},
        )

    assert result["success"] is True
    kh = _kh_upload(uploads)
    assert "[[W. Brian Arthur]]" in kh["content"]


def test_youtube_extra_author_is_quoted_wikilink():
    mock_dbx, uploads = _mock_dbx()
    with _patched(
        _youtube_patches(mock_dbx, title="Cool Video"),
        "services.obsidian.add_youtube_link.datetime",
    ):
        result = add_youtube_link(
            "https://www.youtube.com/watch?v=abcdefghijk",
            extra_frontmatter={"author": "Jane Doe and John Smith"},
        )

    assert result["success"] is True
    kh = _kh_upload(uploads)
    assert 'author: "[[Jane Doe]], [[John Smith]]"' in kh["content"]
    assert kh["path"].endswith("Cool Video by A Channel.md")
    assert "[[" not in kh["path"]


def test_ios_share_link_filename_stays_title_only():
    mock_dbx, uploads = _mock_dbx()
    with _patched(
        _shared_patches(mock_dbx, title="My Article", author="Alice Smith"),
        "services.obsidian.add_shared_link.datetime",
    ):
        add_shared_link("https://example.com/article", title="My Article")

    kh = _kh_upload(uploads)
    assert kh["path"].endswith("My Article.md")
    assert "Alice" not in kh["path"]


def test_wikilink_authors_only_in_kh_yaml_not_buffet_or_stem():
    """Hard constraint: [[Author]] belongs on YAML author only."""
    payload = {
        "id": "01kb5cap1wy21zp37bc2rjj",
        "url": "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj",
        "title": "Increasing Returns",
        "author": "W. Brian Arthur",
        "source_url": "https://example.com/essay",
        "category": "article",
        "parent_id": None,
        "created_at": "2025-11-28T14:02:02.213618+00:00",
        "saved_at": "2025-11-28T14:02:02.173000+00:00",
        "event_type": "reader.any_document.created",
    }
    stem = reader_knowledge_hub_note_stem(payload["title"], payload["author"])
    assert stem == "Increasing Returns by W. Brian Arthur"
    assert "[[" not in stem
    assert "by [[W. Brian Arthur]]" not in stem

    mock_dbx, uploads = _mock_dbx()
    now = LA.localize(datetime(2026, 8, 22, 15, 0))
    with _patched(
        _shared_patches(mock_dbx, title=payload["title"], author=payload["author"]),
        "services.obsidian.add_shared_link.datetime",
    ):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    kh = _kh_upload(uploads)
    journal = _journal_upload(uploads)
    assert journal is not None
    assert 'author: "[[W. Brian Arthur]]"' in kh["content"]
    assert kh["path"].endswith(f"{stem}.md")
    assert "[[" not in kh["path"]
    section = journal["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    lines = [line for line in section.splitlines() if line.strip()]
    assert lines == [f"- [[{stem}]]"]
    assert "by [[W. Brian Arthur]]" not in section
    assert "  - W. Brian Arthur" not in lines
    assert "  - [[W. Brian Arthur]]" not in lines
    assert "[source]" not in section
    assert "readwise.io" not in section
