"""Readwise Reader list + document create client.

Official endpoints:
- GET https://readwise.io/api/v3/list/
- POST https://readwise.io/api/v3/save/
- PATCH https://readwise.io/api/v3/update/<document_id>/
Docs: https://readwise.io/reader_api
"""

import logging
import os
import time
from typing import TypedDict

import requests
from dotenv import load_dotenv

from services.obsidian.utils.author_yaml import is_site_host_author

load_dotenv()

logger = logging.getLogger(__name__)

LIST_URL = "https://readwise.io/api/v3/list/"
SAVE_URL = "https://readwise.io/api/v3/save/"
UPDATE_URL = "https://readwise.io/api/v3/update/{document_id}/"
READER_DOC_URL = "https://read.readwise.io/read/{document_id}"
_UPDATE_MAX_ATTEMPTS = 3
_UPDATE_RETRY_CAP_SECONDS = 5
READER_ANNOTATION_CATEGORIES = {"highlight", "note"}
SAVED_USING = "gen-intelligence"


class ReaderSaveResult(TypedDict):
    """Result of a Reader document CREATE (POST /api/v3/save/)."""

    success: bool
    id: str | None
    url: str | None
    error: str | None
    status_code: int | None


class ReaderUpdateResult(TypedDict):
    """Result of a Reader document PATCH (title only)."""

    success: bool
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


def get_document(
    document_id: str | None,
    *,
    with_html_content: bool = False,
) -> dict | None:
    """GET one Reader document by id via ``/api/v3/list/?id=``.

    Save only returns ``{id, url}``. Title/author/creator come from list.
    Missing token, empty id, and HTTP errors return None and never raise.
    ``with_html_content=True`` adds ``withHtmlContent=true`` so Substack
    note text is available on ``html_content``.
    """
    doc_id = str(document_id).strip() if document_id is not None else ""
    if not doc_id:
        return None
    try:
        headers = _headers()
    except EnvironmentError as exc:
        logger.error("Reader list skipped: %s", exc)
        return None
    params: dict[str, str] = {"id": doc_id}
    if with_html_content:
        params["withHtmlContent"] = "true"
    try:
        response = requests.get(
            LIST_URL,
            params=params,
            headers=headers,
            timeout=60,
        )
        response.raise_for_status()
        data = response.json() if response.content else {}
        results = data.get("results") or []
        if not results:
            logger.info("Reader list returned no document for id=%s", doc_id)
            return None
        return results[0]
    except requests.RequestException as exc:
        logger.error("Failed to fetch Reader document id=%s: %s", doc_id, exc)
        return None


def update_document_title(document_id: str | None, title: str) -> ReaderUpdateResult:
    """PATCH Reader ``/api/v3/update/<id>/`` with ``{"title": title}`` only.

    Never sends location/tags and never uses ``/api/v3/save/``. 429s retry
    with ``Retry-After`` (capped). Failures are returned, never raised.
    """
    result: ReaderUpdateResult = {
        "success": False,
        "error": None,
        "status_code": None,
    }
    doc_id = str(document_id).strip() if document_id is not None else ""
    title_text = str(title).strip() if title is not None else ""
    if not doc_id:
        result["error"] = "Reader update skipped: empty document id"
        return result
    if not title_text:
        result["error"] = "Reader update skipped: empty title"
        return result
    try:
        headers = _headers()
    except EnvironmentError as exc:
        result["error"] = str(exc)
        logger.error("Reader title PATCH skipped: %s", exc)
        return result

    url = UPDATE_URL.format(document_id=doc_id)
    last_error = None
    for attempt in range(1, _UPDATE_MAX_ATTEMPTS + 1):
        try:
            response = requests.patch(
                url,
                headers=headers,
                json={"title": title_text},
                timeout=30,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            logger.error("Failed to PATCH Reader title id=%s: %s", doc_id, exc)
            result["error"] = last_error
            return result
        result["status_code"] = response.status_code
        if response.status_code == 429:
            raw = response.headers.get("Retry-After") or "1"
            try:
                wait = min(max(int(float(raw)), 1), _UPDATE_RETRY_CAP_SECONDS)
            except (TypeError, ValueError):
                wait = 1
            logger.info(
                "Reader title PATCH 429 id=%s attempt=%s sleep=%ss",
                doc_id,
                attempt,
                wait,
            )
            if attempt < _UPDATE_MAX_ATTEMPTS:
                time.sleep(wait)
                continue
            result["error"] = f"Reader title PATCH returned 429 after {attempt} attempts"
            logger.warning("Reader title PATCH gave up on 429 id=%s", doc_id)
            return result
        if response.status_code not in (200, 204):
            result["error"] = (
                f"Reader title PATCH returned {response.status_code}: {response.text}"
            )
            logger.warning(
                "Reader title PATCH failed id=%s status=%s",
                doc_id,
                response.status_code,
            )
            return result
        result["success"] = True
        logger.info("Reader title PATCH ok id=%s", doc_id)
        return result
    result["error"] = last_error or "Reader title PATCH failed"
    return result


def document_title_author(document: dict | None) -> tuple[str | None, str | None]:
    """Reader list ``title`` and ``author``/``creator``. Empty strings → None.

    Site/host authors (``youtube.com``) are treated as missing so YouTube
    stems can fall back to the channel name.
    """
    if not document:
        return None, None
    title = str(document.get("title") or "").strip() or None
    author = (
        str(document.get("author") or "").strip()
        or str(document.get("creator") or "").strip()
        or None
    )
    if author and is_site_host_author(author):
        author = None
    return title, author


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
