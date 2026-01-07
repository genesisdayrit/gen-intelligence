"""Todoist REST API client for task operations."""

import logging
import os
from datetime import date
from typing import TypedDict

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TODOIST_API_BASE = "https://api.todoist.com/rest/v2"


class TodoistTaskResult(TypedDict):
    """Result of a Todoist task operation."""

    success: bool
    task_id: str | None
    error: str | None


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


def create_todoist_task(
    content: str, description: str | None = None
) -> TodoistTaskResult:
    """Create a new task in Todoist with today's due date.

    Args:
        content: Task content/title
        description: Optional task description/notes

    Returns:
        TodoistTaskResult with success status, task_id, and error message
    """
    token = _get_access_token()
    if not token:
        return TodoistTaskResult(
            success=False,
            task_id=None,
            error="TODOIST_ACCESS_TOKEN not set in environment",
        )

    today = date.today().isoformat()

    body: dict = {
        "content": content,
        "due_date": today,
    }

    if description:
        body["description"] = description

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                f"{TODOIST_API_BASE}/tasks",
                headers=_get_headers(),
                json=body,
            )

            if response.status_code != 200:
                error_text = response.text
                logger.error(
                    "Failed to create Todoist task: %s %s",
                    response.status_code,
                    error_text,
                )
                return TodoistTaskResult(
                    success=False,
                    task_id=None,
                    error=f"Todoist API returned status {response.status_code}: {error_text}",
                )

            task_data = response.json()
            task_id = task_data.get("id")
            logger.info(
                "Created Todoist task: id=%s content=%s", task_id, content[:50]
            )
            return TodoistTaskResult(
                success=True,
                task_id=task_id,
                error=None,
            )
    except httpx.RequestError as e:
        logger.error("Failed to connect to Todoist: %s", e)
        return TodoistTaskResult(
            success=False,
            task_id=None,
            error=f"Failed to connect to Todoist: {e}",
        )


def complete_todoist_task(task_id: str) -> bool:
    """Mark a task as completed in Todoist.

    Args:
        task_id: The Todoist task ID

    Returns:
        True if task was completed successfully, False otherwise
    """
    token = _get_access_token()
    if not token:
        logger.error("TODOIST_ACCESS_TOKEN not set")
        return False

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                f"{TODOIST_API_BASE}/tasks/{task_id}/close",
                headers=_get_headers(),
            )

            if response.status_code == 204:
                logger.info("Completed Todoist task: id=%s", task_id)
                return True
            else:
                logger.error(
                    "Failed to complete Todoist task %s: %s %s",
                    task_id,
                    response.status_code,
                    response.text,
                )
                return False
    except httpx.RequestError as e:
        logger.error("Failed to connect to Todoist: %s", e)
        return False


def create_completed_todoist_task(
    content: str, description: str | None = None
) -> TodoistTaskResult:
    """Create a task in Todoist and immediately mark it as completed.

    This is used to record completed items from external sources (like Linear)
    through Todoist's webhook system.

    Args:
        content: Task content/title
        description: Optional task description/notes

    Returns:
        TodoistTaskResult with success status, task_id, and error message
    """
    # First create the task
    create_result = create_todoist_task(content, description)

    if not create_result["success"]:
        return create_result

    # Then mark it as completed
    task_id = create_result["task_id"]
    if task_id:
        completed = complete_todoist_task(task_id)
        if not completed:
            return TodoistTaskResult(
                success=False,
                task_id=task_id,
                error="Task created but failed to mark as completed",
            )

    return create_result
