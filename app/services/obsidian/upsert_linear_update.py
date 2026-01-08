"""Unified function to upsert Linear updates to both Daily Action and Weekly Cycle."""

import logging

from services.obsidian.add_daily_action_updates import upsert_daily_action_update
from services.obsidian.add_weekly_cycle_updates import upsert_weekly_cycle_update

logger = logging.getLogger(__name__)


def upsert_linear_update(section_type: str, url: str, parent_name: str, content: str) -> dict:
    """Upsert Linear update to both Daily Action and Weekly Cycle.

    Writes to Daily Action first, then Weekly Cycle. Both operations are
    independent - if one fails, the other's success/failure is unaffected.

    Args:
        section_type: Either "initiative" or "project"
        url: The Linear URL for the update
        parent_name: The name of the initiative or project
        content: The update body text

    Returns:
        dict with keys:
            - daily_action_success: bool
            - daily_action_action: str | None ("inserted" or "updated")
            - daily_action_error: str | None
            - weekly_cycle_success: bool
            - weekly_cycle_action: str | None ("inserted" or "updated")
            - weekly_cycle_error: str | None
    """
    result = {
        "daily_action_success": False,
        "daily_action_action": None,
        "daily_action_error": None,
        "weekly_cycle_success": False,
        "weekly_cycle_action": None,
        "weekly_cycle_error": None,
    }

    # Write to Daily Action
    try:
        da_result = upsert_daily_action_update(section_type, url, parent_name, content)
        result["daily_action_success"] = da_result["success"]
        result["daily_action_action"] = da_result.get("action")
        if not da_result["success"]:
            result["daily_action_error"] = da_result.get("error")
        else:
            logger.info("Written to Daily Action: action=%s", da_result["action"])
    except Exception as e:
        result["daily_action_error"] = str(e)
        logger.error("Failed to write to Daily Action: %s", e)

    # Write to Weekly Cycle
    try:
        wc_result = upsert_weekly_cycle_update(section_type, url, parent_name, content)
        result["weekly_cycle_success"] = wc_result["success"]
        result["weekly_cycle_action"] = wc_result.get("action")
        if not wc_result["success"]:
            result["weekly_cycle_error"] = wc_result.get("error")
        else:
            logger.info("Written to Weekly Cycle: action=%s", wc_result["action"])
    except Exception as e:
        result["weekly_cycle_error"] = str(e)
        logger.error("Failed to write to Weekly Cycle: %s", e)

    return result
