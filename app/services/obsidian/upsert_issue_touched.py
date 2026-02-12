"""Unified function to upsert Linear issues touched to both Daily Action and Weekly Cycle."""

import logging

from services.obsidian.add_daily_action_issues_touched import upsert_daily_action_issue_touched
from services.obsidian.add_weekly_cycle_issues_touched import upsert_weekly_cycle_issue_touched

logger = logging.getLogger(__name__)


def upsert_issue_touched(
    issue_identifier: str,
    project_name: str,
    issue_title: str,
    status_name: str,
    issue_url: str,
    status_changed: bool,
) -> dict:
    """Upsert Linear issue touched to both Daily Action and Weekly Cycle.

    Writes to Daily Action first, then Weekly Cycle. Both operations are
    independent - if one fails, the other's success/failure is unaffected.

    Args:
        issue_identifier: Human-readable issue ID (e.g., "GD-328")
        project_name: Parent project name (may be empty)
        issue_title: Issue title
        status_name: Current workflow state name (e.g., "In Progress")
        issue_url: Linear URL for the issue
        status_changed: Whether the status was updated in this webhook event

    Returns:
        dict with keys:
            - daily_action_success: bool
            - daily_action_action: str | None ("inserted", "updated", or "skipped")
            - daily_action_error: str | None
            - weekly_cycle_success: bool
            - weekly_cycle_action: str | None ("inserted", "updated", or "skipped")
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
        da_result = upsert_daily_action_issue_touched(
            issue_identifier, project_name, issue_title, status_name, issue_url, status_changed
        )
        result["daily_action_success"] = da_result["success"]
        result["daily_action_action"] = da_result.get("action")
        if not da_result["success"]:
            result["daily_action_error"] = da_result.get("error")
        else:
            logger.info("Issues touched written to Daily Action: action=%s", da_result["action"])
    except Exception as e:
        result["daily_action_error"] = str(e)
        logger.error("Failed to write issues touched to Daily Action: %s", e)

    # Write to Weekly Cycle
    try:
        wc_result = upsert_weekly_cycle_issue_touched(
            issue_identifier, project_name, issue_title, status_name, issue_url, status_changed
        )
        result["weekly_cycle_success"] = wc_result["success"]
        result["weekly_cycle_action"] = wc_result.get("action")
        if not wc_result["success"]:
            result["weekly_cycle_error"] = wc_result.get("error")
        else:
            logger.info("Issues touched written to Weekly Cycle: action=%s", wc_result["action"])
    except Exception as e:
        result["weekly_cycle_error"] = str(e)
        logger.error("Failed to write issues touched to Weekly Cycle: %s", e)

    return result
