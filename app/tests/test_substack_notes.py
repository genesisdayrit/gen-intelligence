"""Substack Notes handling on the Readwise webhook pipeline."""

import os
import sys
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import dropbox
import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")
os.environ.setdefault("READWISE_TOKEN", "test-readwise-token")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from services.obsidian.add_readwise_buffet import (  # noqa: E402
    ARTICLE_HIGHLIGHTS_HEADER,
    BOOKMARKED_TWEETS_HEADER,
    _format_article_page_bullet,
    _format_tweet_page_bullet,
    _is_article_highlight,
    _is_tweet_book,
    _is_x_article_book,
    _resolve_highlight_book,
    _tweet_wikilink_target,
    append_readwise_buffet,
    clear_book_cache,
    format_readwise_bullet,
)
from services.obsidian.add_shared_link import _extract_frontmatter  # noqa: E402
from services.obsidian.utils.substack_note import (  # noqa: E402
    BOOKMARKED_SUBSTACK_NOTES_HEADER,
    SUBSTACK_NOTE_TITLE_SEPARATOR,
    attach_substack_titles,
    author_from_comment_title,
    canonical_substack_note_url,
    clear_substack_title_cache,
    compress_summary_to_title,
    fallback_substack_summary,
    generate_substack_summary,
    html_to_text,
    is_substack_note_url,
    maybe_rename_reader_document,
    new_substack_page_markdown,
    parse_substack_note_url,
    resolve_substack_note_author,
    sanitize_substack_ai_title,
    substack_note_full_title,
    title_already_substack_note,
)

LA = pytz.timezone("America/Los_Angeles")
KH_FOLDER = "/obsidian/personal/01_knowledge-hub"
JOURNAL_NOV_PATH = "/obsidian/personal/01_daily/_journal/Nov 20, 2026.md"
SUBSTACK_PAGE_PATH = f"{KH_FOLDER}/Substack Notes from Celine Nguyen.md"
TWEET_PAGE_PATH = f"{KH_FOLDER}/Tweets from @georgiedorothea.md"
CELINE_URL = "https://substack.com/@celinenguyen/note/c-390742430"
AI_TITLE = "Taste And Judgment"
FULL_TITLE = f"{AI_TITLE}{SUBSTACK_NOTE_TITLE_SEPARATOR}Celine Nguyen"
DOC_ID = "01substackcelinenote001"
DEFAULT_DROPBOX_REV = "aaaaaaaaaaaaaaaa"

SAMPLE_JOURNAL = """---
date: 2026-11-20
---

### Content Buffet:
- 

### Content Planning
- plan something
"""

NOTE_HTML = (
    "<p>Most design advice is really taste advice dressed up as process.</p>"
    "<p>Judgment is the whole job.</p>"
)


@pytest.fixture(autouse=True)
def _reset_caches():
    clear_book_cache()
    clear_substack_title_cache()
    yield
    clear_book_cache()
    clear_substack_title_cache()


def _download_with_rev(content: str, *, rev: str = DEFAULT_DROPBOX_REV, path: str | None = None):
    metadata = MagicMock()
    metadata.rev = rev
    metadata.path_display = path
    response = MagicMock()
    response.content = content.encode("utf-8")
    return metadata, response


def _substack_document(**overrides):
    data = {
        "id": DOC_ID,
        "url": f"https://read.readwise.io/read/{DOC_ID}",
        "title": "Celine Nguyen's comment",
        "author": "Celine Nguyen",
        "site_name": "Substack",
        "source": "Readwise web highlighter",
        "source_url": CELINE_URL,
        "category": "article",
        "parent_id": None,
        "summary": "A short note on taste and judgment in design.",
        "html_content": NOTE_HTML,
        "content": None,
        "notes": "",
        "created_at": "2026-11-20T18:02:02.213618+00:00",
        "saved_at": "2026-11-20T18:02:02.173000+00:00",
        "updated_at": "2026-11-20T18:02:10.648147+00:00",
        "event_type": "reader.any_document.created",
        "secret": "test-readwise-secret",
    }
    data.update(overrides)
    return data


def _substack_highlight(**overrides):
    data = {
        "id": 990001,
        "text": "Judgment is the whole job.",
        "note": "",
        "highlighted_at": "2026-11-20T19:00:00.000000Z",
        "url": None,
        "book_id": 4242,
        "title": "Celine Nguyen's comment",
        "author": "Celine Nguyen",
        "category": "article",
        "source": "reader",
        "source_url": CELINE_URL,
        "event_type": "readwise.highlight.created",
        "secret": "test-readwise-secret",
    }
    data.update(overrides)
    return data


def _tweet_highlight(**overrides):
    data = {
        "id": 954480,
        "text": "Most Amazing Highlight Ever",
        "note": "",
        "highlighted_at": "2026-11-20T18:55:56.719036Z",
        "url": None,
        "book_id": 8237,
        "title": "Tweets From Georgie Dorothea",
        "author": "@georgiedorothea on Twitter",
        "category": "tweets",
        "source": "twitter",
        "source_url": "https://twitter.com/georgiedorothea",
        "event_type": "readwise.highlight.created",
        "secret": "test-readwise-secret",
    }
    data.update(overrides)
    return data


def _x_article_highlight(**overrides):
    data = {
        "id": 954480,
        "text": "Most Amazing Highlight Ever",
        "note": "",
        "highlighted_at": "2026-11-20T18:55:56.719036Z",
        "url": None,
        "book_id": 8237,
        "title": "The Economics of Good Luck",
        "author": "Index Network",
        "category": "tweets",
        "source_url": "https://x.com/indexnetwork_/status/2118382567032508430/?rw_tt_thread=True",
        "event_type": "readwise.highlight.created",
        "secret": "test-readwise-secret",
    }
    data.update(overrides)
    return data


def _mock_vault_dbx(files_by_path=None):
    store = dict(files_by_path or {})
    uploaded = []
    moved = []
    mock_dbx = MagicMock()

    def download(path):
        if path not in store:
            raise FileNotFoundError(f"not found: {path}")
        return _download_with_rev(store[path], path=path)

    def upload(data, path, mode=None, autorename=None):
        text = data.decode("utf-8")
        store[path] = text
        uploaded.append({"path": path, "content": text, "mode": mode, "autorename": autorename})

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

    def search_v2(query, options=None, **_kwargs):
        result = MagicMock()
        result.has_more = False
        matches = []
        q = str(query or "")
        scope = getattr(options, "path", None) if options is not None else None
        prefix = (str(scope).rstrip("/") + "/") if isinstance(scope, str) and scope else None
        for file_path, content in store.items():
            if prefix and not (
                file_path == str(scope).rstrip("/") or file_path.startswith(prefix)
            ):
                continue
            name = file_path.rsplit("/", 1)[-1]
            if not name.endswith(".md"):
                continue
            if q not in f"{file_path}\n{content}":
                continue
            match = MagicMock()
            meta = MagicMock()
            meta.name = name
            meta.path_display = file_path
            meta.path_lower = file_path
            match.metadata.get_metadata.return_value = meta
            matches.append(match)
        result.matches = matches
        return result

    def get_metadata(path):
        if path not in store:
            raise dropbox.exceptions.ApiError("req", MagicMock(), "", "")
        meta = MagicMock()
        meta.path_display = path
        meta.path_lower = path
        meta.name = path.rsplit("/", 1)[-1]
        return meta

    def move_v2(from_path, to_path, autorename=None):
        if from_path not in store:
            raise FileNotFoundError(from_path)
        if to_path in store:
            raise dropbox.exceptions.ApiError("req", MagicMock(), "", "")
        store[to_path] = store.pop(from_path)
        moved.append({"from": from_path, "to": to_path, "autorename": autorename})

    mock_dbx.files_download.side_effect = download
    mock_dbx.files_upload.side_effect = upload
    mock_dbx.files_list_folder.side_effect = list_folder
    mock_dbx.files_search_v2.side_effect = search_v2
    mock_dbx.files_get_metadata.side_effect = get_metadata
    mock_dbx.files_move_v2.side_effect = move_v2
    mock_dbx.files_create_folder_v2.return_value = MagicMock()
    return mock_dbx, uploaded, store, moved


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
    ), patch(
        "services.obsidian.utils.substack_note._openai_substack_summary",
        return_value=AI_TITLE,
    ), patch(
        "services.readwise.reader.update_document_title",
        return_value={"success": True, "error": None, "status_code": 200},
    ) as mock_patch:
        yield mock_patch


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        CELINE_URL,
        "https://www.substack.com/@celinenguyen/note/c-390742430",
        "http://substack.com/@celinenguyen/note/c-390742430",
        "https://substack.com/@celinenguyen/note/c-390742430/",
        "https://substack.com/@celinenguyen/note/c-390742430?utm_source=share",
        "https://substack.com/@celinenguyen/note/c-390742430#comment",
        "https://www.substack.com/@celinenguyen/note/c-390742430?utm=1#frag",
    ],
)
def test_substack_note_url_matches_www_query_fragment(url):
    assert is_substack_note_url(url) is True
    ref = parse_substack_note_url(url)
    assert ref.handle == "celinenguyen"
    assert ref.note_id == "390742430"
    assert canonical_substack_note_url(url) == CELINE_URL


@pytest.mark.parametrize(
    "url",
    [
        "https://substack.com/@celinenguyen/p/a-real-post",
        "https://substack.com/@celinenguyen/p/c-390742430",
        "https://substack.com/home/post/c-390742430",
        "https://substack.com/home/pub/post/c-390742430",
        "https://celinenguyen.substack.com/p/a-post",
        "https://celinenguyen.substack.com/note/c-390742430",
        "https://twitter.com/celinenguyen/status/1",
        "https://read.readwise.io/read/01abc",
        "",
        None,
    ],
)
def test_substack_note_url_rejects_posts_and_home_share_links(url):
    assert is_substack_note_url(url) is False
    assert parse_substack_note_url(url) is None
    assert canonical_substack_note_url(url) is None


# ---------------------------------------------------------------------------
# Author + title
# ---------------------------------------------------------------------------


def test_author_prefers_display_name_then_comment_title_then_handle():
    assert resolve_substack_note_author(
        display_name="Celine Nguyen",
        title="Someone's comment",
        handle="celinenguyen",
    ) == "Celine Nguyen"
    assert author_from_comment_title("Celine Nguyen's comment") == "Celine Nguyen"
    assert resolve_substack_note_author(
        display_name=None,
        title="Celine Nguyen's comment",
        handle="celinenguyen",
    ) == "Celine Nguyen"
    assert resolve_substack_note_author(
        display_name=None,
        title="A generic article",
        handle="celinenguyen",
    ) == "celinenguyen"


def test_full_title_uses_space_hyphen_space_separator():
    assert SUBSTACK_NOTE_TITLE_SEPARATOR == " - Substack Note by "
    assert substack_note_full_title(AI_TITLE, "Celine Nguyen") == FULL_TITLE
    assert title_already_substack_note(FULL_TITLE) is True
    assert title_already_substack_note("Celine Nguyen's comment") is False


def test_sanitize_ai_title_enforces_three_to_seven_words():
    assert sanitize_substack_ai_title("taste and judgment") == "Taste And Judgment"
    assert sanitize_substack_ai_title('"Taste And Judgment."') == "Taste And Judgment"
    assert sanitize_substack_ai_title("Too Short") is None
    assert sanitize_substack_ai_title("One") is None
    assert (
        sanitize_substack_ai_title("This Title Has Eight Whole Words In It") is None
    )
    assert (
        sanitize_substack_ai_title("Good Taste Beats Process Every Time")
        == "Good Taste Beats Process Every Time"
    )


def test_ai_failure_falls_back_to_compressed_summary_then_generic_title():
    summary = "A short note on taste and judgment in design work today."
    compressed = compress_summary_to_title(summary)
    assert compressed is not None
    assert 3 <= len(compressed.split()) <= 7
    assert fallback_substack_summary(
        reader_summary=summary,
        generic_title="Celine Nguyen's comment",
    ) == compressed
    assert fallback_substack_summary(
        reader_summary="Hi",
        generic_title="Celine Nguyen's comment",
    ) == "Celine Nguyen's Comment"
    assert fallback_substack_summary(reader_summary=None, generic_title=None) == "Substack Note"
    with patch(
        "services.obsidian.utils.substack_note._openai_substack_summary",
        return_value=None,
    ):
        assert generate_substack_summary(
            note_text="Most design advice is really taste advice.",
            reader_summary="A short note on taste and judgment in design work today.",
            generic_title="Celine Nguyen's comment",
        ) == compressed


def test_html_to_text_strips_tags():
    assert html_to_text(NOTE_HTML) == (
        "Most design advice is really taste advice dressed up as process. "
        "Judgment is the whole job."
    )


def test_new_substack_page_markdown_mirrors_tweet_yaml():
    from services.obsidian.utils.substack_note import SubstackNoteTitles

    titles = SubstackNoteTitles(
        author="Celine Nguyen",
        handle="celinenguyen",
        summary=AI_TITLE,
        full_title=FULL_TITLE,
        note_text="Judgment is the whole job.",
    )
    entry = (
        f"- {AI_TITLE}\n"
        "  > Judgment is the whole job.\n"
        f"  ([Link]({CELINE_URL}) [Link](https://read.readwise.io/read/{DOC_ID}))"
    )
    markdown = new_substack_page_markdown(titles, entry)
    assert markdown.startswith('---\ntitle: "Substack Notes from Celine Nguyen"\n')
    assert '  - "[[Celine Nguyen]]"' in markdown
    assert 'Substack Profile: "https://substack.com/@celinenguyen"' in markdown
    assert markdown.index("People:") < markdown.index("Substack Profile:")
    assert "# Substack Notes from Celine Nguyen" in markdown
    assert BOOKMARKED_SUBSTACK_NOTES_HEADER in markdown
    frontmatter, _body = _extract_frontmatter(markdown)
    assert frontmatter["People"] == ["[[Celine Nguyen]]"]
    assert frontmatter["Substack Profile"] == "https://substack.com/@celinenguyen"


# ---------------------------------------------------------------------------
# Document path
# ---------------------------------------------------------------------------


def test_reader_document_creates_author_page_and_journal_line():
    mock_dbx, uploaded, store, _moved = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 11, 20, 15, 0))
    with _journal_and_hub(mock_dbx) as mock_patch:
        result = append_readwise_buffet(_substack_document(), now=now)

    assert result["success"] is True
    assert result["action"] == "replaced"
    journal = store[JOURNAL_NOV_PATH]
    assert (
        f"- [[Substack Notes from Celine Nguyen]]: {AI_TITLE} "
        f"([Link](https://read.readwise.io/read/{DOC_ID}))"
    ) in journal
    page = store[SUBSTACK_PAGE_PATH]
    assert 'title: "Substack Notes from Celine Nguyen"' in page
    assert BOOKMARKED_SUBSTACK_NOTES_HEADER in page
    assert f"- {AI_TITLE}" in page
    assert "> Most design advice is really taste advice dressed up as process." in page
    assert CELINE_URL in page
    assert f"https://read.readwise.io/read/{DOC_ID}" in page
    mock_patch.assert_called_once()
    assert mock_patch.call_args.args[0] == DOC_ID
    assert mock_patch.call_args.args[1] == FULL_TITLE
    assert not any("Celine Nguyen's comment by Celine Nguyen" in item["path"] for item in uploaded)
    assert TWEET_PAGE_PATH not in store
    assert not any(ARTICLE_HIGHLIGHTS_HEADER in item["content"] for item in uploaded)


def test_substack_document_does_not_call_add_shared_link():
    mock_dbx, _uploaded, _store, _moved = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 11, 20, 15, 0))
    with _journal_and_hub(mock_dbx), patch(
        "services.obsidian.add_readwise_buffet._create_shared_link"
    ) as mock_share, patch(
        "services.obsidian.add_readwise_buffet.create_bookmark"
    ) as mock_bookmark:
        result = append_readwise_buffet(_substack_document(), now=now)

    assert result["success"] is True
    mock_share.assert_not_called()
    mock_bookmark.assert_not_called()


def test_patch_is_skipped_when_title_already_has_separator():
    mock_dbx, _uploaded, store, _moved = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 11, 20, 15, 0))
    payload = _substack_document(title=FULL_TITLE)
    with _journal_and_hub(mock_dbx) as mock_patch:
        first = append_readwise_buffet(payload, now=now)
        second = append_readwise_buffet(payload, now=now)

    assert first["success"] is True
    assert second["action"] in {"skipped", "replaced"}
    mock_patch.assert_not_called()
    page = store[SUBSTACK_PAGE_PATH]
    assert page.count(CELINE_URL) == 1


def test_second_webhook_does_not_duplicate_or_repatch():
    mock_dbx, uploaded, store, _moved = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 11, 20, 15, 0))
    with _journal_and_hub(mock_dbx) as mock_patch:
        first = append_readwise_buffet(_substack_document(), now=now)
        second = append_readwise_buffet(_substack_document(), now=now)

    assert first["action"] == "replaced"
    assert second["action"] == "skipped"
    mock_patch.assert_called_once()
    assert store[SUBSTACK_PAGE_PATH].count(f"- {AI_TITLE}") == 1
    journal = store[JOURNAL_NOV_PATH]
    assert journal.count("Substack Notes from Celine Nguyen") == 1


# ---------------------------------------------------------------------------
# Highlight after document
# ---------------------------------------------------------------------------


def test_highlight_after_document_attaches_to_same_page():
    mock_dbx, uploaded, store, _moved = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 11, 20, 15, 0))
    with _journal_and_hub(mock_dbx) as mock_patch:
        doc = append_readwise_buffet(_substack_document(), now=now)
        highlight = append_readwise_buffet(_substack_highlight(), now=now)

    assert doc["success"] is True
    assert highlight["success"] is True
    assert SUBSTACK_PAGE_PATH in store
    assert store[SUBSTACK_PAGE_PATH].count(CELINE_URL) == 1
    assert TWEET_PAGE_PATH not in store
    assert not any("Tweets from" in item["path"] for item in uploaded)
    assert not any(ARTICLE_HIGHLIGHTS_HEADER in item["content"] for item in uploaded)
    assert ARTICLE_HIGHLIGHTS_HEADER not in store[SUBSTACK_PAGE_PATH]
    mock_patch.assert_called_once()
    book = _resolve_highlight_book(_substack_highlight())
    assert _is_tweet_book(book) is False
    assert _is_article_highlight(book, _substack_highlight()) is False
    assert _format_tweet_page_bullet(_substack_highlight(), book) is None
    assert _format_article_page_bullet(_substack_highlight(), book) is None


def test_highlight_journal_line_uses_ai_title_not_quote():
    mock_dbx, _uploaded, store, _moved = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 11, 20, 15, 0))
    with _journal_and_hub(mock_dbx):
        append_readwise_buffet(_substack_highlight(), now=now)

    line = [
        ln for ln in store[JOURNAL_NOV_PATH].splitlines() if "Substack Notes from" in ln
    ][0]
    assert line == (
        f"- [[Substack Notes from Celine Nguyen]]: {AI_TITLE} "
        "([Link](https://readwise.io/open/990001))"
    )
    assert '"Judgment is the whole job."' not in line


# ---------------------------------------------------------------------------
# PATCH guard
# ---------------------------------------------------------------------------


def test_maybe_rename_reader_document_patches_once():
    payload = _substack_document()
    with patch(
        "services.obsidian.utils.substack_note._openai_substack_summary",
        return_value=AI_TITLE,
    ):
        titles = attach_substack_titles(payload)
    assert titles is not None
    with patch(
        "services.readwise.reader.update_document_title",
        return_value={"success": True, "error": None, "status_code": 200},
    ) as mock_patch:
        maybe_rename_reader_document(payload, titles)
        maybe_rename_reader_document(payload, titles)
        renamed = {**payload, "title": titles.full_title}
        maybe_rename_reader_document(renamed, titles)
    mock_patch.assert_called_once()
    assert mock_patch.call_args.kwargs == {} or mock_patch.call_args.args[1] == FULL_TITLE


# ---------------------------------------------------------------------------
# Tweet / X article isolation
# ---------------------------------------------------------------------------


def test_tweet_path_unaffected_by_substack_handling():
    mock_dbx, uploaded, store, _moved = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 11, 20, 15, 0))
    payload = _tweet_highlight()
    book = _resolve_highlight_book(payload)
    assert _is_tweet_book(book) is True
    assert _tweet_wikilink_target(book) == "Tweets from @georgiedorothea"
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert TWEET_PAGE_PATH in store
    assert SUBSTACK_PAGE_PATH not in store
    assert BOOKMARKED_TWEETS_HEADER in store[TWEET_PAGE_PATH]
    assert BOOKMARKED_SUBSTACK_NOTES_HEADER not in store[TWEET_PAGE_PATH]
    assert (
        '- [[Tweets from @georgiedorothea]]: '
        '"Most Amazing Highlight Ever" ([Link](https://readwise.io/open/954480))'
    ) in store[JOURNAL_NOV_PATH]
    assert not any("Substack Notes from" in item["path"] for item in uploaded)


def test_x_article_path_unaffected_by_substack_handling():
    mock_dbx, uploaded, store, _moved = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 11, 20, 15, 0))
    payload = _x_article_highlight()
    book = _resolve_highlight_book(payload)
    assert _is_tweet_book(book) is False
    assert _is_x_article_book(book) is True
    assert _is_article_highlight(book, payload) is True
    with _journal_and_hub(mock_dbx):
        result = append_readwise_buffet(payload, now=now)

    assert result["success"] is True
    assert SUBSTACK_PAGE_PATH not in store
    assert TWEET_PAGE_PATH not in store
    assert not any("Substack Notes from" in item["path"] for item in uploaded)
    assert any(ARTICLE_HIGHLIGHTS_HEADER in item["content"] for item in uploaded)
    journal = store[JOURNAL_NOV_PATH]
    assert "The Economics of Good Luck by Index Network" in journal
    assert "Substack Notes from" not in journal


def test_generic_article_document_still_uses_shared_link():
    mock_dbx, _uploaded, _store, _moved = _mock_vault_dbx({JOURNAL_NOV_PATH: SAMPLE_JOURNAL})
    now = LA.localize(datetime(2026, 11, 20, 15, 0))
    payload = {
        "id": "01kb5cap1wy21zp37bc2rjj",
        "url": "https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj",
        "title": "Our Black Friday sale ends soon",
        "author": "The Verge",
        "source_url": "https://www.theverge.com/black-friday",
        "category": "article",
        "event_type": "reader.any_document.created",
        "created_at": "2026-11-20T18:02:02.213618+00:00",
    }
    with patch(
        "services.obsidian.add_readwise_buffet._create_shared_link",
        return_value={
            "success": True,
            "action": "created",
            "error": None,
            "file_path": "_Knowledge-Hub/Our Black Friday sale ends soon by The Verge.md",
        },
    ) as mock_share, patch(
        "services.obsidian.add_readwise_buffet.create_bookmark",
        return_value={"success": True, "bookmark_id": "1", "error": None},
    ):
        result = append_readwise_buffet(payload, now=now)

    assert result["action"] == "created"
    mock_share.assert_called_once()
