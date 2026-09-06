"""Granola public API client.

Docs: https://docs.granola.ai/api-reference/list-notes
Base: https://public-api.granola.ai
"""

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GRANOLA_API_BASE = "https://public-api.granola.ai"
NOTES_URL = f"{GRANOLA_API_BASE}/v1/notes"
DEFAULT_PAGE_SIZE = 30


class GranolaAPIError(Exception):
    """Granola API request failed."""


class GranolaNoteNotFound(GranolaAPIError):
    """GET /v1/notes/{id} returned 404."""


def _headers() -> dict[str, str]:
    token = os.getenv("GRANOLA_API_KEY")
    if not token:
        raise EnvironmentError("GRANOLA_API_KEY not set")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def list_notes_page(
    updated_after: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> dict:
    """Fetch one page of notes. Never logs the API key."""
    params: dict[str, str | int] = {"page_size": page_size}
    if updated_after:
        params["updated_after"] = updated_after
    if cursor:
        params["cursor"] = cursor
    logger.info("Granola list notes request params=%s", params)
    response = requests.get(
        NOTES_URL,
        params=params,
        headers=_headers(),
        timeout=60,
    )
    if response.status_code >= 400:
        raise GranolaAPIError(f"Granola list notes failed: HTTP {response.status_code}")
    return response.json()


def iter_notes(
    updated_after: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
):
    """Yield note dicts from every list page (hasMore / cursor)."""
    cursor = None
    seen_cursors: set[str] = set()
    while True:
        data = list_notes_page(
            updated_after=updated_after,
            page_size=page_size,
            cursor=cursor,
        )
        for note in data.get("notes") or []:
            yield note
        if not data.get("hasMore"):
            break
        cursor = data.get("cursor")
        if not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)


def get_note(note_id: str) -> dict:
    """Fetch a single note. Raises GranolaNoteNotFound on 404."""
    url = f"{NOTES_URL}/{note_id}"
    logger.info("Granola get note id=%s", note_id)
    response = requests.get(url, headers=_headers(), timeout=60)
    if response.status_code == 404:
        raise GranolaNoteNotFound(note_id)
    if response.status_code >= 400:
        raise GranolaAPIError(f"Granola get note failed: HTTP {response.status_code}")
    return response.json()
