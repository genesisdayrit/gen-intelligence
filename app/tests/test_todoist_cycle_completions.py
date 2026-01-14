"""Test script for fetching completed Todoist tasks within a cycle date range.

This script retrieves completed tasks from Todoist using the API v1 endpoint
`/api/v1/tasks/completed/by_completion_date`, filtering by the current or
previous weekly cycle (Wednesday-Tuesday).

Usage:
    python test_todoist_cycle_completions.py              # Current cycle
    python test_todoist_cycle_completions.py --previous   # Previous cycle
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytz
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.obsidian.utils.date_helpers import DAY_ROLLOVER_HOUR, get_effective_date

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


def get_access_token() -> str | None:
    """Get Todoist access token from environment."""
    return os.getenv("TODOIST_ACCESS_TOKEN")


def get_headers() -> dict[str, str]:
    """Get authorization headers for Todoist API."""
    token = get_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def get_cycle_bounds(tz, previous: bool = False) -> tuple[datetime, datetime]:
    """Calculate the Wednesday-Tuesday bounds for a weekly cycle.

    Args:
        tz: Timezone for calculating effective date
        previous: If True, return the previous cycle's bounds

    Returns:
        Tuple of (cycle_start, cycle_end) as timezone-aware datetimes
    """
    now = datetime.now(tz)
    effective_now = get_effective_date(now)

    # Wednesday is weekday 2 (Monday=0, Tuesday=1, Wednesday=2, ...)
    # Calculate days since the most recent Wednesday (including today if it's Wednesday)
    days_since_wednesday = (effective_now.weekday() - 2) % 7

    cycle_start = effective_now - timedelta(days=days_since_wednesday)
    cycle_end = cycle_start + timedelta(days=6)  # Tuesday

    if previous:
        # Shift back by 7 days for the previous cycle
        cycle_start = cycle_start - timedelta(days=7)
        cycle_end = cycle_end - timedelta(days=7)

    # Set times: cycle_start at midnight, cycle_end at 23:59:59
    cycle_start = cycle_start.replace(hour=0, minute=0, second=0, microsecond=0)
    cycle_end = cycle_end.replace(hour=23, minute=59, second=59, microsecond=0)

    return cycle_start, cycle_end


def format_date_range(cycle_start: datetime, cycle_end: datetime) -> str:
    """Format the date range string for display.

    Format: (Jan. 07 - Jan. 13, 2026)
    """
    start_str = f"{cycle_start.strftime('%b')}. {cycle_start.strftime('%d')}"
    end_str = f"{cycle_end.strftime('%b')}. {cycle_end.strftime('%d')}, {cycle_end.strftime('%Y')}"
    return f"({start_str} - {end_str})"


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
        List of completed task objects
    """
    token = get_access_token()
    if not token:
        logger.error("TODOIST_ACCESS_TOKEN not set in environment")
        return []

    # Format dates for API (ISO 8601 format)
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
                    headers=get_headers(),
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

                # Check for pagination
                next_cursor = data.get("next_cursor")
                if not next_cursor:
                    break
                cursor = next_cursor

            except httpx.RequestError as e:
                logger.error(f"Request error: {e}")
                break

    return all_tasks


def save_results(tasks: list[dict], cycle_start: datetime, cycle_end: datetime, previous: bool) -> Path:
    """Save the fetched tasks to a timestamped JSON file.

    Args:
        tasks: List of task objects to save
        cycle_start: Start of the cycle (for metadata)
        cycle_end: End of the cycle (for metadata)
        previous: Whether this is the previous cycle

    Returns:
        Path to the saved file
    """
    # Ensure tests/data directory exists
    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Create timestamped filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cycle_type = "previous" if previous else "current"
    filename = f"{timestamp}_todoist_completed_{cycle_type}_cycle.json"
    file_path = data_dir / filename

    # Build output structure
    output = {
        "metadata": {
            "fetched_at": datetime.now().isoformat(),
            "cycle_type": cycle_type,
            "cycle_start": cycle_start.isoformat(),
            "cycle_end": cycle_end.isoformat(),
            "cycle_range": format_date_range(cycle_start, cycle_end),
            "total_tasks": len(tasks),
        },
        "tasks": tasks,
    }

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info(f"Saved {len(tasks)} tasks to {file_path}")
    return file_path


def main():
    """Main entry point for the test script."""
    parser = argparse.ArgumentParser(
        description="Fetch completed Todoist tasks for a weekly cycle"
    )
    parser.add_argument(
        "--previous",
        action="store_true",
        help="Fetch tasks from the previous cycle instead of current",
    )
    args = parser.parse_args()

    # Get timezone from environment
    timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Pacific")
    system_tz = pytz.timezone(timezone_str)
    logger.info(f"Using timezone: {timezone_str}")

    # Calculate cycle bounds
    cycle_start, cycle_end = get_cycle_bounds(system_tz, previous=args.previous)
    date_range = format_date_range(cycle_start, cycle_end)

    cycle_type = "previous" if args.previous else "current"
    logger.info(f"Fetching {cycle_type} cycle: {date_range}")
    logger.info(f"  Start: {cycle_start.isoformat()}")
    logger.info(f"  End:   {cycle_end.isoformat()}")

    # Fetch completed tasks
    tasks = fetch_completed_tasks(cycle_start, cycle_end)

    if not tasks:
        logger.warning("No completed tasks found for this cycle")
    else:
        logger.info(f"Found {len(tasks)} completed tasks")

        # Print summary
        print("\n" + "=" * 60)
        print(f"COMPLETED TASKS - {date_range}")
        print("=" * 60)
        for task in tasks:
            content = task.get("content", "Unknown")
            completed_at = task.get("completed_at", "Unknown")
            print(f"- {content}")
            print(f"  Completed: {completed_at}")
        print("=" * 60 + "\n")

    # Save results
    file_path = save_results(tasks, cycle_start, cycle_end, args.previous)
    print(f"Results saved to: {file_path}")


if __name__ == "__main__":
    main()
