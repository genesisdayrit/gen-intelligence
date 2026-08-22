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

from services.obsidian.utils.date_helpers import get_effective_date

load_dotenv()

logger = logging.getLogger(__name__)

CONTENT_BUFFET_HEADER = "### Content Buffet:"
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


def _book_from_payload(payload: dict) -> dict:
    """Book fields already present on a webhook/export payload."""
    return {
        "title": _nonempty(payload.get("title")),
        "author": _nonempty(payload.get("author")),
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


def format_readwise_bullet(payload: dict) -> str | None:
    """Build a compact highlight bullet. Looks up book title/author when possible.

    Export payloads include ``title``, ``author``, and tweet-detection fields;
    use those and skip the books API. Webhook payloads typically omit them,
    so fall back to GET /api/v2/books/{id}/.
    """
    if not is_highlight_event(payload):
        return None
    book = _book_from_payload(payload)
    if not _has_book_identity(book):
        book = fetch_book(payload.get("book_id"))
    return _format_highlight(payload, book)


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
    note = _nonempty(payload.get("note"))
    raw_title = _nonempty((book or {}).get("title"))
    raw_author = _nonempty((book or {}).get("author"))
    title = _wikilink_target(raw_title) if raw_title else None
    author = _collapse(raw_author) if raw_author else None
    if author and not _wikilink_target(author):
        author = None
    highlight_url = _highlight_permalink(payload)
    quote = f'"{_collapse(text)}"'
    if highlight_url:
        quote = f"[{quote}]({highlight_url})"

    tweet_target = _tweet_wikilink_target(book)
    if tweet_target:
        line = f"- [[{tweet_target}]]: {quote}"
    elif title and author:
        target = _wikilink_target(f"{title} by {author}")
        line = f"- [[{target}]]: {quote}" if target else f"- {quote}"
    elif title:
        line = f"- [[{title}]]: {quote}"
    elif author:
        line = f"- {author}: {quote}"
    else:
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


def _section_bounds(lines: list[str]) -> tuple[int | None, int]:
    header_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == CONTENT_BUFFET_HEADER),
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


def insert_content_buffet_bullet(
    content: str,
    bullet: str,
    keys: list[str] | None = None,
) -> tuple[str, str]:
    """Insert ``bullet`` under Content Buffet. Returns (updated_content, action).

    Actions: ``inserted``, ``replaced`` (empty placeholder), ``skipped`` (dedup).
    """
    lines = content.split("\n")
    header_idx, section_end = _section_bounds(lines)

    if header_idx is None:
        new_section = [CONTENT_BUFFET_HEADER, bullet, ""]
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
    section_text = "\n".join(section_body)
    for key in keys or []:
        if key and key in section_text:
            return content, "skipped"

    nonempty = [line for line in section_body if line.strip()]
    if len(nonempty) == 1 and EMPTY_PLACEHOLDER.match(nonempty[0].strip()):
        replaced = False
        new_body: list[str] = []
        for line in section_body:
            if not replaced and EMPTY_PLACEHOLDER.match(line.strip()):
                new_body.append(bullet)
                replaced = True
            else:
                new_body.append(line)
        return "\n".join(lines[: header_idx + 1] + new_body + lines[section_end:]), "replaced"

    insert_at = header_idx + 1
    for i, line in enumerate(section_body):
        if line.strip() and not EMPTY_PLACEHOLDER.match(line.strip()):
            insert_at = header_idx + 1 + i + 1
    updated = lines[:insert_at] + [bullet] + lines[insert_at:]
    return "\n".join(updated), "inserted"


def _wikilink_from_note_stem(note_title: str) -> str | None:
    """Sanitize a Knowledge Hub filename stem for ``[[wikilink]]``.

    Keeps the stem as-named (so the link resolves to the KH file) and only
    strips characters that break Obsidian links: ``|``, ``#``, ``^``, ``]]``.
    Does not collapse internal whitespace.
    """
    cleaned = note_title.replace("]]", "")
    cleaned = re.sub(r"[|#^]", "", cleaned).strip()
    return cleaned or None


def append_wikilink_to_journal_buffet(
    note_title: str,
    journal_date: str,
    dbx: dropbox.Dropbox | None = None,
) -> dict:
    """Append ``- [[note_title]]`` to that journal day's Content Buffet.

    ``journal_date`` must be the same string written to KH YAML ``Journal``
    (e.g. ``Aug 22, 2026``). Missing journal files are skipped — never created
    and never raised to the caller.
    """
    result: dict = {"success": True, "action": None, "error": None, "file_path": None}
    target = _wikilink_from_note_stem(note_title)
    if not target:
        logger.info("KH journal buffet skipped; empty wikilink target from %r", note_title)
        result["action"] = "ignored"
        return result

    bullet = f"- [[{target}]]"
    keys = [bullet, f"[[{target}]]", target]
    if note_title and note_title != target:
        keys.append(note_title)

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

        updated, action = insert_content_buffet_bullet(content, bullet, keys)
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
    to today.
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
            for payload in group:
                bullet = format_fn(payload)
                if not bullet:
                    continue
                content, action = insert_content_buffet_bullet(
                    content, bullet, keys_fn(payload)
                )
                last_action = action
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
        except Exception as exc:
            logger.exception("Readwise buffet failed for %s", file_path)
            if raise_errors:
                raise
            summary["errors"].append(f"{file_path}: {exc}")

    return summary


def append_readwise_buffet(payload: dict, now: datetime | None = None) -> dict:
    """Append a Readwise highlight or Reader document to the journal for its date."""
    write_kwargs: dict = {}
    if is_highlight_event(payload):
        bullet = format_readwise_bullet(payload)
    elif is_reader_annotation_document(payload):
        logger.info(
            "Readwise document skipped (highlight/note or parent_id set): %s",
            payload.get("event_type", "unknown"),
        )
        return {"success": True, "action": "ignored", "error": None, "file_path": None}
    elif is_document_event(payload):
        bullet = format_document_bullet(payload)
        write_kwargs = {
            "journal_path_fn": get_document_journal_path,
            "format_fn": format_document_bullet,
            "keys_fn": document_dedup_keys,
        }
    else:
        logger.info(
            "Readwise event ignored (not a highlight or document): %s",
            payload.get("event_type", "unknown"),
        )
        return {"success": True, "action": "ignored", "error": None, "file_path": None}

    if not bullet:
        logger.info("Readwise event had no usable fields; skipping write")
        return {"success": True, "action": "ignored", "error": None, "file_path": None}

    result = write_highlights_by_journal(
        [payload], now=now, raise_errors=True, **write_kwargs
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
