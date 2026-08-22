"""Append Readwise webhook events to today's Obsidian journal Content Buffet."""

import logging
import os
import re
from datetime import datetime

import dropbox
import pytz
from dotenv import load_dotenv

from services.obsidian.utils.date_helpers import get_effective_date

load_dotenv()

logger = logging.getLogger(__name__)

CONTENT_BUFFET_HEADER = "### Content Buffet:"
CONTENT_PLANNING_HEADER_PREFIX = "### Content Planning"
EMPTY_PLACEHOLDER = re.compile(r"^-\s*$")
HEADING_PREFIX = "### "


def _system_tz() -> pytz.BaseTzInfo:
    return pytz.timezone(os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles"))


def journal_filename(dt: datetime) -> str:
    """Title-case month, unpadded day — e.g. ``Aug 22, 2026.md``.

    Do not lowercase this filename; Dropbox 404s on ``aug 22, 2026.md``.
    """
    return f"{dt.strftime('%b')} {dt.day}, {dt.strftime('%Y')}.md"


def get_today_journal_path(journal_folder_path: str, now: datetime | None = None) -> str:
    """Journal path for the effective date (3am local rollover)."""
    if now is None:
        now = datetime.now(_system_tz())
    elif now.tzinfo is None:
        now = _system_tz().localize(now)
    else:
        now = now.astimezone(_system_tz())
    return f"{journal_folder_path}/{journal_filename(get_effective_date(now))}"


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


def _link_url(payload: dict) -> str | None:
    """Prefer source_url, then url; keep a non-http value if that is all we have."""
    return (
        _http_url(payload.get("source_url"), payload.get("url"))
        or _nonempty(payload.get("source_url"))
        or _nonempty(payload.get("url"))
    )


def _event_suffix(event_type: str | None) -> str:
    """Mention event_type only when it is not an obvious *.created event."""
    if not event_type or event_type.endswith(".created"):
        return ""
    short = event_type.rsplit(".", 1)[-1].replace("_", " ")
    return f" ({short})" if short else ""


def _is_highlight(payload: dict) -> bool:
    event_type = payload.get("event_type") or ""
    return event_type.startswith("readwise.highlight") or (
        _nonempty(payload.get("text")) is not None
        and payload.get("title") is None
        and payload.get("book_id") is not None
    )


def format_readwise_bullet(payload: dict) -> str | None:
    """Build a compact markdown bullet from a Reader or highlight payload."""
    if _is_highlight(payload):
        return _format_highlight(payload)
    return _format_reader(payload)


def _format_reader(payload: dict) -> str | None:
    title = _nonempty(payload.get("title"))
    author = _nonempty(payload.get("author"))
    url = _link_url(payload)
    fallback = (
        _nonempty(payload.get("summary"))
        or _nonempty(payload.get("notes"))
        or _nonempty(payload.get("id"))
    )
    if title and url:
        line = f"- [{_collapse(title)}]({url})"
    elif title:
        line = f"- {_collapse(title)}"
    elif url:
        line = f"- [{url}]({url})"
    elif fallback:
        line = f"- {_collapse(fallback)}"
    else:
        return None
    if author:
        line += f" — {_collapse(author)}"
    line += _event_suffix(payload.get("event_type"))
    return line


def _format_highlight(payload: dict) -> str | None:
    text = _nonempty(payload.get("text"))
    note = _nonempty(payload.get("note"))
    url = _link_url(payload)
    book_id = _nonempty(payload.get("book_id"))
    title = (
        _nonempty(payload.get("title"))
        or _nonempty(payload.get("book_title"))
        or (f"book {book_id}" if book_id else None)
    )
    parts: list[str] = []
    if text:
        parts.append(f'"{_collapse(text)}"')
    if title and url:
        parts.append(f"[{_collapse(title)}]({url})")
    elif title:
        parts.append(_collapse(title))
    elif url:
        parts.append(f"[highlight]({url})")
    if note:
        parts.append(_collapse(note))
    if not parts:
        fallback = _nonempty(payload.get("id"))
        if not fallback:
            return None
        parts.append(fallback)
    line = "- " + " — ".join(parts)
    line += _event_suffix(payload.get("event_type"))
    return line


def dedup_keys(payload: dict) -> list[str]:
    """Stable identifiers already present in Content Buffet should skip a rewrite."""
    keys: list[str] = []
    for candidate in (
        payload.get("id"),
        payload.get("source_url"),
        payload.get("url"),
        payload.get("book_id"),
    ):
        text = _nonempty(candidate)
        if text and text not in keys:
            keys.append(text)
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


def append_readwise_buffet(payload: dict, now: datetime | None = None) -> dict:
    """Append a Readwise event to today's journal Content Buffet section."""
    bullet = format_readwise_bullet(payload)
    if not bullet:
        logger.info("Readwise event had no usable fields; skipping write")
        return {"success": True, "action": "ignored", "error": None, "file_path": None}

    vault_path = os.getenv("DROPBOX_OBSIDIAN_VAULT_PATH")
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

    dbx = _get_dropbox_client()
    daily_folder = _find_folder_by_suffix(dbx, vault_path, "_Daily")
    journal_folder = _find_folder_by_suffix(dbx, daily_folder, "_Journal")
    file_path = get_today_journal_path(journal_folder, now=now)
    content = _get_file_content(dbx, file_path)

    updated, action = insert_content_buffet_bullet(content, bullet, dedup_keys(payload))
    if action == "skipped":
        logger.info("Readwise buffet skipped (duplicate) path=%s", file_path)
        return {"success": True, "action": action, "error": None, "file_path": file_path}

    dbx.files_upload(
        updated.encode("utf-8"),
        file_path,
        mode=dropbox.files.WriteMode.overwrite,
    )
    logger.info("Readwise buffet %s path=%s", action, file_path)
    return {"success": True, "action": action, "error": None, "file_path": file_path}
