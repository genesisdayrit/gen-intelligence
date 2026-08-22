"""Readwise Reader list client.

Official endpoint: GET https://readwise.io/api/v3/list/
Docs: https://readwise.io/reader_api
"""

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

LIST_URL = "https://readwise.io/api/v3/list/"
READER_ANNOTATION_CATEGORIES = {"highlight", "note"}


def _headers() -> dict[str, str]:
    token = os.getenv("READWISE_TOKEN")
    if not token:
        raise EnvironmentError("READWISE_TOKEN not set")
    return {"Authorization": f"Token {token}"}


def is_parent_reader_document(document: dict) -> bool:
    """Skip Reader child docs (highlights/notes) that duplicate highlight export."""
    category = str(document.get("category") or "").strip().lower()
    if category in READER_ANNOTATION_CATEGORIES:
        return False
    parent_id = document.get("parent_id")
    if parent_id is not None and str(parent_id).strip():
        return False
    return True


def fetch_list_pages(updated_after: str | None = None):
    """Yield each Reader list page. Paginates with nextPageCursor / pageCursor."""
    page_cursor = None
    while True:
        params: dict[str, str] = {}
        if page_cursor:
            params["pageCursor"] = page_cursor
        if updated_after:
            params["updatedAfter"] = updated_after
        logger.info("Readwise Reader list request params=%s", params or "{}")
        response = requests.get(
            LIST_URL,
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


def iter_reader_documents(updated_after: str | None = None):
    """Yield parent Reader documents from every list page."""
    for page in fetch_list_pages(updated_after=updated_after):
        for document in page.get("results") or []:
            if is_parent_reader_document(document):
                yield document
