"""Inbound Raindrop.io bookmark webhook helpers.

Raindrop's public API has no first-party webhook
(https://developer.raindrop.io/). This module accepts the REST raindrop
item shape plus common IFTTT / Make / manual POST bodies so those tools
can hit ``POST /raindrop/webhook`` when a document is created.
"""

from __future__ import annotations

import logging
from typing import Any

from services.obsidian.add_shared_link import add_shared_link
from services.obsidian.add_youtube_link import add_youtube_link, is_valid_youtube_url

logger = logging.getLogger(__name__)

# Event-name fields only — not raindrop ``type`` (link/article/video).
_EVENT_FIELDS = ("event", "event_type", "eventType", "action", "operation")
_NESTED_BOOKMARK_KEYS = ("raindrop", "item", "bookmark", "data")
_URL_KEYS = ("url", "link", "Url", "Link", "source_url")
_TITLE_KEYS = ("title", "Title", "name", "Name")
_CREATE_TOKENS = ("created", "create", "added", "new")
_IGNORE_TOKENS = (
    "deleted",
    "delete",
    "removed",
    "remove",
    "updated",
    "update",
    "edited",
    "edit",
    "archived",
    "archive",
    "trashed",
    "trash",
)


def _nonempty(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _http_url(value: object) -> str | None:
    text = _nonempty(value)
    if not text:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return None


def _title_from(obj: dict) -> str | None:
    for key in _TITLE_KEYS:
        title = _nonempty(obj.get(key))
        if title:
            return title
    return None


def _url_from(obj: dict) -> str | None:
    for key in _URL_KEYS:
        url = _http_url(obj.get(key))
        if url:
            return url
    return None


def _candidate_objects(payload: dict) -> list[dict]:
    objects: list[dict] = [payload]
    for key in _NESTED_BOOKMARK_KEYS:
        nested = payload.get(key)
        if isinstance(nested, dict):
            objects.append(nested)
    items = payload.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                objects.append(item)
                break
    return objects


def extract_raindrop_bookmark(payload: dict) -> dict[str, str | None] | None:
    """Return ``{url, title}`` from common Raindrop / IFTTT / Make shapes."""
    if not isinstance(payload, dict):
        return None
    for obj in _candidate_objects(payload):
        url = _url_from(obj)
        if url:
            return {"url": url, "title": _title_from(obj)}
    return None


def _event_token(payload: dict) -> str | None:
    for key in _EVENT_FIELDS:
        token = _nonempty(payload.get(key))
        if token:
            return token.lower()
    return None


def _is_removed(payload: dict) -> bool:
    if payload.get("removed") is True:
        return True
    for key in _NESTED_BOOKMARK_KEYS:
        nested = payload.get(key)
        if isinstance(nested, dict) and nested.get("removed") is True:
            return True
    return False


def _looks_like_highlight_only(payload: dict) -> bool:
    """Raindrop highlight objects have text + raindropRef and no bookmark URL."""
    if extract_raindrop_bookmark(payload) is not None:
        return False
    has_highlight_text = _nonempty(payload.get("text")) is not None
    has_ref = payload.get("raindropRef") is not None or payload.get("raindrop_ref") is not None
    return has_highlight_text and has_ref


def is_created_raindrop_event(payload: dict) -> bool:
    """True when the payload is a new raindrop/item/bookmark, not delete/update.

    Missing event fields (IFTTT / Make / curl) count as created when a URL
    can be extracted. Raindrop highlights are never treated as documents.
    """
    if not isinstance(payload, dict):
        return False
    if _is_removed(payload) or _looks_like_highlight_only(payload):
        return False
    if extract_raindrop_bookmark(payload) is None:
        return False
    token = _event_token(payload)
    if token is None:
        return True
    if any(part in token for part in _CREATE_TOKENS):
        return True
    if any(part in token for part in _IGNORE_TOKENS):
        return False
    return True


def process_created_raindrop(payload: dict) -> dict[str, Any]:
    """Write the Knowledge Hub note + standalone buffet wikilink.

    YouTube URLs use ``add_youtube_link``; everything else uses
    ``add_shared_link``. Both helpers apply 3am-aware today via
    ``get_effective_date`` + ``journal_filename`` and call
    ``append_wikilink_to_journal_buffet``. Same-day existing KH notes
    skip (no second buffet line). Missing journals skip the buffet
    without failing the KH save. Does not remirror to Raindrop.
    """
    if not is_created_raindrop_event(payload):
        logger.info("Raindrop webhook ignored (not a created bookmark)")
        return {"success": True, "action": "ignored", "error": None}

    bookmark = extract_raindrop_bookmark(payload)
    if not bookmark or not bookmark.get("url"):
        logger.info("Raindrop webhook ignored (no url)")
        return {"success": True, "action": "ignored", "error": None}

    url = str(bookmark["url"])
    title = bookmark.get("title")
    logger.info("Raindrop created bookmark | title=%s | url=%s", title or "(none)", url[:100])

    if is_valid_youtube_url(url):
        return add_youtube_link(url)
    return add_shared_link(url, title=title)
