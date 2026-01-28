"""Unified function to append completed tasks to both Daily Action and Weekly Cycle."""

import logging
from datetime import datetime

from services.obsidian.add_todoist_completed import append_todoist_completed
from services.obsidian.add_weekly_cycle_completed import append_weekly_cycle_completed

logger = logging.getLogger(__name__)


def append_completed_task(task_content: str, target_dt: datetime | None = None) -> dict:
    """Append completed task to both Daily Action and Weekly Cycle.

    Writes to Daily Action first, then Weekly Cycle. Both operations are
    independent - if one fails, the other's success/failure is unaffected.

    Args:
        task_content: The task text to add
        target_dt: Optional timezone-aware datetime for file routing and timestamp.
                   When None, uses datetime.now() (real-time webhook behavior).

    Returns:
        dict with keys:
            - daily_action_success: bool
            - weekly_cycle_success: bool
            - daily_action_error: str | None
            - weekly_cycle_error: str | None
    """
    result = {
        "daily_action_success": False,
        "weekly_cycle_success": False,
        "daily_action_error": None,
        "weekly_cycle_error": None,
    }

    # Write to Daily Action
    try:
        append_todoist_completed(task_content, target_dt)
        result["daily_action_success"] = True
        logger.info("Written to Daily Action")
    except Exception as e:
        result["daily_action_error"] = str(e)
        logger.error("Failed to write to Daily Action: %s", e)

    # Write to Weekly Cycle
    try:
        append_weekly_cycle_completed(task_content, target_dt)
        result["weekly_cycle_success"] = True
        logger.info("Written to Weekly Cycle")
    except Exception as e:
        result["weekly_cycle_error"] = str(e)
        logger.error("Failed to write to Weekly Cycle: %s", e)

    return result
