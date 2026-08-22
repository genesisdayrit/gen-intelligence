"""Readwise Highlight EXPORT client.

Official sync endpoint: GET https://readwise.io/api/v2/export/
Docs: https://readwise.io/api_deets
"""

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

EXPORT_URL = "https://readwise.io/api/v2/export/"


def _nonempty(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _headers() -> dict[str, str]:
    token = os.getenv("READWISE_TOKEN")
    if not token:
        raise EnvironmentError("READWISE_TOKEN not set")
    return {"Authorization": f"Token {token}"}


def highlight_from_export(book: dict, highlight: dict) -> dict:
    """Flatten an export book + highlight into a webhook-shaped payload.

    Carries the book title so ``format_readwise_bullet`` can skip the books API.
    Maps ``updated_at`` to ``updated`` for journal dating fallback.
    """
    book_id = highlight.get("book_id")
    if book_id is None or book_id == "":
        book_id = book.get("user_book_id")
    title = _nonempty(book.get("title")) or _nonempty(book.get("readable_title"))
    payload = {
        **highlight,
        "book_id": book_id,
        "title": title,
        "updated": highlight.get("updated") or highlight.get("updated_at"),
    }
    return payload


def is_usable_export_highlight(book: dict, highlight: dict) -> bool:
    """Skip deleted/discarded books or highlights and empty quote text."""
    if book.get("is_deleted") or highlight.get("is_deleted") or highlight.get("is_discard"):
        return False
    return _nonempty(highlight.get("text")) is not None


def fetch_export_pages(updated_after: str | None = None):
    """Yield each export page dict. Paginates with nextPageCursor / pageCursor."""
    page_cursor = None
    while True:
        params: dict[str, str] = {}
        if page_cursor:
            params["pageCursor"] = page_cursor
        if updated_after:
            params["updatedAfter"] = updated_after
        logger.info("Readwise export request params=%s", params or "{}")
        response = requests.get(
            EXPORT_URL,
            params=params,
            headers=_headers(),
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        yield data
        page_cursor = data.get("nextPageCursor")
        if not page_cursor:
            break


def iter_export_highlights(updated_after: str | None = None):
    """Yield usable highlight payloads from every export page."""
    for page in fetch_export_pages(updated_after=updated_after):
        for book in page.get("results") or []:
            if book.get("is_deleted"):
                continue
            for highlight in book.get("highlights") or []:
                if not is_usable_export_highlight(book, highlight):
                    continue
                yield highlight_from_export(book, highlight)
