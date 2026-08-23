"""Readwise Reader list + document create client.

Official endpoints:
- GET https://readwise.io/api/v3/list/
- POST https://readwise.io/api/v3/save/
Docs: https://readwise.io/reader_api
"""

import logging
import os
from typing import TypedDict

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

LIST_URL = "https://readwise.io/api/v3/list/"
SAVE_URL = "https://readwise.io/api/v3/save/"
READER_DOC_URL = "https://read.readwise.io/read/{document_id}"
READER_ANNOTATION_CATEGORIES = {"highlight", "note"}
SAVED_USING = "gen-intelligence"


class ReaderSaveResult(TypedDict):
    """Result of a Reader document CREATE (POST /api/v3/save/)."""

    success: bool
    id: str | None
    url: str | None
    error: str | None
    status_code: int | None


def _headers() -> dict[str, str]:
    token = os.getenv("READWISE_TOKEN")
    if not token:
        raise EnvironmentError("READWISE_TOKEN not set")
    return {"Authorization": f"Token {token}"}


def reader_permalink(document_id: str | None, returned_url: str | None = None) -> str | None:
    """Stable Reader permalink for Knowledge Hub YAML.

    Document CREATE returns ``https://read.readwise.io/new/read/{id}`` (inbox
    location). That opens the video, but KH metadata already uses
    ``https://read.readwise.io/read/{id}`` for Reader docs. Prefer that
    form so Genesis can open the video from YAML; fall back to rewriting
    ``/new/read/`` on a returned URL if the id is missing.
    """
    doc_id = str(document_id).strip() if document_id is not None else ""
    if doc_id:
        return READER_DOC_URL.format(document_id=doc_id)
    url = str(returned_url).strip() if returned_url else ""
    if not url:
        return None
    return url.replace("/new/read/", "/read/", 1)


def save_document(
    url: str,
    *,
    category: str | None = None,
    title: str | None = None,
    saved_using: str | None = SAVED_USING,
) -> ReaderSaveResult:
    """POST a URL to Reader Document CREATE.

    ``201`` (new) and ``200`` (already exists) are both success. The
    response is ``{id, url}``. Missing ``READWISE_TOKEN`` and HTTP errors
    return ``success=False`` and never raise.
    """
    result: ReaderSaveResult = {
        "success": False,
        "id": None,
        "url": None,
        "error": None,
        "status_code": None,
    }
    try:
        headers = _headers()
    except EnvironmentError as exc:
        result["error"] = str(exc)
        logger.error("Reader save skipped: %s", exc)
        return result

    body: dict[str, str] = {"url": url}
    if category:
        body["category"] = category
    if title:
        body["title"] = title
    if saved_using:
        body["saved_using"] = saved_using

    try:
        response = requests.post(
            SAVE_URL,
            headers=headers,
            json=body,
            timeout=60,
        )
        result["status_code"] = response.status_code
        if response.status_code not in (200, 201):
            result["error"] = (
                f"Reader save returned {response.status_code}: {response.text}"
            )
            logger.error(
                "Failed to save document to Reader: %s %s",
                response.status_code,
                response.text,
            )
            return result
        data = response.json() if response.content else {}
        doc_id = data.get("id")
        result["success"] = True
        result["id"] = str(doc_id) if doc_id not in (None, "") else None
        result["url"] = data.get("url") or None
        logger.info(
            "Saved document to Reader: id=%s status=%s url=%s",
            result["id"],
            response.status_code,
            url[:100],
        )
        return result
    except requests.RequestException as exc:
        result["error"] = str(exc)
        logger.error("Failed to connect to Reader save API: %s", exc)
        return result


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
