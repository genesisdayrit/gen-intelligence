"""Raindrop.io REST API client for bookmark operations."""

import logging
import os
from typing import TypedDict

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

RAINDROP_API_BASE = "https://api.raindrop.io/rest/v1"
UNSORTED_COLLECTION_ID = -1


class RaindropBookmarkResult(TypedDict):
    """Result of a Raindrop.io bookmark operation."""

    success: bool
    bookmark_id: str | None
    error: str | None


def _get_access_token() -> str | None:
    """Get Raindrop.io access token from environment."""
    return os.getenv("RAINDROP_IO_TEST_TOKEN")


def _get_headers() -> dict[str, str]:
    """Get authorization headers for Raindrop.io API."""
    token = _get_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def create_bookmark(url: str, title: str | None = None) -> RaindropBookmarkResult:
    """Save a URL to Raindrop.io unsorted collection.

    Args:
        url: The URL to bookmark
        title: Optional title for the bookmark. Raindrop.io will fetch one if omitted.

    Returns:
        RaindropBookmarkResult with success status, bookmark_id, and error message
    """
    token = _get_access_token()
    if not token:
        return RaindropBookmarkResult(
            success=False,
            bookmark_id=None,
            error="RAINDROP_IO_TEST_TOKEN not set in environment",
        )

    body: dict = {
        "link": url,
        "collectionId": UNSORTED_COLLECTION_ID,
    }

    if title:
        body["title"] = title

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                f"{RAINDROP_API_BASE}/raindrop",
                headers=_get_headers(),
                json=body,
            )

            if response.status_code != 200:
                error_text = response.text
                logger.error(
                    "Failed to create Raindrop.io bookmark: %s %s",
                    response.status_code,
                    error_text,
                )
                return RaindropBookmarkResult(
                    success=False,
                    bookmark_id=None,
                    error=f"Raindrop.io API returned status {response.status_code}: {error_text}",
                )

            data = response.json()
            bookmark_id = str(data.get("item", {}).get("_id", ""))
            logger.info(
                "Created Raindrop.io bookmark: id=%s url=%s", bookmark_id, url[:100]
            )
            return RaindropBookmarkResult(
                success=True,
                bookmark_id=bookmark_id,
                error=None,
            )

    except httpx.RequestError as e:
        logger.error("Failed to connect to Raindrop.io: %s", e)
        return RaindropBookmarkResult(
            success=False,
            bookmark_id=None,
            error=f"Failed to connect to Raindrop.io: {e}",
        )
