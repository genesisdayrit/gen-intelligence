"""Sync completed Linear issues to Todoist.

This script:
1. Queries all Linear issues completed in the last 24 hours
2. Checks against Todoist completed tasks (last 7 days) for matches
3. For any Linear issue not found in Todoist, creates a completed task

This helps patch missing Todoist completions from Linear issues.

Usage:
    python sync_completed_to_todoist.py
    python sync_completed_to_todoist.py --dry-run  # Preview without creating tasks
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

import httpx
import pytz
from dotenv import load_dotenv

# Add parent directories to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.linear.sync_utils import execute_query
from services.todoist.client import create_completed_todoist_task

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# --- Constants ---
TODOIST_API_V1_BASE = "https://api.todoist.com/api/v1"
COMPLETED_TASKS_ENDPOINT = f"{TODOIST_API_V1_BASE}/tasks/completed/by_completion_date"

# GraphQL query for completed issues
# Uses DateTimeOrDuration type for completedAt filter and orderBy updatedAt
COMPLETED_ISSUES_QUERY = """
query CompletedIssues($completedAfter: DateTimeOrDuration!, $first: Int!, $after: String) {
  issues(
    filter: {
      completedAt: { gte: $completedAfter }
    }
    first: $first
    after: $after
    orderBy: updatedAt
  ) {
    nodes {
      id
      identifier
      title
      completedAt
      url
      state {
        name
        type
      }
      assignee {
        name
        email
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def get_todoist_headers() -> dict[str, str]:
    """Get authorization headers for Todoist API."""
    token = os.getenv("TODOIST_ACCESS_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def fetch_completed_linear_issues(hours: int = 24) -> list[dict]:
    """Fetch Linear issues completed in the last N hours.

    Args:
        hours: Number of hours to look back (default 24)

    Returns:
        List of completed issue objects
    """
    timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Pacific")
    system_tz = pytz.timezone(timezone_str)
    now = datetime.now(system_tz)
    completed_after = now - timedelta(hours=hours)

    # Format for GraphQL DateTime
    completed_after_str = completed_after.strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(f"Fetching Linear issues completed after {completed_after_str}")

    all_issues: list[dict] = []
    after_cursor = None

    while True:
        variables = {
            "completedAfter": completed_after_str,
            "first": 50,
            "after": after_cursor,
        }

        try:
            data = execute_query(COMPLETED_ISSUES_QUERY, variables)
            issues_data = data["data"]["issues"]
            issues = issues_data["nodes"]
            all_issues.extend(issues)

            logger.info(f"Fetched {len(issues)} issues (total: {len(all_issues)})")

            page_info = issues_data["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            after_cursor = page_info["endCursor"]

        except Exception as e:
            logger.error(f"Error fetching Linear issues: {e}")
            break

    return all_issues


def fetch_completed_todoist_tasks(days: int = 7) -> list[dict]:
    """Fetch completed Todoist tasks from the last N days.

    Args:
        days: Number of days to look back (default 7)

    Returns:
        List of completed task objects
    """
    token = os.getenv("TODOIST_ACCESS_TOKEN")
    if not token:
        logger.error("TODOIST_ACCESS_TOKEN not set in environment")
        return []

    timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Pacific")
    system_tz = pytz.timezone(timezone_str)
    now = datetime.now(system_tz)

    since = now - timedelta(days=days)
    until = now

    since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
    until_str = until.strftime("%Y-%m-%dT%H:%M:%S")

    logger.info(f"Fetching Todoist completed tasks from {since_str} to {until_str}")

    all_tasks: list[dict] = []
    cursor: str | None = None

    with httpx.Client(timeout=30.0) as client:
        while True:
            params: dict = {
                "since": since_str,
                "until": until_str,
                "limit": 200,
            }

            if cursor:
                params["cursor"] = cursor

            try:
                response = client.get(
                    COMPLETED_TASKS_ENDPOINT,
                    headers=get_todoist_headers(),
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

                logger.info(f"Fetched {len(items)} Todoist tasks (total: {len(all_tasks)})")

                next_cursor = data.get("next_cursor")
                if not next_cursor:
                    break
                cursor = next_cursor

            except httpx.RequestError as e:
                logger.error(f"Request error: {e}")
                break

    return all_tasks


def normalize_text(text: str) -> str:
    """Normalize text for comparison by lowercasing and removing extra whitespace."""
    return " ".join(text.lower().split())


def find_matching_todoist_task(
    linear_issue: dict, todoist_tasks: list[dict]
) -> dict | None:
    """Find a Todoist task that matches the Linear issue.

    Matching is done by checking if the Linear issue title or identifier
    appears in the Todoist task content.

    Args:
        linear_issue: Linear issue object
        todoist_tasks: List of Todoist completed task objects

    Returns:
        Matching Todoist task or None
    """
    issue_title = normalize_text(linear_issue.get("title", ""))
    issue_identifier = linear_issue.get("identifier", "").lower()

    for task in todoist_tasks:
        task_content = normalize_text(task.get("content", ""))

        # Check for title match (fuzzy - title contained in task content)
        if issue_title and issue_title in task_content:
            return task

        # Check for identifier match (e.g., "GEN-123" in task content)
        if issue_identifier and issue_identifier in task_content:
            return task

    return None


def sync_completed_issues_to_todoist(dry_run: bool = False) -> dict:
    """Main sync function to patch missing Todoist completions.

    Args:
        dry_run: If True, only preview what would be created without making changes

    Returns:
        Dict with sync statistics
    """
    stats = {
        "linear_issues_found": 0,
        "todoist_tasks_found": 0,
        "already_matched": 0,
        "tasks_created": 0,
        "tasks_failed": 0,
    }

    # Fetch completed Linear issues from last 24 hours
    linear_issues = fetch_completed_linear_issues(hours=24)
    stats["linear_issues_found"] = len(linear_issues)

    if not linear_issues:
        logger.info("No completed Linear issues found in the last 24 hours")
        return stats

    # Fetch completed Todoist tasks from last 7 days (wider range for safety)
    todoist_tasks = fetch_completed_todoist_tasks(days=7)
    stats["todoist_tasks_found"] = len(todoist_tasks)

    # Process each Linear issue
    issues_to_create = []

    for issue in linear_issues:
        identifier = issue.get("identifier", "Unknown")
        title = issue.get("title", "Unknown")
        url = issue.get("url", "")

        matching_task = find_matching_todoist_task(issue, todoist_tasks)

        if matching_task:
            logger.info(f"[MATCH] {identifier}: {title}")
            logger.debug(f"  Matched Todoist task: {matching_task.get('content', '')[:50]}")
            stats["already_matched"] += 1
        else:
            logger.info(f"[MISSING] {identifier}: {title}")
            issues_to_create.append(issue)

    # Print summary before creating tasks
    print("\n" + "=" * 70)
    print("SYNC SUMMARY")
    print("=" * 70)
    print(f"Linear issues completed (last 24h): {stats['linear_issues_found']}")
    print(f"Todoist completed tasks (last 7d):  {stats['todoist_tasks_found']}")
    print(f"Already matched:                    {stats['already_matched']}")
    print(f"Missing in Todoist:                 {len(issues_to_create)}")
    print("=" * 70)

    if not issues_to_create:
        print("\nAll Linear completions are already in Todoist!")
        return stats

    print(f"\nTasks to create ({len(issues_to_create)}):")
    for issue in issues_to_create:
        identifier = issue.get("identifier", "")
        title = issue.get("title", "")
        print(f"  - [{identifier}] {title}")

    if dry_run:
        print("\n[DRY RUN] No tasks were created.")
        return stats

    print("\nCreating tasks in Todoist...")

    # Create completed tasks in Todoist
    for issue in issues_to_create:
        identifier = issue.get("identifier", "")
        title = issue.get("title", "")
        url = issue.get("url", "")

        # Task content format: "[GEN-123] Issue Title"
        task_content = f"[{identifier}] {title}"
        task_description = f"Linear: {url}" if url else None

        result = create_completed_todoist_task(task_content, task_description)

        if result["success"]:
            logger.info(f"[CREATED] {task_content}")
            stats["tasks_created"] += 1
        else:
            logger.error(f"[FAILED] {task_content}: {result['error']}")
            stats["tasks_failed"] += 1

    print("\n" + "=" * 70)
    print("FINAL RESULTS")
    print("=" * 70)
    print(f"Tasks created:     {stats['tasks_created']}")
    print(f"Tasks failed:      {stats['tasks_failed']}")
    print("=" * 70)

    return stats


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync completed Linear issues to Todoist"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be created without making changes",
    )
    args = parser.parse_args()

    logger.info("Starting Linear to Todoist completion sync...")

    stats = sync_completed_issues_to_todoist(dry_run=args.dry_run)

    logger.info("Sync completed")
    return stats


if __name__ == "__main__":
    main()
