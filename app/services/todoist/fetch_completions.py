"""Todoist API client for fetching completed tasks by date range."""

import logging
import os
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TODOIST_API_V1_BASE = "https://api.todoist.com/api/v1"
COMPLETED_TASKS_ENDPOINT = f"{TODOIST_API_V1_BASE}/tasks/completed/by_completion_date"


def _get_access_token() -> str | None:
    """Get Todoist access token from environment."""
    return os.getenv("TODOIST_ACCESS_TOKEN")


def _get_headers() -> dict[str, str]:
    """Get authorization headers for Todoist API."""
    token = _get_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def fetch_completed_tasks(
    since: datetime, until: datetime, limit: int = 200
) -> list[dict]:
    """Fetch completed tasks from Todoist API within the given date range.

    Uses cursor-based pagination to retrieve all results.

    Args:
        since: Start of the date range (inclusive)
        until: End of the date range (inclusive)
        limit: Number of items per page (max 200)

    Returns:
        List of completed task objects with 'content', 'completed_at', 'id', etc.
    """
    token = _get_access_token()
    if not token:
        logger.error("TODOIST_ACCESS_TOKEN not set in environment")
        return []

    since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
    until_str = until.strftime("%Y-%m-%dT%H:%M:%S")

    all_tasks: list[dict] = []
    cursor: str | None = None

    logger.info(f"Fetching completed tasks from {since_str} to {until_str}")

    with httpx.Client(timeout=30.0) as client:
        while True:
            params: dict = {
                "since": since_str,
                "until": until_str,
                "limit": limit,
            }

            if cursor:
                params["cursor"] = cursor

            try:
                response = client.get(
                    COMPLETED_TASKS_ENDPOINT,
                    headers=_get_headers(),
                    params=params,
                )

                if response.status_code != 200:
                    logger.error(
                        f"Failed to fetch completed tasks: {response.status_code} {response.text}"
                    )
                    break

                data = response.json()
                items = data.get("items", [])
                all_tasks.extend(items)

                logger.info(f"Fetched {len(items)} tasks (total: {len(all_tasks)})")

                next_cursor = data.get("next_cursor")
                if not next_cursor:
                    break
                cursor = next_cursor

            except httpx.RequestError as e:
                logger.error(f"Request error: {e}")
                break

    return all_tasks
