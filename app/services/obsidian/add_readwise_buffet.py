"""Append Readwise webhook events to the Obsidian journal Content Buffet."""

import logging
import os
import re
from datetime import datetime
from urllib.parse import urlparse

import dropbox
import pytz
import requests
from dotenv import load_dotenv

from services.obsidian.utils.author_yaml import (
    author_frontmatter_value,
    author_yaml_literal,
    is_plain_to_wikilink_author_upgrade,
    plain_author_label,
    split_author_names,
)
from services.obsidian.utils.date_helpers import get_effective_date
from services.obsidian.utils.tweet_highlight_quote import tweet_highlight_quote
from services.raindrop.client import create_bookmark

load_dotenv()

logger = logging.getLogger(__name__)

CONTENT_BUFFET_HEADER = "### Content Buffet:"
BOOKMARKED_TWEETS_HEADER = "### Bookmarked Tweets"
BOOK_HIGHLIGHTS_HEADER = "### Book highlights"
ARTICLE_HIGHLIGHTS_HEADER = "### Article highlights"
_VIDEO_CATEGORIES = {"videos", "video", "youtube"}
_VIDEO_SOURCES = {"youtube"}
CONTENT_PLANNING_HEADER_PREFIX = "### Content Planning"
EMPTY_PLACEHOLDER = re.compile(r"^-\s*$")
HEADING_PREFIX = "### "
BOOK_DETAIL_URL = "https://readwise.io/api/v2/books/{book_id}/"
HIGHLIGHT_OPEN_URL = "https://readwise.io/open/{highlight_id}"
READER_DOC_URL = "https://read.readwise.io/read/{document_id}"
DOCUMENT_CREATED_EVENTS = {
    "reader.any_document.created",
    "reader.non_feed_document.created",
    "reader.feed_document.created",
}
READER_ANNOTATION_CATEGORIES = {"highlight", "note"}
_IGNORED_READER_SUFFIXES = (
    ".tags_updated",
    ".finished",
    ".archived",
    ".moved_to_later",
    ".moved_to_inbox",
    ".shortlisted",
)
_SHORT_ATTR_MAX = 80

# book_id → title/author/category/source/source_url/highlights_url (or None after a failed lookup)
_book_cache: dict[str, dict | None] = {}

_AUTHOR_TWITTER_SUFFIX = re.compile(r"\s+on twitter\s*$", re.I)
_AUTHOR_HANDLE = re.compile(r"@([^\s]+)")
_TWEETS_FROM_TITLE = re.compile(r"^tweets from\b", re.I)
_TWITTER_HOSTS = {"twitter.com", "x.com", "mobile.twitter.com"}

# Same image-embed rules as the journal tweet strip (separate PR). Used
# locally for Bookmarked Tweets until ``tweet_highlight_quote`` is on main.
# Live Aug 23, 2026 journal: t.co then one or more space-separated
# ``![](https://pbs.twimg.com/media/....jpg)``.
_EMPTY_ALT_TWIMG_MEDIA = re.compile(
    r"(?:!\[\]\(\s*https?://pbs\.twimg\.com/media/[^)\s]+\s*\)(?:\s+)?)+",
    re.IGNORECASE,
)
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_HTML_IMG_CLOSE = re.compile(r"</img\s*>", re.IGNORECASE)
_TWEET_MEDIA_URL = re.compile(
    r"(?:https?://)?(?:pbs\.twimg\.com|pic\.twitter\.com|video\.twimg\.com)"
    r"/[^\s<>)\]'\"]+",
    re.IGNORECASE,
)


def _system_tz() -> pytz.BaseTzInfo:
    return pytz.timezone(os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles"))


def journal_filename(dt: datetime) -> str:
    """Title-case month, unpadded day — e.g. ``Aug 22, 2026.md``.

    Do not lowercase this filename; Dropbox 404s on ``aug 22, 2026.md``.
    """
    return f"{dt.strftime('%b')} {dt.day}, {dt.strftime('%Y')}.md"


def _as_system_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return _system_tz().localize(dt)
    return dt.astimezone(_system_tz())


def parse_highlight_datetime(value: object) -> datetime | None:
    """Parse an ISO8601 timestamp into SYSTEM_TIMEZONE.

    Z / offset values are treated as UTC (or the given offset), then converted
    to local time. Naive values are assumed UTC.
    """
    text = _nonempty(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.info("Readwise timestamp not parseable: %s", value)
        return None
    if parsed.tzinfo is None:
        parsed = pytz.UTC.localize(parsed)
    return parsed.astimezone(_system_tz())


def _local_datetime_from_keys(
    payload: dict,
    keys: tuple[str, ...],
    now: datetime | None = None,
) -> datetime:
    for key in keys:
        parsed = parse_highlight_datetime(payload.get(key))
        if parsed is not None:
            return parsed
    if now is None:
        return datetime.now(_system_tz())
    return _as_system_tz(now)


def highlight_local_datetime(payload: dict, now: datetime | None = None) -> datetime:
    """Local time for journal dating: highlighted_at, created_at, updated, else now."""
    return _local_datetime_from_keys(payload, ("highlighted_at", "created_at", "updated"), now)


def document_local_datetime(payload: dict, now: datetime | None = None) -> datetime:
    """Local time for journal dating: created_at, saved_at, updated_at, else now."""
    return _local_datetime_from_keys(payload, ("created_at", "saved_at", "updated_at"), now)


def get_today_journal_path(journal_folder_path: str, now: datetime | None = None) -> str:
    """Journal path for the effective date (3am local rollover)."""
    if now is None:
        now = datetime.now(_system_tz())
    else:
        now = _as_system_tz(now)
    return f"{journal_folder_path}/{journal_filename(get_effective_date(now))}"


def get_highlight_journal_path(
    journal_folder_path: str,
    payload: dict,
    now: datetime | None = None,
) -> str:
    """Journal path for a highlight's created time (3am local rollover)."""
    local = highlight_local_datetime(payload, now=now)
    return f"{journal_folder_path}/{journal_filename(get_effective_date(local))}"


def get_document_journal_path(
    journal_folder_path: str,
    payload: dict,
    now: datetime | None = None,
) -> str:
    """Journal path for a Reader document's created time (3am local rollover)."""
    local = document_local_datetime(payload, now=now)
    return f"{journal_folder_path}/{journal_filename(get_effective_date(local))}"


def _get_dropbox_client() -> dropbox.Dropbox:
    """Dropbox client that auto-refreshes via OAuth refresh token.

    Prefer the SDK refresh-token constructor so a stale Redis-cached access
    token cannot fail mid-request.
    """
    refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
    app_key = os.getenv("DROPBOX_ACCESS_KEY")
    app_secret = os.getenv("DROPBOX_ACCESS_SECRET")
    if not all([refresh_token, app_key, app_secret]):
        raise EnvironmentError(
            "Missing one of DROPBOX_ACCESS_KEY / DROPBOX_ACCESS_SECRET / DROPBOX_REFRESH_TOKEN"
        )
    return dropbox.Dropbox(
        oauth2_refresh_token=refresh_token,
        app_key=app_key,
        app_secret=app_secret,
    )


def _find_folder_by_suffix(dbx: dropbox.Dropbox, parent_path: str, suffix: str) -> str:
    result = dbx.files_list_folder(parent_path)
    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith(suffix):
                return entry.path_lower
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)
    raise FileNotFoundError(f"Could not find '{suffix}' folder in {parent_path}")


def _get_file_content(dbx: dropbox.Dropbox, file_path: str) -> str:
    try:
        _, response = dbx.files_download(file_path)
        return response.content.decode("utf-8")
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.DownloadError):
            raise FileNotFoundError(f"Journal not found: {file_path}") from e
        raise


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


def is_highlight_event(payload: dict) -> bool:
    """True for ``readwise.highlight.created`` and highlight-shaped payloads."""
    event_type = str(payload.get("event_type") or "")
    if event_type == "readwise.highlight.created" or event_type.startswith("readwise.highlight"):
        return True
    if event_type.startswith("reader."):
        return False
    return _nonempty(payload.get("text")) is not None and payload.get("book_id") is not None


def is_reader_annotation_document(payload: dict) -> bool:
    """True for Reader child docs (highlights/notes) that duplicate highlight webhooks.

    Reader models highlights and notes as Documents (``category=highlight|note``,
    ``parent_id`` set). Those may fire ``reader.any_document.created`` but must
    not be written as document bullets.
    """
    category = str(payload.get("category") or "").strip().lower()
    if category in READER_ANNOTATION_CATEGORIES:
        return True
    return _nonempty(payload.get("parent_id")) is not None


def is_document_event(payload: dict) -> bool:
    """True for persistable Reader ``*_document.created`` parent documents."""
    if is_reader_annotation_document(payload):
        return False
    event_type = str(payload.get("event_type") or "")
    if event_type in DOCUMENT_CREATED_EVENTS:
        return True
    if event_type.startswith("reader.") and event_type.endswith("document.created"):
        return True
    if not event_type.startswith("reader."):
        return False
    if any(event_type.endswith(suffix) for suffix in _IGNORED_READER_SUFFIXES):
        return False
    return _nonempty(payload.get("title")) is not None or _http_url(payload.get("url")) is not None


def clear_book_cache() -> None:
    _book_cache.clear()


def fetch_book(book_id: object) -> dict | None:
    """GET /api/v2/books/{book_id}/. Cached in-process. Never raises."""
    if book_id is None or book_id == "":
        return None
    key = str(book_id)
    if key in _book_cache:
        return _book_cache[key]

    token = os.getenv("READWISE_TOKEN")
    if not token:
        return None

    try:
        response = requests.get(
            BOOK_DETAIL_URL.format(book_id=key),
            headers={"Authorization": f"Token {token}"},
            timeout=10,
        )
    except Exception:
        logger.exception("Readwise book lookup failed for %s", key)
        return None

    if response.status_code != 200:
        logger.info("Readwise book lookup %s returned %s", key, response.status_code)
        _book_cache[key] = None
        return None

    try:
        data = response.json()
    except Exception:
        logger.exception("Readwise book lookup returned invalid JSON for %s", key)
        _book_cache[key] = None
        return None

    book = {
        "title": _nonempty(data.get("title")),
        "author": _nonempty(data.get("author")),
        "category": _nonempty(data.get("category")),
        "source": _nonempty(data.get("source")),
        "highlights_url": _nonempty(data.get("highlights_url")),
        "source_url": _nonempty(data.get("source_url")),
    }
    _book_cache[key] = book
    return book


def _wikilink_target(text: str) -> str | None:
    """Sanitize a string for use inside ``[[...]]``.

    Only characters that would break a wikilink are removed: ``|``, ``#``,
    ``^``, and ``]]``. Returns None if nothing usable remains.
    """
    cleaned = _collapse(text).replace("]]", "")
    cleaned = re.sub(r"[|#^]", "", cleaned)
    return _collapse(cleaned) or None


def _sanitize_note_filename(title: str) -> str:
    """Same filename sanitizer Knowledge Hub uses for share-link notes."""
    from services.obsidian.add_shared_link import _sanitize_filename

    return _sanitize_filename(title)


def knowledge_hub_note_stem(title: str | None) -> str | None:
    """Turn a document/book title into the Knowledge Hub filename stem.

    Applies ``_sanitize_filename`` then ``_wikilink_from_note_stem`` so
    highlight wikilinks resolve to the same note a share/document save created.
    Title-only — regular iOS share-link / YouTube saves use this shape.
    Returns None when the title is empty or sanitizes to junk.
    """
    text = _nonempty(title)
    if not text:
        return None
    stem = _wikilink_from_note_stem(_sanitize_note_filename(text))
    if not stem or not re.search(r"[^\W_]", stem, re.UNICODE):
        return None
    return stem


def reader_knowledge_hub_note_stem(
    title: str | None,
    author: str | None = None,
) -> str | None:
    """Reader KH filename stem: ``Title by Author`` when author/creator exists.

    Same sanitizing as share-link (``_sanitize_filename`` then wikilink
    sanitize). No usable author → title-only stem. Empty/junk title → None.
    Author is always plain text — never ``Title by [[Author]]``.
    """
    base = knowledge_hub_note_stem(title)
    if not base:
        return None
    author_text = plain_author_label(author)
    if author_text and knowledge_hub_note_stem(author_text):
        return knowledge_hub_note_stem(f"{_nonempty(title)} by {author_text}")
    return base


def document_page_url(payload: dict) -> str | None:
    """Prefer ``source_url`` when it is an http(s) page, else Reader permalink."""
    return _http_url(payload.get("source_url")) or _document_permalink(payload)


def _reader_document_id(payload: dict) -> str | None:
    value = payload.get("id")
    if value is None or value == "":
        return None
    return str(value)


def _reader_author(payload: dict) -> str | None:
    return _nonempty(payload.get("author")) or _nonempty(payload.get("creator"))


def _reader_personal_url(payload: dict) -> str | None:
    """Personal Reader link: payload ``url`` if it is already one, else ``/read/{id}``."""
    url = _http_url(payload.get("url"))
    if url and "read.readwise.io/read/" in url:
        return url
    doc_id = _reader_document_id(payload)
    if doc_id:
        return READER_DOC_URL.format(document_id=doc_id)
    return None


def _as_published_date(value: object) -> str | None:
    """YYYY-MM-DD from a payload date. None if missing or unparseable (never invented)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        if ts <= 0:
            return None
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=pytz.UTC).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    text = _nonempty(value)
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso).date().isoformat()
    except ValueError:
        return None


def _published_date_value(payload: dict) -> str | None:
    for key in ("published_date", "published", "published_at", "date_published"):
        parsed = _as_published_date(payload.get(key))
        if parsed:
            return parsed
    return None


def _document_saved_at_iso(payload: dict) -> str | None:
    """ISO timestamp used for the document. Keep the original timezone string."""
    for key in ("created_at", "saved_at", "updated", "updated_at"):
        value = payload.get(key)
        if isinstance(value, datetime):
            return value.isoformat()
        text = _nonempty(value)
        if text:
            return text
    return None


def reader_document_extra_frontmatter(payload: dict) -> dict:
    """KH YAML extras for a Reader parent document. Omits empty fields."""
    extra: dict = {}
    source = _http_url(payload.get("source_url"))
    if source:
        extra["URL"] = source
    author = _reader_author(payload)
    if author:
        formatted = author_frontmatter_value(
            author, is_tweet=_is_tweet_book(_book_from_payload(payload))
        )
        if formatted:
            extra["author"] = formatted
    doc_id = _reader_document_id(payload)
    if doc_id:
        extra["readwise_id"] = doc_id
    readwise_url = _reader_personal_url(payload)
    if readwise_url:
        extra["readwise_url"] = readwise_url
    published = _published_date_value(payload)
    if published:
        extra["published"] = published
    saved = _document_saved_at_iso(payload)
    if saved:
        extra["saved_at"] = saved
    return extra


def document_journal_date(payload: dict, now: datetime | None = None) -> str:
    """3am-aware journal date label for a Reader document (e.g. ``Aug 22, 2026``)."""
    local = document_local_datetime(payload, now=now)
    return journal_filename(get_effective_date(local)).removesuffix(".md")


def _youtube_url_for_document(payload: dict, url: str | None) -> str | None:
    """Return a YouTube URL from the document if source/url is YouTube."""
    from services.obsidian.add_youtube_link import is_valid_youtube_url

    for href in (url, _http_url(payload.get("source_url")), _http_url(payload.get("url"))):
        if href and is_valid_youtube_url(href):
            return href
    return None


def _create_shared_link(
    url: str,
    title: str | None,
    journal_date: str,
    extra_frontmatter: dict | None = None,
) -> dict:
    from services.obsidian.add_shared_link import add_shared_link

    return add_shared_link(
        url,
        title=title,
        journal_date=journal_date,
        extra_frontmatter=extra_frontmatter,
    )


def _create_youtube_link(
    url: str,
    journal_date: str,
    extra_frontmatter: dict | None = None,
) -> dict:
    from services.obsidian.add_youtube_link import add_youtube_link

    return add_youtube_link(
        url,
        journal_date=journal_date,
        extra_frontmatter=extra_frontmatter,
    )


def _book_from_payload(payload: dict) -> dict:
    """Book fields already present on a webhook/export payload."""
    return {
        "title": _nonempty(payload.get("title")),
        "author": _nonempty(payload.get("author")) or _nonempty(payload.get("creator")),
        "category": _nonempty(payload.get("category")),
        "source": _nonempty(payload.get("source")),
        "source_url": _nonempty(payload.get("source_url")),
    }


def _has_book_identity(book: dict | None) -> bool:
    if not book:
        return False
    return any(book.get(key) for key in ("title", "author", "category", "source", "source_url"))


def _twitter_handle_from_url(url: object) -> str | None:
    text = _nonempty(url)
    if not text:
        return None
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in _TWITTER_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return parts[-1] if parts else None


def _tweet_handle(book: dict | None) -> str | None:
    """Extract @handle from author, else twitter.com/x.com source_url."""
    if not book:
        return None
    author = _nonempty(book.get("author"))
    if author:
        stripped = _AUTHOR_TWITTER_SUFFIX.sub("", author)
        match = _AUTHOR_HANDLE.search(stripped) or _AUTHOR_HANDLE.search(author)
        if match:
            return match.group(1)
    return _twitter_handle_from_url(book.get("source_url"))


def _is_tweet_book(book: dict | None) -> bool:
    """True for Readwise tweet sources (category/source/title/author/url)."""
    if not book:
        return False
    category = (_nonempty(book.get("category")) or "").lower()
    source = (_nonempty(book.get("source")) or "").lower()
    if category == "tweets" or source == "twitter":
        return True
    title = _nonempty(book.get("title"))
    if title and _TWEETS_FROM_TITLE.search(title):
        return True
    author = _nonempty(book.get("author"))
    if author and _AUTHOR_HANDLE.search(author) and re.search(r"\bon twitter\b", author, re.I):
        return True
    return _twitter_handle_from_url(book.get("source_url")) is not None


def _tweet_wikilink_target(book: dict | None) -> str | None:
    """``Tweets from @{handle}`` when this is a tweet book with a handle."""
    if not _is_tweet_book(book):
        return None
    handle = _tweet_handle(book)
    if not handle:
        return None
    return _wikilink_target(f"Tweets from @{handle}")


def _highlight_permalink(payload: dict) -> str | None:
    """``https://readwise.io/open/{id}``. Prefer this over payload.url (often null)."""
    highlight_id = payload.get("id")
    if highlight_id is not None and highlight_id != "":
        return HIGHLIGHT_OPEN_URL.format(highlight_id=highlight_id)
    return _http_url(payload.get("readwise_url"))


def _resolve_highlight_book(payload: dict) -> dict | None:
    """Book identity from the payload, else GET /api/v2/books/{id}/."""
    book = _book_from_payload(payload)
    if not _has_book_identity(book):
        book = fetch_book(payload.get("book_id"))
    return book


def format_readwise_bullet(payload: dict) -> str | None:
    """Build a compact highlight bullet. Looks up book title/author when possible.

    Export payloads include ``title``, ``author``, and tweet-detection fields;
    use those and skip the books API. Webhook payloads typically omit them,
    so fall back to GET /api/v2/books/{id}/.
    """
    if not is_highlight_event(payload):
        return None
    return _format_highlight(payload, _resolve_highlight_book(payload))


def _host_from_url(value: object) -> str | None:
    text = _nonempty(value)
    if not text:
        return None
    parsed = urlparse(text)
    host = parsed.netloc
    if not host or "@" in host:
        return None
    if host.lower().startswith("www."):
        host = host[4:]
    return host or None


def _short_attr(value: object) -> str | None:
    text = _nonempty(value)
    if not text:
        return None
    collapsed = _collapse(text)
    if len(collapsed) > _SHORT_ATTR_MAX:
        return None
    if collapsed.startswith(("http://", "https://")):
        return None
    return collapsed


def _document_permalink(payload: dict) -> str | None:
    """Prefer the official Reader URL; never invent a bookreview link."""
    url = _http_url(payload.get("url"))
    if url:
        return url
    doc_id = payload.get("id")
    if doc_id is not None and doc_id != "":
        return READER_DOC_URL.format(document_id=doc_id)
    return None


def format_document_bullet(payload: dict) -> str | None:
    """Build a Reader document bullet: ``- [Title](https://read.readwise.io/read/{id})``."""
    if not is_document_event(payload):
        return None
    url = _document_permalink(payload)
    title = _nonempty(payload.get("title"))
    site_name = _nonempty(payload.get("site_name"))
    display = title or site_name or _host_from_url(payload.get("source_url"))
    if not display and url:
        display = _host_from_url(url)
    if not display:
        return None

    collapsed = _collapse(display)
    if url:
        line = f"- [{collapsed}]({url})"
    else:
        line = f"- {collapsed}"

    extra = _short_attr(payload.get("author"))
    if extra is None or extra == collapsed:
        extra = _short_attr(payload.get("site_name"))
    if extra and extra != collapsed:
        line += f" — {extra}"
    return line


def _format_highlight(payload: dict, book: dict | None = None) -> str | None:
    text = _nonempty(payload.get("text"))
    if not text:
        return None
    if _is_tweet_book(book):
        text = tweet_highlight_quote(text)
        if not text:
            return None
    note = _nonempty(payload.get("note"))
    raw_title = _nonempty((book or {}).get("title"))
    raw_author = _nonempty((book or {}).get("author"))
    author = _collapse(raw_author) if raw_author else None
    if author and not _wikilink_target(author):
        author = None
    highlight_url = _highlight_permalink(payload)
    quote = f'"{_collapse(text)}"'
    if highlight_url:
        quote = f"[{quote}]({highlight_url})"

    tweet_target = _tweet_wikilink_target(book)
    if tweet_target:
        stem = tweet_target
    elif _is_tweet_book(book):
        stem = knowledge_hub_note_stem(raw_title)
    else:
        stem = reader_knowledge_hub_note_stem(
            raw_title,
            _reader_author(book or {}) or _reader_author(payload),
        )
    if stem:
        line = f"- [[{stem}]]: {quote}"
    elif author:
        line = f"- {author}: {quote}"
    else:
        line = f"- {quote}"
    if note:
        line += f" — {_collapse(note)}"
    return line


def _strip_tweet_image_embeds(text: object) -> str | None:
    """Remove markdown/HTML/twimg image embeds from tweet highlight text.

    Same rules as the journal buffet strip: no ``![]()``, ``<img>``, or
    bare ``pbs.twimg.com`` / ``pic.twitter.com`` / ``video.twimg.com``.
    Keeps quote text (including t.co links). Returns None if nothing remains.
    """
    if text is None:
        return None
    raw = str(text)
    if not raw.strip():
        return None
    cleaned = _EMPTY_ALT_TWIMG_MEDIA.sub("", raw)
    cleaned = _MARKDOWN_IMAGE.sub("", cleaned)
    cleaned = _HTML_IMG_TAG.sub("", cleaned)
    cleaned = _HTML_IMG_CLOSE.sub("", cleaned)
    cleaned = _TWEET_MEDIA_URL.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _tweet_quote_for_page(text: object) -> str | None:
    """Tweet quote for the handle page. Prefer the shared helper when present."""
    try:
        from services.obsidian.utils.tweet_highlight_quote import tweet_highlight_quote
    except ImportError:
        return _strip_tweet_image_embeds(text)
    return tweet_highlight_quote(text)


def _format_tweet_page_bullet(payload: dict, book: dict | None = None) -> str | None:
    """Quote line for the handle page: stripped quote + open/id, no wikilink.

    Journal keeps ``- [[Tweets from @handle]]: ["quote"](open/id)``.
    The handle page is already that note, so this is ``- ["quote"](open/id)``.
    Tweets only: requires ``_is_tweet_book`` and ``_tweet_handle``.
    Image embeds are stripped (same rule as the journal buffet).
    """
    if not _is_tweet_book(book) or not _tweet_handle(book):
        return None
    if not _tweet_wikilink_target(book):
        return None
    text = _nonempty(payload.get("text"))
    if not text:
        return None
    text = _tweet_quote_for_page(text)
    if not text:
        return None
    note = _nonempty(payload.get("note"))
    highlight_url = _highlight_permalink(payload)
    quote = f'"{text}"'
    if highlight_url:
        quote = f"[{quote}]({highlight_url})"
    line = f"- {quote}"
    if note:
        line += f" — {_collapse(note)}"
    return line


def dedup_keys(payload: dict) -> list[str]:
    """Per-highlight identifiers. Do not include book_id (shared across highlights).

    Use the open URL (which embeds the highlight id) rather than the bare id.
    A raw ``"2"`` would false-positive against dates like ``2026-08-22``.
    """
    keys: list[str] = []
    highlight_id = payload.get("id")
    if highlight_id is not None and highlight_id != "":
        keys.append(HIGHLIGHT_OPEN_URL.format(highlight_id=highlight_id))
    url = _nonempty(payload.get("url"))
    if url and url not in keys:
        keys.append(url)
    return keys


def document_dedup_keys(payload: dict) -> list[str]:
    """Per-document identifiers: Reader id and ``read.readwise.io`` permalink."""
    keys: list[str] = []
    doc_id = payload.get("id")
    if doc_id is not None and doc_id != "":
        keys.append(str(doc_id))
        keys.append(READER_DOC_URL.format(document_id=doc_id))
    url = _nonempty(payload.get("url"))
    if url and url not in keys:
        keys.append(url)
    return keys


def _section_bounds(
    lines: list[str],
    header: str = CONTENT_BUFFET_HEADER,
) -> tuple[int | None, int]:
    header_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == header),
        None,
    )
    if header_idx is None:
        return None, -1
    section_end = len(lines)
    for i in range(header_idx + 1, len(lines)):
        if lines[i].startswith(HEADING_PREFIX):
            section_end = i
            break
    return header_idx, section_end


def _planning_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if line.strip().startswith(CONTENT_PLANNING_HEADER_PREFIX):
            return i
    return None


def _section_has_dedup_key(
    section_body: list[str],
    keys: list[str] | None,
    *,
    exact_line: bool,
) -> bool:
    if not keys:
        return False
    if exact_line:
        stripped = {line.strip() for line in section_body}
        return any(key.strip() in stripped for key in keys if key)
    section_text = "\n".join(section_body)
    return any(key in section_text for key in keys if key)


def _buffet_bullet_lines(bullet: str) -> list[str]:
    """Split a (possibly nested) buffet block into individual lines."""
    lines = str(bullet).splitlines()
    return lines if lines else [""]


def insert_content_buffet_bullet(
    content: str,
    bullet: str,
    keys: list[str] | None = None,
    *,
    exact_line: bool = False,
) -> tuple[str, str]:
    """Insert ``bullet`` under Content Buffet. Returns (updated_content, action).

    Actions: ``inserted``, ``replaced`` (empty placeholder), ``skipped`` (dedup).

    ``exact_line=True`` matches stripped lines only, so a standalone
    ``- [[Note Title]]`` does not collapse into
    ``- [[Note Title]]: ["quote"](https://readwise.io/open/{id})``.
    Nested metadata under the standalone line is part of ``bullet`` and is
    inserted with it; dedup keys should still be the first line only.
    """
    lines = content.split("\n")
    header_idx, section_end = _section_bounds(lines)
    bullet_lines = _buffet_bullet_lines(bullet)

    if header_idx is None:
        new_section = [CONTENT_BUFFET_HEADER, *bullet_lines, ""]
        planning_idx = _planning_index(lines)
        if planning_idx is not None:
            updated = lines[:planning_idx] + new_section + lines[planning_idx:]
        else:
            updated = list(lines)
            if updated and updated[-1].strip():
                updated.append("")
            updated.extend(new_section)
        return "\n".join(updated), "inserted"

    section_body = lines[header_idx + 1 : section_end]
    if _section_has_dedup_key(section_body, keys, exact_line=exact_line):
        return content, "skipped"

    nonempty = [line for line in section_body if line.strip()]
    if len(nonempty) == 1 and EMPTY_PLACEHOLDER.match(nonempty[0].strip()):
        replaced = False
        new_body: list[str] = []
        for line in section_body:
            if not replaced and EMPTY_PLACEHOLDER.match(line.strip()):
                new_body.extend(bullet_lines)
                replaced = True
            else:
                new_body.append(line)
        return "\n".join(lines[: header_idx + 1] + new_body + lines[section_end:]), "replaced"

    insert_at = header_idx + 1
    for i, line in enumerate(section_body):
        if line.strip() and not EMPTY_PLACEHOLDER.match(line.strip()):
            insert_at = header_idx + 1 + i + 1
    updated = lines[:insert_at] + bullet_lines + lines[insert_at:]
    return "\n".join(updated), "inserted"


def _insert_heading_bullet(
    content: str,
    header: str,
    bullet: str,
    keys: list[str] | None = None,
) -> tuple[str, str]:
    """Insert ``bullet`` under ``header``. Returns (content, action).

    If the heading is missing, it is appended at the end of the note —
    existing People/body/other headings are left alone. Dedup uses
    ``_section_has_dedup_key`` on the open URL (not the title or stem).
    """
    lines = content.split("\n")
    header_idx, section_end = _section_bounds(lines, header)
    bullet_lines = _buffet_bullet_lines(bullet)

    if header_idx is None:
        updated = list(lines)
        if updated and updated[-1].strip():
            updated.append("")
        updated.append(header)
        updated.extend(bullet_lines)
        return "\n".join(updated), "inserted"

    section_body = lines[header_idx + 1 : section_end]
    if _section_has_dedup_key(section_body, keys, exact_line=False):
        return content, "skipped"

    nonempty = [line for line in section_body if line.strip()]
    if len(nonempty) == 1 and EMPTY_PLACEHOLDER.match(nonempty[0].strip()):
        replaced = False
        new_body: list[str] = []
        for line in section_body:
            if not replaced and EMPTY_PLACEHOLDER.match(line.strip()):
                new_body.extend(bullet_lines)
                replaced = True
            else:
                new_body.append(line)
        return "\n".join(lines[: header_idx + 1] + new_body + lines[section_end:]), "replaced"

    insert_at = header_idx + 1
    for i, line in enumerate(section_body):
        if line.strip() and not EMPTY_PLACEHOLDER.match(line.strip()):
            insert_at = header_idx + 1 + i + 1
    updated = lines[:insert_at] + bullet_lines + lines[insert_at:]
    return "\n".join(updated), "inserted"


def insert_bookmarked_tweets_bullet(
    content: str,
    bullet: str,
    keys: list[str] | None = None,
) -> tuple[str, str]:
    """Insert ``bullet`` under ``### Bookmarked Tweets``. Returns (content, action).

    Sibling of ``insert_content_buffet_bullet``. If the heading is missing, it
    is appended at the end of the note — existing People/body/other headings
    are left alone. Dedup uses ``_section_has_dedup_key`` on the open URL
    (not the handle or title).
    """
    return _insert_heading_bullet(content, BOOKMARKED_TWEETS_HEADER, bullet, keys)


def insert_book_highlights_bullet(
    content: str,
    bullet: str,
    keys: list[str] | None = None,
) -> tuple[str, str]:
    """Insert ``bullet`` under ``### Book highlights``. Returns (content, action).

    Sibling of ``insert_bookmarked_tweets_bullet``. Missing heading is
    appended; other sections stay. Dedup is the open URL only.
    """
    return _insert_heading_bullet(content, BOOK_HIGHLIGHTS_HEADER, bullet, keys)


def insert_article_highlights_bullet(
    content: str,
    bullet: str,
    keys: list[str] | None = None,
) -> tuple[str, str]:
    """Insert ``bullet`` under ``### Article highlights``. Returns (content, action).

    Sibling of ``insert_book_highlights_bullet``. Missing heading is
    appended; other sections stay. Dedup is the open URL only.
    """
    return _insert_heading_bullet(content, ARTICLE_HIGHLIGHTS_HEADER, bullet, keys)


def _wikilink_from_note_stem(note_title: str) -> str | None:
    """Sanitize a Knowledge Hub filename stem for ``[[wikilink]]``.

    Keeps the stem as-named (so the link resolves to the KH file) and only
    strips characters that break Obsidian links: ``|``, ``#``, ``^``, ``]]``.
    Does not collapse internal whitespace.
    """
    cleaned = note_title.replace("]]", "")
    cleaned = re.sub(r"[|#^]", "", cleaned).strip()
    return cleaned or None


def standalone_wikilink_bullet(
    note_title: str,
    nested_lines: list[str] | None = None,
) -> tuple[str, list[str]] | None:
    """Standalone buffet line ``- [[stem]]`` with exact-line dedup keys.

    Keys are the first line only. Nested metadata (source/readwise/author)
    is optional and is not used as a dedup key. A later highlight line
    ``- [[stem]]: ["quote"](https://readwise.io/open/{id})`` must not match.
    """
    target = _wikilink_from_note_stem(note_title)
    if not target:
        return None
    first = f"- [[{target}]]"
    if not nested_lines:
        return first, [first]
    block = [first]
    for line in nested_lines:
        text = str(line).rstrip("\n")
        if text.strip():
            block.append(text)
    return "\n".join(block), [first]


def append_wikilink_to_journal_buffet(
    note_title: str,
    journal_date: str,
    dbx: dropbox.Dropbox | None = None,
    *,
    nested_lines: list[str] | None = None,
) -> dict:
    """Append ``- [[note_title]]`` to that journal day's Content Buffet.

    ``journal_date`` must be the same string written to KH YAML ``Journal``
    (e.g. ``Aug 22, 2026``). Missing journal files are skipped — never created
    and never raised to the caller. Dedup is exact-line on the standalone
    wikilink so highlight quote lines are not treated as the same entry.
    Optional ``nested_lines`` are written under the wikilink on first insert
    only; a same-day skip does not duplicate them.

    The first line is ``- [[Title]]`` or ``- [[Title by Author]]`` with a
    plain stem — never ``Title by [[Author]]``. Do not nest author wikilinks.
    """
    result: dict = {"success": True, "action": None, "error": None, "file_path": None}
    prepared = standalone_wikilink_bullet(note_title, nested_lines=nested_lines)
    if not prepared:
        logger.info("KH journal buffet skipped; empty wikilink target from %r", note_title)
        result["action"] = "ignored"
        return result

    bullet, keys = prepared
    target = _wikilink_from_note_stem(note_title) or note_title

    try:
        if dbx is None:
            dbx = _get_dropbox_client()
        journal_folder = _resolve_journal_folder(dbx)
        file_path = f"{journal_folder}/{journal_date}.md"
        result["file_path"] = file_path
        try:
            content = _get_file_content(dbx, file_path)
        except FileNotFoundError:
            logger.warning(
                "KH journal buffet skipped; journal not found (will not create): %s",
                file_path,
            )
            result["action"] = "skipped_missing_journal"
            return result

        updated, action = insert_content_buffet_bullet(
            content, bullet, keys, exact_line=True
        )
        result["action"] = action
        if action != "skipped" and updated != content:
            dbx.files_upload(
                updated.encode("utf-8"),
                file_path,
                mode=dropbox.files.WriteMode.overwrite,
            )
            logger.info("KH journal buffet %s path=%s title=%s", action, file_path, target)
        elif action == "skipped":
            logger.info("KH journal buffet skipped (duplicate) path=%s title=%s", file_path, target)
        return result
    except Exception as exc:
        logger.warning("KH journal buffet failed (KH save still succeeds): %s", exc)
        result["action"] = "error"
        result["error"] = str(exc)
        return result


def _resolve_journal_folder(dbx: dropbox.Dropbox) -> str:
    vault_path = os.getenv("DROPBOX_OBSIDIAN_VAULT_PATH")
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")
    daily_folder = _find_folder_by_suffix(dbx, vault_path, "_Daily")
    return _find_folder_by_suffix(dbx, daily_folder, "_Journal")


def _empty_write_summary(selected: int = 0) -> dict:
    return {
        "selected": selected,
        "inserted": 0,
        "replaced": 0,
        "skipped": 0,
        "skipped_missing_journal": 0,
        "files_written": 0,
        "errors": [],
        "paths": [],
    }


def _resolve_knowledge_hub_folder(dbx: dropbox.Dropbox) -> str:
    """Knowledge Hub folder (``*_Knowledge-Hub``) via the share-link helper."""
    vault_path = os.getenv("DROPBOX_OBSIDIAN_VAULT_PATH")
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")
    from services.obsidian.add_shared_link import _find_knowledge_hub_path

    return _find_knowledge_hub_path(dbx, vault_path)


def _hub_note_filename(stem: str) -> str:
    return _sanitize_note_filename(stem) + ".md"


def _tweet_page_filename(target: str) -> str:
    return _hub_note_filename(target)


def _tweet_people_wikilink(handle: str) -> str:
    """People target ``[[@{handle}]]`` — Readwise casing as-is, including ``@``.

    Do not lowercase, strip ``@``, or run the handle through a sanitizer
    that drops ``@``. ``_tweet_handle`` already returns the token without
    a leading ``@``.
    """
    return f"[[@{handle}]]"


def _people_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item is not None and str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _people_entry_has_handle(entry: object, handle: str) -> bool:
    """True when ``entry`` is already this handle (case-insensitive).

    ``[[@Handle]]``, ``@Handle``, and ``Handle`` all count as present.
    """
    text = str(entry).strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].strip()
    if "|" in text:
        text = text.split("|", 1)[0].strip()
    if text.startswith("@"):
        text = text[1:]
    return bool(text) and text.casefold() == handle.casefold()


def _ensure_tweet_page_people(content: str, handle: str) -> str:
    """Add ``[[@{handle}]]`` to YAML People when that handle is missing.

    Does not overwrite other People entries or clear the body. A matching
    ``[[@Handle]]`` / ``@Handle`` / ``Handle`` (case-insensitive) is left
    as-is so we do not duplicate.
    """
    from services.obsidian.add_shared_link import _extract_frontmatter, _rebuild_markdown

    people_link = _tweet_people_wikilink(handle)
    frontmatter, body = _extract_frontmatter(content)
    people = _people_list(frontmatter.get("People"))
    if any(_people_entry_has_handle(item, handle) for item in people):
        return content
    frontmatter["People"] = [*people, people_link]
    return _rebuild_markdown(frontmatter, body)


def _new_tweet_page_markdown(title: str, bullet: str, handle: str) -> str:
    """Minimal handle page: YAML title + People, H1, Bookmarked Tweets, first bullet.

    People is a list of wikilinks, same shape share-link uses
    (``People: ["[[@handle]]"]`` / block list of ``[[name]]`` items).
    """
    people_link = _tweet_people_wikilink(handle)
    return (
        f"---\n"
        f'title: "{title}"\n'
        f"People:\n"
        f'  - "{people_link}"\n'
        f"---\n"
        f"\n"
        f"# {title}\n"
        f"\n"
        f"{BOOKMARKED_TWEETS_HEADER}\n"
        f"{bullet}\n"
    )


def _find_hub_note_by_filename(
    dbx: dropbox.Dropbox,
    hub_path: str,
    filename: str,
) -> str | None:
    """Case-insensitive filename match in the Knowledge Hub folder (non-recursive).

    Share-link lookup is a constructed path; Dropbox path reads are
    case-insensitive. This scan covers an existing file whose displayed
    casing differs. ``entries`` must be a real list so MagicMock clients
    used in journal-only tests do not iterate forever.
    """
    want = filename.casefold()
    result = dbx.files_list_folder(hub_path)
    while True:
        entries = getattr(result, "entries", None)
        if not isinstance(entries, (list, tuple)):
            return None
        for entry in entries:
            name = getattr(entry, "name", None)
            if not isinstance(name, str) or name.casefold() != want:
                continue
            path = getattr(entry, "path_display", None) or getattr(entry, "path_lower", None)
            if path:
                return path
        if getattr(result, "has_more", False) is not True:
            break
        result = dbx.files_list_folder_continue(result.cursor)
    return None


def _download_hub_note(
    dbx: dropbox.Dropbox,
    hub_path: str,
    filename: str,
) -> tuple[str, str] | None:
    """Return ``(path, content)`` for an existing Knowledge Hub note, or None."""
    constructed = f"{hub_path}/{filename}"
    try:
        return constructed, _get_file_content(dbx, constructed)
    except FileNotFoundError:
        pass
    alt = _find_hub_note_by_filename(dbx, hub_path, filename)
    if not alt:
        return None
    try:
        return alt, _get_file_content(dbx, alt)
    except FileNotFoundError:
        return None


def _download_tweet_page(
    dbx: dropbox.Dropbox,
    hub_path: str,
    filename: str,
) -> tuple[str, str] | None:
    """Return ``(path, content)`` for an existing handle page, or None."""
    return _download_hub_note(dbx, hub_path, filename)


def _append_tweet_page(
    dbx: dropbox.Dropbox,
    payload: dict,
    hub_path: str,
) -> str | None:
    """Create or append one tweet bookmark on the handle's Knowledge Hub note.

    Tweets only — gated on ``_is_tweet_book`` and ``_tweet_handle``. Returns
    the Dropbox action (``created``, ``inserted``, ``replaced``, ``skipped``)
    or None when this highlight is not a tweet with a handle.
    """
    book = _resolve_highlight_book(payload)
    handle = _tweet_handle(book)
    if not _is_tweet_book(book) or not handle:
        return None
    target = _tweet_wikilink_target(book)
    if not target:
        return None
    bullet = _format_tweet_page_bullet(payload, book)
    if not bullet:
        return None
    keys = dedup_keys(payload)
    filename = _tweet_page_filename(target)
    existing = _download_tweet_page(dbx, hub_path, filename)
    if existing is None:
        file_path = f"{hub_path}/{filename}"
        markdown = _new_tweet_page_markdown(target, bullet, handle)
        dbx.files_upload(
            markdown.encode("utf-8"),
            file_path,
            mode=dropbox.files.WriteMode.overwrite,
        )
        logger.info("Tweet page created path=%s", file_path)
        return "created"

    file_path, content = existing
    updated, action = insert_bookmarked_tweets_bullet(content, bullet, keys)
    updated = _ensure_tweet_page_people(updated, handle)
    if updated != content:
        dbx.files_upload(
            updated.encode("utf-8"),
            file_path,
            mode=dropbox.files.WriteMode.overwrite,
        )
        if action == "skipped":
            logger.info("Tweet page people backfill path=%s", file_path)
        else:
            logger.info("Tweet page %s path=%s", action, file_path)
    elif action == "skipped":
        logger.info("Tweet page skipped (duplicate) path=%s", file_path)
    return action


def _append_tweet_pages_after_journal(
    dbx: dropbox.Dropbox,
    payloads: list[dict],
) -> None:
    """Best-effort handle-page writes after a successful highlight journal pass.

    ``readwise.highlight.created`` only (the same ``format_readwise_bullet``
    writer the journal tweet line uses). Gated on ``_is_tweet_book`` and
    ``_tweet_handle`` — articles, books, Reader docs, and other highlights
    stay journal-only (no page create, no ``### Bookmarked Tweets``, no
    append). Not used for Reader ``*_document.created``, share-link,
    YouTube, or Raindrop. Resolves the Knowledge Hub folder only when the
    payload is a tweet with a handle. Failures are logged and never raised
    — the journal write already happened.
    """
    hub_path: str | None = None
    for payload in payloads:
        if not is_highlight_event(payload):
            continue
        book = _resolve_highlight_book(payload)
        if not _is_tweet_book(book) or not _tweet_handle(book):
            continue
        try:
            if hub_path is None:
                hub_path = _resolve_knowledge_hub_folder(dbx)
            _append_tweet_page(dbx, payload, hub_path)
        except Exception:
            logger.exception(
                "Tweet page write failed; journal write kept (highlight %s)",
                payload.get("id"),
            )


def _is_book_highlight(book: dict | None) -> bool:
    """True for Readwise ``category == books`` that are not tweets."""
    if not book or _is_tweet_book(book):
        return False
    category = (_nonempty(book.get("category")) or "").lower()
    return category == "books"


def _book_page_stem(book: dict | None, payload: dict) -> str | None:
    """``Title by Author`` stem (title-only if no author) for a book highlight."""
    if not _is_book_highlight(book):
        return None
    return reader_knowledge_hub_note_stem(
        _nonempty((book or {}).get("title")),
        _reader_author(book or {}) or _reader_author(payload),
    )


def _distinct_author_names(raw: object) -> list[str]:
    """Distinct author names for YAML author + People. Dedups case-insensitively."""
    text = plain_author_label(raw)
    if not text:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for part in split_author_names(_collapse(text)):
        target = _wikilink_target(part)
        if not target:
            continue
        key = target.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(target)
    return names


def _book_author_people(book: dict | None, payload: dict) -> tuple[str | None, list[str]]:
    """Wikilinked ``author`` value and People list for distinct book authors."""
    names = _distinct_author_names(
        _reader_author(book or {}) or _reader_author(payload)
    )
    if not names:
        return None, []
    formatted = author_frontmatter_value(", ".join(names))
    return formatted, [f"[[{name}]]" for name in names]


def _book_page_extras(payload: dict, book: dict | None) -> dict:
    """Fill-if-empty YAML extras for a book page. Omits empty fields."""
    extras: dict = {}
    author, _people = _book_author_people(book, payload)
    if author:
        extras["author"] = author
    source_url = _http_url((book or {}).get("source_url") or payload.get("source_url"))
    if source_url:
        extras["URL"] = source_url
    book_id = payload.get("book_id")
    if book_id is not None and book_id != "":
        extras["readwise_id"] = str(book_id)
    category = _nonempty((book or {}).get("category") or payload.get("category"))
    if category:
        extras["category"] = category
    source = _nonempty((book or {}).get("source") or payload.get("source"))
    if source:
        extras["source"] = source
    return extras


def _format_book_page_bullet(payload: dict, book: dict | None = None) -> str | None:
    """Quote line for the book page: quote + open/id, no title wikilink.

    Journal keeps ``- [[Title by Author]]: ["quote"](open/id)``.
    The book page is already that note, so this is ``- ["quote"](open/id)``.
    Books only: requires ``_is_book_highlight``. User notes use the same
    em dash as the journal formatter.
    """
    if not _is_book_highlight(book):
        return None
    if not _book_page_stem(book, payload):
        return None
    text = _nonempty(payload.get("text"))
    if not text:
        return None
    note = _nonempty(payload.get("note"))
    highlight_url = _highlight_permalink(payload)
    quote = f'"{_collapse(text)}"'
    if highlight_url:
        quote = f"[{quote}]({highlight_url})"
    line = f"- {quote}"
    if note:
        line += f" — {_collapse(note)}"
    return line


def _yaml_value_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict)) and not value:
        return True
    return False


def _people_entry_has_name(entry: object, name: str) -> bool:
    """True when ``entry`` is already this person (case-insensitive).

    ``[[Alice Smith]]`` and ``Alice Smith`` count as present.
    """
    text = str(entry).strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].strip()
    if "|" in text:
        text = text.split("|", 1)[0].strip()
    have = _collapse(text)
    want = _collapse(name)
    return bool(have) and have.casefold() == want.casefold()


def _new_book_page_markdown(
    stem: str,
    bullet: str,
    extras: dict,
    people_links: list[str],
) -> str:
    """Minimal book page: YAML metadata, H1, Book highlights, first bullet."""
    lines = ["---", f'title: "{stem}"']
    author = extras.get("author")
    if author:
        lines.append(f"author: {author_yaml_literal(author)}")
    if people_links:
        lines.append("People:")
        for link in people_links:
            lines.append(f'  - "{link}"')
    for key in ("URL", "readwise_id", "category", "source"):
        value = extras.get(key)
        if value is None or (isinstance(value, str) and not str(value).strip()):
            continue
        lines.append(f"{key}: {value}")
    lines.extend(["---", "", f"# {stem}", "", BOOK_HIGHLIGHTS_HEADER, bullet, ""])
    return "\n".join(lines)


def _ensure_book_page_frontmatter(
    content: str,
    extras: dict,
    people_links: list[str],
) -> str:
    """Fill empty YAML keys and add missing People author wikilinks.

    Does not overwrite People/body/other keys blindly. A plain-text
    ``author`` is upgraded to wikilinks only when it is the same name(s).
    """
    from services.obsidian.add_shared_link import _extract_frontmatter, _rebuild_markdown

    frontmatter, body = _extract_frontmatter(content)
    changed = False
    if extras.get("title") and _yaml_value_empty(frontmatter.get("title")):
        frontmatter["title"] = extras["title"]
        changed = True
    for key, value in extras.items():
        if key == "title":
            continue
        if value is None or (isinstance(value, str) and not str(value).strip()):
            continue
        existing = frontmatter.get(key)
        if key == "author":
            if _yaml_value_empty(existing) or is_plain_to_wikilink_author_upgrade(
                existing, value
            ):
                frontmatter[key] = value
                changed = True
            continue
        if _yaml_value_empty(existing):
            frontmatter[key] = value
            changed = True

    if people_links:
        people = _people_list(frontmatter.get("People"))
        added = False
        for link in people_links:
            name = link[2:-2] if link.startswith("[[") and link.endswith("]]") else link
            if any(_people_entry_has_name(item, name) for item in people):
                continue
            people.append(link)
            added = True
        if added:
            frontmatter["People"] = people
            changed = True

    if not changed:
        return content
    return _rebuild_markdown(frontmatter, body)


def _append_book_page(
    dbx: dropbox.Dropbox,
    payload: dict,
    hub_path: str,
) -> str | None:
    """Create or append one book highlight on the ``Title by Author`` note.

    Books only — gated on ``_is_book_highlight``. Returns the Dropbox
    action (``created``, ``inserted``, ``replaced``, ``skipped``) or None
    when this highlight is not a book.
    """
    book = _resolve_highlight_book(payload)
    if not _is_book_highlight(book):
        return None
    stem = _book_page_stem(book, payload)
    if not stem:
        return None
    bullet = _format_book_page_bullet(payload, book)
    if not bullet:
        return None
    keys = dedup_keys(payload)
    extras = _book_page_extras(payload, book)
    extras["title"] = stem
    _author, people_links = _book_author_people(book, payload)
    filename = _hub_note_filename(stem)
    existing = _download_hub_note(dbx, hub_path, filename)
    if existing is None:
        file_path = f"{hub_path}/{filename}"
        markdown = _new_book_page_markdown(stem, bullet, extras, people_links)
        dbx.files_upload(
            markdown.encode("utf-8"),
            file_path,
            mode=dropbox.files.WriteMode.overwrite,
        )
        logger.info("Book page created path=%s", file_path)
        return "created"

    file_path, content = existing
    updated, action = insert_book_highlights_bullet(content, bullet, keys)
    updated = _ensure_book_page_frontmatter(updated, extras, people_links)
    if updated != content:
        dbx.files_upload(
            updated.encode("utf-8"),
            file_path,
            mode=dropbox.files.WriteMode.overwrite,
        )
        if action == "skipped":
            logger.info("Book page metadata backfill path=%s", file_path)
        else:
            logger.info("Book page %s path=%s", action, file_path)
    elif action == "skipped":
        logger.info("Book page skipped (duplicate) path=%s", file_path)
    return action


def _append_book_pages_after_journal(
    dbx: dropbox.Dropbox,
    payloads: list[dict],
) -> None:
    """Best-effort book-page writes after a successful highlight journal pass.

    ``readwise.highlight.created`` only (the same ``format_readwise_bullet``
    writer the journal book line uses). Gated on ``_is_book_highlight`` —
    tweets stay on the tweet-page path, articles and Reader documents do
    not get ``### Book highlights``. Not used for Reader
    ``*_document.created``, share-link, YouTube, or Raindrop. Resolves the
    Knowledge Hub folder only when the payload is a book. Failures are
    logged and never raised — the journal write already happened.
    """
    hub_path: str | None = None
    for payload in payloads:
        if not is_highlight_event(payload):
            continue
        book = _resolve_highlight_book(payload)
        if not _is_book_highlight(book):
            continue
        try:
            if hub_path is None:
                hub_path = _resolve_knowledge_hub_folder(dbx)
            _append_book_page(dbx, payload, hub_path)
        except Exception:
            logger.exception(
                "Book page write failed; journal write kept (highlight %s)",
                payload.get("id"),
            )


def _is_youtube_or_video_highlight(
    book: dict | None,
    payload: dict | None = None,
) -> bool:
    """True for YouTube/video parent documents (Transcript Highlights owns these)."""
    data = book or {}
    extra = payload or {}
    category = (
        _nonempty(data.get("category")) or _nonempty(extra.get("category")) or ""
    ).lower()
    if category in _VIDEO_CATEGORIES:
        return True
    source = (
        _nonempty(data.get("source")) or _nonempty(extra.get("source")) or ""
    ).lower()
    if source in _VIDEO_SOURCES:
        return True
    return bool(_youtube_url_for_document(extra or data, None))


def _is_article_highlight(book: dict | None, payload: dict | None = None) -> bool:
    """True for Reader/article highlights that are not tweets, books, or video.

    Readwise ``articles`` / Reader ``article`` and other non-tweet, non-book,
    non-youtube/video parent documents (email, pdfs, …) count. A category
    is required so uncategorized export books stay journal-only.
    YouTube/video highlights are left for ``### Transcript Highlights``.
    """
    if not book or _is_tweet_book(book) or _is_book_highlight(book):
        return False
    if _is_youtube_or_video_highlight(book, payload):
        return False
    category = (
        _nonempty(book.get("category")) or _nonempty((payload or {}).get("category")) or ""
    ).lower()
    return bool(category)


def _article_page_stem(book: dict | None, payload: dict) -> str | None:
    """``Title by Author`` stem (title-only if no author) for an article highlight."""
    if not _is_article_highlight(book, payload):
        return None
    return reader_knowledge_hub_note_stem(
        _nonempty((book or {}).get("title")),
        _reader_author(book or {}) or _reader_author(payload),
    )


def _article_page_extras(payload: dict, book: dict | None) -> dict:
    """Minimal YAML extras for an article page: author + fill-if-empty URL."""
    extras: dict = {}
    author, _people = _book_author_people(book, payload)
    if author:
        extras["author"] = author
    source_url = _http_url((book or {}).get("source_url") or payload.get("source_url"))
    if source_url:
        extras["URL"] = source_url
    return extras


def _format_article_page_bullet(payload: dict, book: dict | None = None) -> str | None:
    """Quote line for the article page: quote + open/id, no title wikilink.

    Journal keeps ``- [[Title by Author]]: ["quote"](open/id)``.
    The article page is already that note, so this is ``- ["quote"](open/id)``.
    Articles only: requires ``_is_article_highlight``. User notes use the same
    em dash as the journal formatter.
    """
    if not _is_article_highlight(book, payload):
        return None
    if not _article_page_stem(book, payload):
        return None
    text = _nonempty(payload.get("text"))
    if not text:
        return None
    note = _nonempty(payload.get("note"))
    highlight_url = _highlight_permalink(payload)
    quote = f'"{_collapse(text)}"'
    if highlight_url:
        quote = f"[{quote}]({highlight_url})"
    line = f"- {quote}"
    if note:
        line += f" — {_collapse(note)}"
    return line


def _new_article_page_markdown(
    stem: str,
    bullet: str,
    extras: dict,
    people_links: list[str],
) -> str:
    """Minimal article page: YAML title/author/People/URL, H1, Article highlights."""
    lines = ["---", f'title: "{stem}"']
    author = extras.get("author")
    if author:
        lines.append(f"author: {author_yaml_literal(author)}")
    if people_links:
        lines.append("People:")
        for link in people_links:
            lines.append(f'  - "{link}"')
    url = extras.get("URL")
    if url and str(url).strip():
        lines.append(f"URL: {url}")
    lines.extend(["---", "", f"# {stem}", "", ARTICLE_HIGHLIGHTS_HEADER, bullet, ""])
    return "\n".join(lines)


def _append_article_page(
    dbx: dropbox.Dropbox,
    payload: dict,
    hub_path: str,
) -> str | None:
    """Create or append one article highlight on the ``Title by Author`` note.

    Articles only — gated on ``_is_article_highlight``. Returns the Dropbox
    action (``created``, ``inserted``, ``replaced``, ``skipped``) or None
    when this highlight is not an article. Existing share-link / Reader
    notes are updated in place and never renamed.
    """
    book = _resolve_highlight_book(payload)
    if not _is_article_highlight(book, payload):
        return None
    stem = _article_page_stem(book, payload)
    if not stem:
        return None
    bullet = _format_article_page_bullet(payload, book)
    if not bullet:
        return None
    keys = dedup_keys(payload)
    extras = _article_page_extras(payload, book)
    extras["title"] = stem
    _author, people_links = _book_author_people(book, payload)
    filename = _hub_note_filename(stem)
    existing = _download_hub_note(dbx, hub_path, filename)
    if existing is None:
        file_path = f"{hub_path}/{filename}"
        markdown = _new_article_page_markdown(stem, bullet, extras, people_links)
        dbx.files_upload(
            markdown.encode("utf-8"),
            file_path,
            mode=dropbox.files.WriteMode.overwrite,
        )
        logger.info("Article page created path=%s", file_path)
        return "created"

    file_path, content = existing
    updated, action = insert_article_highlights_bullet(content, bullet, keys)
    url_only = {}
    if extras.get("URL"):
        url_only["URL"] = extras["URL"]
    if url_only:
        updated = _ensure_book_page_frontmatter(updated, url_only, [])
    if updated != content:
        dbx.files_upload(
            updated.encode("utf-8"),
            file_path,
            mode=dropbox.files.WriteMode.overwrite,
        )
        if action == "skipped":
            logger.info("Article page URL backfill path=%s", file_path)
        else:
            logger.info("Article page %s path=%s", action, file_path)
    elif action == "skipped":
        logger.info("Article page skipped (duplicate) path=%s", file_path)
    return action


def _append_article_pages_after_journal(
    dbx: dropbox.Dropbox,
    payloads: list[dict],
) -> None:
    """Best-effort article-page writes after a successful highlight journal pass.

    ``readwise.highlight.created`` only (the same ``format_readwise_bullet``
    writer the journal article line uses). Gated on ``_is_article_highlight``
    — tweets stay on the tweet-page path, books stay on ``### Book
    highlights``, and YouTube/video highlights are left for Transcript
    Highlights. Not used for Reader ``*_document.created``, share-link,
    YouTube, or Raindrop. Resolves the Knowledge Hub folder only when the
    payload is an article. Failures are logged and never raised — the
    journal write already happened.
    """
    hub_path: str | None = None
    for payload in payloads:
        if not is_highlight_event(payload):
            continue
        book = _resolve_highlight_book(payload)
        if not _is_article_highlight(book, payload):
            continue
        try:
            if hub_path is None:
                hub_path = _resolve_knowledge_hub_folder(dbx)
            _append_article_page(dbx, payload, hub_path)
        except Exception:
            logger.exception(
                "Article page write failed; journal write kept (highlight %s)",
                payload.get("id"),
            )


def write_highlights_by_journal(
    payloads: list[dict],
    now: datetime | None = None,
    raise_errors: bool = False,
    *,
    journal_path_fn=None,
    format_fn=None,
    keys_fn=None,
) -> dict:
    """Write events grouped by journal file (one download/upload per day).

    Defaults are the highlight formatter, highlight journal path, and
    highlight dedup keys. Missing journal files are skipped — never written
    to today. After a successful journal pass, tweet *highlights* also
    create or append the ``Tweets from @handle`` Knowledge Hub page, and
    book *highlights* (``category == books``, not tweets) create or append
    the ``Title by Author`` page under ``### Book highlights``, and
    article *highlights* (not tweets, not books, not YouTube/video) create
    or append the same stem under ``### Article highlights``, when
    ``format_fn`` is ``format_readwise_bullet`` (webhook
    ``readwise.highlight.created`` and the existing highlight backfill,
    which already calls this writer). Document fallback / other formatters
    skip those page writes. Page failures are logged and never undo the
    journal write.
    """
    if journal_path_fn is None:
        journal_path_fn = get_highlight_journal_path
    if format_fn is None:
        format_fn = format_readwise_bullet
    if keys_fn is None:
        keys_fn = dedup_keys

    summary = _empty_write_summary(selected=len(payloads))
    if not payloads:
        return summary

    dbx = _get_dropbox_client()
    journal_folder = _resolve_journal_folder(dbx)

    by_path: dict[str, list[dict]] = {}
    for payload in payloads:
        path = journal_path_fn(journal_folder, payload, now=now)
        by_path.setdefault(path, []).append(payload)

    for file_path, group in by_path.items():
        summary["paths"].append(file_path)
        try:
            try:
                content = _get_file_content(dbx, file_path)
            except FileNotFoundError:
                logger.warning(
                    "Readwise buffet skipped; journal not found (will not write today): %s",
                    file_path,
                )
                summary["skipped_missing_journal"] += len(group)
                continue

            original = content
            file_counts = {"inserted": 0, "replaced": 0, "skipped": 0}
            last_action = None
            buffet_payloads: list[dict] = []
            for payload in group:
                bullet = format_fn(payload)
                if not bullet:
                    continue
                content, action = insert_content_buffet_bullet(
                    content, bullet, keys_fn(payload)
                )
                last_action = action
                buffet_payloads.append(payload)
                if action in file_counts:
                    file_counts[action] += 1
                    summary[action] += 1

            if content != original:
                dbx.files_upload(
                    content.encode("utf-8"),
                    file_path,
                    mode=dropbox.files.WriteMode.overwrite,
                )
                summary["files_written"] += 1
                if len(group) == 1 and last_action:
                    logger.info("Readwise buffet %s path=%s", last_action, file_path)
                else:
                    logger.info(
                        "Readwise buffet wrote path=%s inserted=%s replaced=%s skipped=%s",
                        file_path,
                        file_counts["inserted"],
                        file_counts["replaced"],
                        file_counts["skipped"],
                    )
            elif last_action == "skipped":
                logger.info("Readwise buffet skipped (duplicate) path=%s", file_path)

            if format_fn is format_readwise_bullet:
                _append_tweet_pages_after_journal(dbx, buffet_payloads)
                _append_book_pages_after_journal(dbx, buffet_payloads)
                _append_article_pages_after_journal(dbx, buffet_payloads)
        except Exception as exc:
            logger.exception("Readwise buffet failed for %s", file_path)
            if raise_errors:
                raise
            summary["errors"].append(f"{file_path}: {exc}")

    return summary


def _write_buffet_bullet(
    payload: dict,
    now: datetime | None = None,
    *,
    write_kwargs: dict | None = None,
) -> dict:
    result = write_highlights_by_journal(
        [payload], now=now, raise_errors=True, **(write_kwargs or {})
    )
    file_path = result["paths"][0] if result["paths"] else None
    if result["skipped_missing_journal"]:
        action = "skipped_missing_journal"
    elif result["skipped"]:
        action = "skipped"
    elif result["replaced"]:
        action = "replaced"
    elif result["inserted"]:
        action = "inserted"
    else:
        action = "ignored"
    return {"success": True, "action": action, "error": None, "file_path": file_path}


def _reader_document_excerpt(payload: dict) -> str | None:
    """Optional Raindrop excerpt: payload ``excerpt``, else ``summary``."""
    return _nonempty(payload.get("excerpt")) or _nonempty(payload.get("summary"))


def _mirror_reader_document_to_raindrop(
    payload: dict,
    url: str | None,
    title: str | None,
) -> None:
    """Bookmark a parent Reader document via ``create_bookmark`` (Unsorted).

    Same outbound helper as ``/share/link``. Never raises — missing token
    and duplicate-URL rejections are logged and skipped.
    """
    if not url:
        logger.info("Readwise document Raindrop skip (no http url)")
        return
    try:
        raindrop_result = create_bookmark(url, title, _reader_document_excerpt(payload))
        if raindrop_result["success"]:
            logger.info(
                "Mirrored Reader document to Raindrop.io: %s (id=%s)",
                url[:100],
                raindrop_result["bookmark_id"],
            )
        else:
            logger.error(
                "Failed to mirror Reader document to Raindrop.io: %s - %s",
                url[:100],
                raindrop_result["error"],
            )
    except Exception as exc:
        logger.error(
            "Unexpected error mirroring Reader document to Raindrop.io: %s - %s",
            url[:100],
            exc,
        )


def _append_document_markdown_fallback(payload: dict, now: datetime | None = None) -> dict:
    """Write the legacy Reader URL bullet when KH was skipped for a junk/empty title."""
    bullet = format_document_bullet(payload)
    if not bullet:
        logger.info("Readwise document had no usable title or URL; skipping write")
        return {"success": True, "action": "ignored", "error": None, "file_path": None}
    return _write_buffet_bullet(
        payload,
        now=now,
        write_kwargs={
            "journal_path_fn": get_document_journal_path,
            "format_fn": format_document_bullet,
            "keys_fn": document_dedup_keys,
        },
    )


def _append_reader_document_knowledge_hub(
    payload: dict,
    now: datetime | None = None,
) -> dict:
    """Create/update a Knowledge Hub note and buffet wikilink for a parent document.

    After a successful KH create or update (including same-day skip), also
    bookmark the document page URL in Raindrop Unsorted. Raindrop errors
    never fail the webhook or undo the KH write.
    """
    url = document_page_url(payload)
    title = _nonempty(payload.get("title"))
    stem = reader_knowledge_hub_note_stem(title, _reader_author(payload))
    journal_date = document_journal_date(payload, now=now)
    youtube_url = _youtube_url_for_document(payload, url)
    extras = reader_document_extra_frontmatter(payload) or None

    if not youtube_url and not stem:
        logger.info(
            "Readwise document KH skipped (empty/junk title); writing markdown fallback"
        )
        return _append_document_markdown_fallback(payload, now=now)

    if not youtube_url and not url:
        logger.info("Readwise document KH skipped (no http url)")
        return {"success": True, "action": "ignored", "error": None, "file_path": None}

    try:
        if youtube_url:
            result = _create_youtube_link(
                youtube_url,
                journal_date=journal_date,
                extra_frontmatter=extras,
            )
        else:
            result = _create_shared_link(
                url,
                title=stem,
                journal_date=journal_date,
                extra_frontmatter=extras,
            )
    except Exception as exc:
        logger.exception(
            "Knowledge Hub save failed for Reader document %s", payload.get("id")
        )
        return {"success": True, "action": "kh_error", "error": str(exc), "file_path": None}

    if not result.get("success"):
        logger.error(
            "Knowledge Hub save failed for Reader document %s: %s",
            payload.get("id"),
            result.get("error"),
        )
        return {
            "success": True,
            "action": result.get("action") or "kh_error",
            "error": result.get("error"),
            "file_path": result.get("file_path"),
        }

    logger.info(
        "Readwise document KH %s title=%s date=%s",
        result.get("action"),
        title or result.get("title"),
        journal_date,
    )
    bookmark_title = title or _nonempty(result.get("title"))
    _mirror_reader_document_to_raindrop(payload, url=url or youtube_url, title=bookmark_title)
    return {
        "success": True,
        "action": result.get("action"),
        "error": None,
        "file_path": result.get("file_path"),
    }


def append_readwise_buffet(payload: dict, now: datetime | None = None) -> dict:
    """Append a Readwise highlight or Reader document to the journal for its date.

    Parent Reader documents create/update a Knowledge Hub note (with Reader
    YAML extras when present) and write a standalone ``- [[Title by Author]]``
    (or ``- [[Title]]`` if no author) to that day's Content Buffet, then
    bookmark the document page URL in Raindrop Unsorted. Highlights wikilink
    that same KH stem and are not bookmarked. ``readwise.highlight.created``
    tweet highlights also create or append ``Tweets from @handle`` under
    ``### Bookmarked Tweets`` after the journal write. Book highlights
    (``category == books``, not tweets) create or append
    ``Title by Author`` under ``### Book highlights``. Article highlights
    (not tweets, not books, not YouTube/video) create or append the same
    stem under ``### Article highlights``. Reader document events,
    share-link, YouTube, and Raindrop do not write those pages. Child
    annotation documents are ignored.
    """
    if is_highlight_event(payload):
        bullet = format_readwise_bullet(payload)
        if not bullet:
            logger.info("Readwise event had no usable fields; skipping write")
            return {"success": True, "action": "ignored", "error": None, "file_path": None}
        return _write_buffet_bullet(payload, now=now)
    if is_reader_annotation_document(payload):
        logger.info(
            "Readwise document skipped (highlight/note or parent_id set): %s",
            payload.get("event_type", "unknown"),
        )
        return {"success": True, "action": "ignored", "error": None, "file_path": None}
    if is_document_event(payload):
        return _append_reader_document_knowledge_hub(payload, now=now)

    logger.info(
        "Readwise event ignored (not a highlight or document): %s",
        payload.get("event_type", "unknown"),
    )
    return {"success": True, "action": "ignored", "error": None, "file_path": None}
