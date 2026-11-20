"""Substack Notes helpers for the Readwise Knowledge Hub pipeline.

Detection, author/title formatting, and the gpt-4o-mini summary call live
here so ``add_readwise_buffet`` can treat Substack notes like tweets:
one author page, a journal buffet line, and no generic article scrape.
"""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass
from typing import NamedTuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SUBSTACK_NOTE_TITLE_SEPARATOR = " - Substack Note by "
BOOKMARKED_SUBSTACK_NOTES_HEADER = "### Bookmarked Substack Notes"
SUBSTACK_NOTE_PAGE_PREFIX = "Substack Notes from "
GPT4O_MINI_MODEL = "gpt-4o-mini"
_AI_TITLE_MIN_WORDS = 3
_AI_TITLE_MAX_WORDS = 7

# ``substack.com/@handle/note/c-<digits>`` only. ``/p/`` posts, custom
# publication hosts, and ``/home/post/c-`` share URLs are not notes.
_SUBSTACK_NOTE_PATH = re.compile(
    r"^/@(?P<handle>[A-Za-z0-9._-]+)/note/c-(?P<note_id>\d+)/?$",
    re.I,
)
_COMMENT_TITLE = re.compile(r"^(?P<name>.+?)'s comment\s*$", re.I)
_HTML_BR = re.compile(r"(?i)<br\s*/?>")
_HTML_BLOCK_CLOSE = re.compile(r"(?i)</(?:p|div|li|h[1-6]|blockquote|tr)>")
_HTML_TAG = re.compile(r"<[^>]+>")
_TRAILING_PUNCT = re.compile(r"[\s.!?,;:]+$")
_WRAPPING_QUOTES = re.compile(r'^["\'“”‘’«»]+|["\'“”‘’«»]+$')
_INLINE_QUOTES = re.compile(r'["“”«»]')

_SUBSTACK_SUMMARY_KEY = "_substack_ai_title"
_SUBSTACK_AUTHOR_KEY = "_substack_author"
_SUBSTACK_HANDLE_KEY = "_substack_handle"
_SUBSTACK_FULL_TITLE_KEY = "_substack_full_title"
_SUBSTACK_NOTE_TEXT_KEY = "_substack_note_text"

# identity (doc id / canonical URL) → already-chosen full Reader title
_title_cache: dict[str, str] = {}
_patched_ids: set[str] = set()


class SubstackNoteRef(NamedTuple):
    """Handle + numeric note id from a canonical Substack note URL."""

    handle: str
    note_id: str


@dataclass(frozen=True)
class SubstackNoteTitles:
    """Resolved display strings for one Substack note."""

    author: str
    handle: str
    summary: str
    full_title: str
    note_text: str | None


def clear_substack_title_cache() -> None:
    """Reset in-process title/PATCH memory (tests + process reuse)."""
    _title_cache.clear()
    _patched_ids.clear()


def _nonempty(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _http_url(*candidates: object) -> str | None:
    for candidate in candidates:
        text = _nonempty(candidate)
        if text and text.startswith(("http://", "https://")):
            return text
    return None


def parse_substack_note_url(url: object) -> SubstackNoteRef | None:
    """Parse ``https://substack.com/@handle/note/c-123`` (www, query, fragment).

    ``/p/`` posts and ``/home/post/c-`` share URLs return None.
    """
    text = _nonempty(url)
    if not text:
        return None
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "substack.com":
        return None
    match = _SUBSTACK_NOTE_PATH.match(parsed.path or "")
    if not match:
        return None
    return SubstackNoteRef(handle=match.group("handle"), note_id=match.group("note_id"))


def is_substack_note_url(url: object) -> bool:
    return parse_substack_note_url(url) is not None


def canonical_substack_note_url(url: object) -> str | None:
    """Scheme-host-path identity with query and fragment stripped."""
    ref = parse_substack_note_url(url)
    if not ref:
        return None
    return f"https://substack.com/@{ref.handle}/note/c-{ref.note_id}"


def substack_profile_url(handle: str) -> str:
    token = handle.lstrip("@").strip()
    return f"https://substack.com/@{token}"


def substack_note_page_title(author: str) -> str:
    return f"{SUBSTACK_NOTE_PAGE_PREFIX}{author}"


def substack_note_full_title(summary: str, author: str) -> str:
    """``{summary} - Substack Note by {author}`` (space-hyphen-space)."""
    return f"{summary}{SUBSTACK_NOTE_TITLE_SEPARATOR}{author}"


def title_already_substack_note(title: object) -> bool:
    text = _nonempty(title)
    return bool(text) and SUBSTACK_NOTE_TITLE_SEPARATOR in text


def summary_from_substack_full_title(title: object) -> str | None:
    """Phrase before `` - Substack Note by ``, or None."""
    text = _nonempty(title)
    if not text or SUBSTACK_NOTE_TITLE_SEPARATOR not in text:
        return None
    summary, _sep, _author = text.partition(SUBSTACK_NOTE_TITLE_SEPARATOR)
    summary = _collapse(summary)
    return summary or None


def html_to_text(value: object) -> str | None:
    """Strip tags from Reader ``html_content`` / ``content``. Stdlib only."""
    raw = _nonempty(value)
    if not raw:
        return None
    text = html.unescape(raw)
    text = _HTML_BR.sub("\n", text)
    text = _HTML_BLOCK_CLOSE.sub("\n", text)
    text = _HTML_TAG.sub("", text)
    text = _collapse(text.replace("\n", " "))
    return text or None


def _title_word(word: str) -> str:
    if not word:
        return word
    if word.isupper() and len(word) > 1:
        return word
    return word[0].upper() + word[1:]


def sanitize_substack_ai_title(text: object) -> str | None:
    """3–7 word Title Case phrase. No quotes, no trailing punctuation."""
    raw = _nonempty(text)
    if not raw:
        return None
    cleaned = _collapse(raw)
    cleaned = _WRAPPING_QUOTES.sub("", cleaned).strip()
    cleaned = _INLINE_QUOTES.sub("", cleaned)
    cleaned = _TRAILING_PUNCT.sub("", cleaned).strip()
    cleaned = _collapse(cleaned)
    if not cleaned:
        return None
    words = [word for word in cleaned.split() if word]
    if not (_AI_TITLE_MIN_WORDS <= len(words) <= _AI_TITLE_MAX_WORDS):
        return None
    titled = " ".join(_title_word(word) for word in words)
    titled = _TRAILING_PUNCT.sub("", titled).strip()
    titled = _WRAPPING_QUOTES.sub("", titled).strip()
    check = titled.split()
    if not (_AI_TITLE_MIN_WORDS <= len(check) <= _AI_TITLE_MAX_WORDS):
        return None
    return titled


def compress_summary_to_title(text: object) -> str | None:
    """First ≤7 words of ``text``, Title Case, if at least 3 remain."""
    raw = _nonempty(text)
    if not raw:
        return None
    cleaned = _collapse(_WRAPPING_QUOTES.sub("", raw))
    cleaned = _INLINE_QUOTES.sub("", cleaned)
    cleaned = _TRAILING_PUNCT.sub("", cleaned).strip()
    words = [word for word in cleaned.split() if word][:_AI_TITLE_MAX_WORDS]
    if len(words) < _AI_TITLE_MIN_WORDS:
        return None
    return sanitize_substack_ai_title(" ".join(words))


def fallback_substack_summary(
    *,
    reader_summary: str | None,
    generic_title: str | None,
) -> str:
    """AI-failure fallback: compressed summary, else the generic title."""
    compressed = compress_summary_to_title(reader_summary)
    if compressed:
        return compressed
    generic = _nonempty(generic_title)
    if generic:
        cleaned = _collapse(_WRAPPING_QUOTES.sub("", generic))
        cleaned = _INLINE_QUOTES.sub("", cleaned)
        cleaned = _TRAILING_PUNCT.sub("", cleaned).strip() or generic.strip()
        sanitized = sanitize_substack_ai_title(cleaned)
        if sanitized:
            return sanitized
        return cleaned
    return "Substack Note"


def author_from_comment_title(title: object) -> str | None:
    """``Celine Nguyen's comment`` → ``Celine Nguyen``."""
    text = _nonempty(title)
    if not text:
        return None
    match = _COMMENT_TITLE.match(_collapse(text))
    if not match:
        return None
    name = match.group("name").strip()
    return name or None


def resolve_substack_note_author(
    *,
    display_name: object = None,
    title: object = None,
    handle: str | None = None,
) -> str | None:
    """Prefer Reader/book display name, then ``X's comment``, then URL handle."""
    from services.obsidian.utils.author_yaml import is_site_host_author, plain_author_label

    named = plain_author_label(display_name)
    if named and not is_site_host_author(named):
        return named
    from_title = author_from_comment_title(title)
    if from_title:
        return from_title
    token = (handle or "").lstrip("@").strip()
    return token or None


def substack_note_urls_from_payload(
    payload: dict,
    book: dict | None = None,
) -> list[str]:
    """Candidate page URLs (source_url first). Highlight open URLs skipped."""
    candidates = [
        payload.get("source_url"),
        (book or {}).get("source_url") if book else None,
        payload.get("url"),
        payload.get("source"),
    ]
    urls: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        href = _http_url(candidate)
        if not href or "readwise.io/open/" in href:
            continue
        key = href.casefold()
        if key in seen:
            continue
        seen.add(key)
        urls.append(href)
    return urls


def substack_note_ref_from_payload(
    payload: dict,
    book: dict | None = None,
) -> SubstackNoteRef | None:
    for href in substack_note_urls_from_payload(payload, book):
        ref = parse_substack_note_url(href)
        if ref:
            return ref
    return None


def is_substack_note_payload(payload: dict, book: dict | None = None) -> bool:
    return substack_note_ref_from_payload(payload, book) is not None


def source_url_from_payload(payload: dict, book: dict | None = None) -> str | None:
    for href in substack_note_urls_from_payload(payload, book):
        canonical = canonical_substack_note_url(href)
        if canonical:
            return canonical
    return None


def note_text_from_payload(payload: dict) -> str | None:
    """Prefer stripped ``html_content``, then ``content``, then highlight text."""
    for key in ("html_content", "content"):
        text = html_to_text(payload.get(key))
        if text:
            return text
    highlight = _nonempty(payload.get("text"))
    if highlight:
        return _collapse(highlight)
    return None


def _identity_keys(payload: dict, book: dict | None = None) -> list[str]:
    keys: list[str] = []
    for source in (payload, book or {}):
        for field in ("id", "readwise_id"):
            value = source.get(field)
            if value is None or value == "":
                continue
            text = str(value).strip()
            if text:
                keys.append(f"id:{text}")
    canonical = source_url_from_payload(payload, book)
    if canonical:
        keys.append(f"url:{canonical}")
    return keys


def cached_full_title(payload: dict, book: dict | None = None) -> str | None:
    for key in _identity_keys(payload, book):
        cached = _title_cache.get(key)
        if cached:
            return cached
    return None


def remember_full_title(payload: dict, full_title: str, book: dict | None = None) -> None:
    for key in _identity_keys(payload, book):
        _title_cache[key] = full_title


def already_patched_document(document_id: object) -> bool:
    text = _nonempty(document_id)
    return bool(text) and text in _patched_ids


def mark_patched_document(document_id: object) -> None:
    text = _nonempty(document_id)
    if text:
        _patched_ids.add(text)


def _openai_substack_summary(note_text: str, reader_summary: str | None) -> str | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.info("Substack note title skipped (OPENAI_API_KEY not set)")
        return None
    parts = [f"Note text:\n{note_text[:4000]}"]
    if reader_summary:
        parts.append(f"Reader summary:\n{reader_summary[:1000]}")
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=GPT4O_MINI_MODEL,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You title a short Substack note. Return a 3-7 word "
                        "Title Case phrase that captures the note's sentiment "
                        "or core idea. No quotes. No trailing punctuation. "
                        "No extra text."
                    ),
                },
                {"role": "user", "content": "\n\n".join(parts)},
            ],
        )
        content = response.choices[0].message.content
        return sanitize_substack_ai_title(content)
    except Exception:
        logger.warning("Substack note AI title failed", exc_info=True)
        return None


def generate_substack_summary(
    *,
    note_text: str | None,
    reader_summary: str | None,
    generic_title: str | None,
) -> str:
    """AI title, else compressed summary, else the generic Reader title."""
    material = note_text or reader_summary
    if material:
        generated = _openai_substack_summary(material, reader_summary)
        if generated:
            return generated
    return fallback_substack_summary(
        reader_summary=reader_summary,
        generic_title=generic_title,
    )


def enrich_payload_html_content(payload: dict) -> None:
    """Fill ``html_content`` from Reader list when the webhook omitted it."""
    if html_to_text(payload.get("html_content")) or html_to_text(payload.get("content")):
        return
    event_type = str(payload.get("event_type") or "")
    if event_type.startswith("readwise.highlight"):
        return
    doc_id = payload.get("id")
    if doc_id is None or doc_id == "":
        return
    if str(doc_id).isdigit():
        return
    try:
        from services.readwise.reader import get_document

        listed = get_document(str(doc_id), with_html_content=True)
    except Exception:
        logger.warning("Substack note html_content fetch failed", exc_info=True)
        return
    if not listed:
        return
    html_content = listed.get("html_content") or listed.get("content")
    if html_content:
        payload["html_content"] = html_content
    if not _nonempty(payload.get("summary")) and _nonempty(listed.get("summary")):
        payload["summary"] = listed.get("summary")


def attach_substack_titles(
    payload: dict,
    book: dict | None = None,
) -> SubstackNoteTitles | None:
    """Resolve author + AI title once per document/URL. Mutates payload cache keys."""
    ref = substack_note_ref_from_payload(payload, book)
    if not ref:
        return None
    existing = (
        _nonempty(payload.get(_SUBSTACK_SUMMARY_KEY))
        and _nonempty(payload.get(_SUBSTACK_AUTHOR_KEY))
        and _nonempty(payload.get(_SUBSTACK_FULL_TITLE_KEY))
    )
    if existing:
        return SubstackNoteTitles(
            author=payload[_SUBSTACK_AUTHOR_KEY],
            handle=payload.get(_SUBSTACK_HANDLE_KEY) or ref.handle,
            summary=payload[_SUBSTACK_SUMMARY_KEY],
            full_title=payload[_SUBSTACK_FULL_TITLE_KEY],
            note_text=payload.get(_SUBSTACK_NOTE_TEXT_KEY),
        )

    title = _nonempty((book or {}).get("title")) or _nonempty(payload.get("title"))
    display = (
        (book or {}).get("author")
        or payload.get("author")
        or payload.get("creator")
    )
    author = resolve_substack_note_author(
        display_name=display,
        title=title,
        handle=ref.handle,
    )
    if not author:
        return None

    cached = cached_full_title(payload, book)
    if cached and title_already_substack_note(cached):
        summary = summary_from_substack_full_title(cached) or fallback_substack_summary(
            reader_summary=_nonempty(payload.get("summary")),
            generic_title=title,
        )
        full_title = cached
    elif title_already_substack_note(title):
        summary = summary_from_substack_full_title(title) or fallback_substack_summary(
            reader_summary=_nonempty(payload.get("summary")),
            generic_title=title,
        )
        full_title = title or substack_note_full_title(summary, author)
    else:
        enrich_payload_html_content(payload)
        note_text = note_text_from_payload(payload)
        summary = generate_substack_summary(
            note_text=note_text,
            reader_summary=_nonempty(payload.get("summary")),
            generic_title=title,
        )
        full_title = substack_note_full_title(summary, author)
        remember_full_title(payload, full_title, book)

    note_text = note_text_from_payload(payload) or _nonempty(payload.get("summary"))
    payload[_SUBSTACK_SUMMARY_KEY] = summary
    payload[_SUBSTACK_AUTHOR_KEY] = author
    payload[_SUBSTACK_HANDLE_KEY] = ref.handle
    payload[_SUBSTACK_FULL_TITLE_KEY] = full_title
    if note_text:
        payload[_SUBSTACK_NOTE_TEXT_KEY] = note_text
    remember_full_title(payload, full_title, book)
    return SubstackNoteTitles(
        author=author,
        handle=ref.handle,
        summary=summary,
        full_title=full_title,
        note_text=note_text,
    )


def maybe_rename_reader_document(payload: dict, titles: SubstackNoteTitles) -> None:
    """PATCH Reader title once. Non-fatal. Skip if already renamed/patched."""
    doc_id = payload.get("id")
    if doc_id is None or doc_id == "":
        return
    event_type = str(payload.get("event_type") or "")
    if event_type.startswith("readwise.highlight"):
        return
    doc_key = str(doc_id).strip()
    if not doc_key or doc_key.isdigit():
        return
    if already_patched_document(doc_key):
        return
    current = _nonempty(payload.get("title"))
    if title_already_substack_note(current):
        mark_patched_document(doc_key)
        return
    from services.readwise.reader import update_document_title

    result = update_document_title(doc_key, titles.full_title)
    mark_patched_document(doc_key)
    if not result.get("success"):
        logger.warning(
            "Reader Substack title PATCH failed id=%s error=%s",
            doc_key,
            result.get("error"),
        )
    else:
        payload["title"] = titles.full_title


def format_substack_journal_bullet(
    titles: SubstackNoteTitles,
    link_url: str | None,
) -> str:
    """``- [[Substack Notes from {author}]]: {AI title} ([Link](url))``."""
    target = substack_note_page_title(titles.author)
    line = f"- [[{target}]]: {titles.summary}"
    if link_url:
        line += f" ([Link]({link_url}))"
    return line


def format_substack_page_entry(
    titles: SubstackNoteTitles,
    *,
    source_url: str | None,
    readwise_url: str | None,
) -> str:
    """Author-page entry: AI title, blockquote, Substack + Readwise links."""
    lines = [f"- {titles.summary}"]
    if titles.note_text:
        lines.append(f"  > {titles.note_text}")
    links: list[str] = []
    if source_url:
        links.append(f"[Link]({source_url})")
    if readwise_url and readwise_url != source_url:
        links.append(f"[Link]({readwise_url})")
    if links:
        lines.append("  (" + " ".join(links) + ")")
    return "\n".join(lines)


def new_substack_page_markdown(
    titles: SubstackNoteTitles,
    entry: str,
) -> str:
    """Minimal author page: YAML title + People + Substack Profile, H1, first entry."""
    page_title = substack_note_page_title(titles.author)
    people_link = f"[[{titles.author}]]"
    profile = substack_profile_url(titles.handle)
    return (
        f"---\n"
        f'title: "{page_title}"\n'
        f"People:\n"
        f'  - "{people_link}"\n'
        f'Substack Profile: "{profile}"\n'
        f"---\n"
        f"\n"
        f"# {page_title}\n"
        f"\n"
        f"{BOOKMARKED_SUBSTACK_NOTES_HEADER}\n"
        f"{entry}\n"
    )


def substack_dedup_keys(
    payload: dict,
    book: dict | None = None,
    *,
    source_url: str | None = None,
) -> list[str]:
    """Source URL + Readwise ids so repeat webhooks do not duplicate."""
    keys: list[str] = []
    canonical = source_url or source_url_from_payload(payload, book)
    if canonical:
        keys.append(canonical)
    doc_id = payload.get("id")
    if doc_id is not None and doc_id != "":
        keys.append(str(doc_id))
        keys.append(f"https://read.readwise.io/read/{doc_id}")
    highlight_id = payload.get("highlight_id") or (
        payload.get("id") if payload.get("book_id") is not None else None
    )
    if highlight_id is not None and highlight_id != "" and payload.get("book_id") is not None:
        keys.append(f"https://readwise.io/open/{highlight_id}")
    url = _http_url(payload.get("url"))
    if url and url not in keys:
        keys.append(url)
    return keys
