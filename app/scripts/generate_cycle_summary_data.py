#!/usr/bin/env python3
"""Weekly Cycle Summarizer - Fetch Linear data for cycle reporting.

Usage:
    python -m scripts.generate_cycle_summary_data                    # Current cycle
    python -m scripts.generate_cycle_summary_data --current-cycle    # Current cycle (explicit)
    python -m scripts.generate_cycle_summary_data --previous-cycle   # Previous cycle
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from dotenv import load_dotenv

# Add app directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.linear.sync_utils import (
    execute_query,
    fetch_all_pages,
    fetch_initiatives,
    fetch_initiative_updates,
    fetch_initiative_projects,
    fetch_project_updates,
)
from services.obsidian.utils.date_helpers import get_effective_date

logger = logging.getLogger(__name__)

# =============================================================================
# GraphQL Queries for Date-Filtered Data
# =============================================================================

ISSUES_FILTERED_QUERY = """
query IssuesFiltered($first: Int!, $after: String, $filter: IssueFilter) {
  issues(first: $first, after: $after, filter: $filter) {
    nodes {
      id
      identifier
      title
      description
      priority
      createdAt
      updatedAt
      completedAt
      url
      state { id name type }
      assignee { id name email }
      project { id name }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

PROJECT_ISSUES_FILTERED_QUERY = """
query ProjectIssuesFiltered($projectId: String!, $first: Int!, $after: String, $filter: IssueFilter) {
  project(id: $projectId) {
    issues(first: $first, after: $after, filter: $filter) {
      nodes {
        id
        identifier
        title
        description
        priority
        createdAt
        updatedAt
        completedAt
        url
        state { id name type }
        assignee { id name email }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PROJECTS_FILTERED_QUERY = """
query ProjectsFiltered($first: Int!, $after: String, $filter: ProjectFilter) {
  projects(first: $first, after: $after, filter: $filter) {
    nodes {
      id
      name
      slugId
      url
      state
      createdAt
      lead { id name email }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# =============================================================================
# Cycle Date Calculation
# =============================================================================


def get_cycle_bounds(cycle_type: str, tz) -> tuple[datetime, datetime]:
    """Get cycle date bounds.

    Args:
        cycle_type: 'current' or 'previous'
        tz: timezone object

    Returns:
        (cycle_start, cycle_end) as datetime objects
    """
    now = datetime.now(tz)
    effective_now = get_effective_date(now)  # 3am buffer

    # Wednesday is weekday 2 (Monday=0, Tuesday=1, Wednesday=2, ...)
    days_since_wednesday = (effective_now.weekday() - 2) % 7

    if cycle_type == "previous":
        cycle_start = effective_now - timedelta(days=days_since_wednesday + 7)
    else:  # current
        cycle_start = effective_now - timedelta(days=days_since_wednesday)

    cycle_end = cycle_start + timedelta(days=6)  # Tuesday
    return cycle_start, cycle_end


# =============================================================================
# Date Filtering Helpers
# =============================================================================


def is_within_cycle(date_str: str | None, start: datetime, end: datetime) -> bool:
    """Check if an ISO datetime string falls within the cycle range."""
    if not date_str:
        return False
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return start.date() <= dt.date() <= end.date()
    except ValueError:
        return False


# =============================================================================
# Data Fetching Functions
# =============================================================================


def fetch_project_issues_in_range(
    project_id: str, start: datetime, end: datetime, issue_type: str
) -> list[dict]:
    """Fetch project issues with date filter.

    Args:
        project_id: Linear project ID
        start: Cycle start datetime
        end: Cycle end datetime
        issue_type: 'completed', 'created', or 'modified'

    Returns:
        List of issues matching the filter
    """
    field_map = {
        "completed": "completedAt",
        "created": "createdAt",
        "modified": "updatedAt",
    }
    field = field_map[issue_type]

    filter_var = {
        field: {
            "gte": start.strftime("%Y-%m-%dT00:00:00Z"),
            "lte": end.strftime("%Y-%m-%dT23:59:59Z"),
        }
    }

    logger.debug(f"Fetching {issue_type} issues for project {project_id}")

    return fetch_all_pages(
        PROJECT_ISSUES_FILTERED_QUERY,
        {"projectId": project_id, "first": 100, "filter": filter_var},
        ["project", "issues"],
    )


def fetch_all_completed_issues_in_range(
    start: datetime, end: datetime
) -> list[dict]:
    """Fetch ALL completed issues in cycle (for 'other' calculation)."""
    filter_var = {
        "completedAt": {
            "gte": start.strftime("%Y-%m-%dT00:00:00Z"),
            "lte": end.strftime("%Y-%m-%dT23:59:59Z"),
        }
    }

    logger.debug("Fetching all completed issues in range")

    return fetch_all_pages(
        ISSUES_FILTERED_QUERY,
        {"first": 100, "filter": filter_var},
        ["issues"],
    )


def fetch_newly_created_projects(start: datetime, end: datetime) -> list[dict]:
    """Fetch projects created within cycle."""
    filter_var = {
        "createdAt": {
            "gte": start.strftime("%Y-%m-%dT00:00:00Z"),
            "lte": end.strftime("%Y-%m-%dT23:59:59Z"),
        }
    }

    logger.debug("Fetching newly created projects")

    return fetch_all_pages(
        PROJECTS_FILTERED_QUERY,
        {"first": 100, "filter": filter_var},
        ["projects"],
    )


# =============================================================================
# Initiative Enrichment
# =============================================================================


def enrich_initiative_for_cycle(
    initiative: dict, start: datetime, end: datetime
) -> dict | None:
    """Enrich an initiative with cycle-specific updates and issues.

    Returns None if the initiative cannot be accessed.
    """
    initiative_id = initiative["id"]
    initiative_name = initiative.get("name", "Unknown")

    logger.debug(f"Enriching initiative: {initiative_name}")

    # Fetch all updates, filter to cycle in Python (low volume)
    try:
        all_updates = fetch_initiative_updates(initiative_id)
    except Exception as e:
        logger.warning(f"Could not access initiative '{initiative_name}': {e}")
        return None

    initiative["updates_in_cycle"] = [
        u for u in all_updates if is_within_cycle(u.get("createdAt"), start, end)
    ]

    # Capture the latest update (most recent overall, not just in cycle)
    if all_updates:
        initiative["latest_update"] = max(
            all_updates, key=lambda u: u.get("createdAt", "")
        )
    else:
        initiative["latest_update"] = None

    logger.debug(
        f"  Found {len(initiative['updates_in_cycle'])} updates in cycle"
    )

    # Fetch projects
    projects = fetch_initiative_projects(initiative_id)

    for project in projects:
        project_id = project["id"]
        project_name = project.get("name", "Unknown")

        logger.debug(f"  Processing project: {project_name}")

        # Project updates (filter in Python)
        all_proj_updates = fetch_project_updates(project_id)
        project["updates_in_cycle"] = [
            u
            for u in all_proj_updates
            if is_within_cycle(u.get("createdAt"), start, end)
        ]

        # Issues (use GraphQL filtering for efficiency)
        project["completed_issues"] = fetch_project_issues_in_range(
            project_id, start, end, "completed"
        )
        project["created_issues"] = fetch_project_issues_in_range(
            project_id, start, end, "created"
        )
        project["modified_issues"] = fetch_project_issues_in_range(
            project_id, start, end, "modified"
        )

        logger.debug(
            f"    Completed: {len(project['completed_issues'])}, "
            f"Created: {len(project['created_issues'])}, "
            f"Modified: {len(project['modified_issues'])}"
        )

    initiative["projects"] = projects
    return initiative


# =============================================================================
# Main Data Collection
# =============================================================================


def collect_cycle_data(cycle_start: datetime, cycle_end: datetime) -> dict:
    """Collect all cycle data from Linear."""
    logger.info("Fetching all initiatives...")

    # 1. Get all initiatives (including archived to detect newly created)
    all_initiatives = fetch_initiatives(include_archived=True)
    logger.info(f"Found {len(all_initiatives)} total initiatives")

    # 2. Filter to active initiatives for main enrichment
    active_initiatives = [
        i for i in all_initiatives if i.get("status") == "Active"
    ]
    logger.info(f"Found {len(active_initiatives)} active initiatives")

    # 3. Track project IDs in active initiatives
    active_project_ids = set()

    # 4. Enrich each active initiative
    enriched = []
    for i, init in enumerate(active_initiatives):
        logger.info(
            f"Processing initiative {i + 1}/{len(active_initiatives)}: "
            f"{init.get('name', 'Unknown')}"
        )
        data = enrich_initiative_for_cycle(init, cycle_start, cycle_end)
        if data is None:
            continue  # Skip initiatives that couldn't be accessed
        enriched.append(data)
        for proj in data.get("projects", []):
            active_project_ids.add(proj["id"])

    # 5. Get "other" completed issues (not in active initiatives)
    logger.info("Fetching all completed issues in range...")
    all_completed = fetch_all_completed_issues_in_range(cycle_start, cycle_end)
    other_completed = [
        i
        for i in all_completed
        if i.get("project", {}).get("id") not in active_project_ids
    ]
    logger.info(
        f"Found {len(all_completed)} total completed issues, "
        f"{len(other_completed)} outside active initiatives"
    )

    # 6. Newly created initiatives
    newly_created_initiatives = [
        i
        for i in all_initiatives
        if is_within_cycle(i.get("createdAt"), cycle_start, cycle_end)
    ]
    logger.info(
        f"Found {len(newly_created_initiatives)} newly created initiatives"
    )

    # 7. Newly created projects
    logger.info("Fetching newly created projects...")
    newly_created_projects = fetch_newly_created_projects(cycle_start, cycle_end)
    logger.info(f"Found {len(newly_created_projects)} newly created projects")

    # 8. Build summary of latest initiative updates (one per active initiative)
    latest_initiative_updates = []
    for init in enriched:
        if init.get("latest_update"):
            latest_initiative_updates.append({
                "initiative_id": init["id"],
                "initiative_name": init["name"],
                "update": init["latest_update"],
            })
    logger.info(
        f"Found {len(latest_initiative_updates)} initiatives with updates"
    )

    return {
        "cycle_start_date": cycle_start.strftime("%Y-%m-%d"),
        "cycle_end_date": cycle_end.strftime("%Y-%m-%d"),
        "latest_initiative_updates": latest_initiative_updates,
        "active_initiatives": enriched,
        "other_completed_issues": other_completed,
        "newly_created_initiatives": newly_created_initiatives,
        "newly_created_projects": newly_created_projects,
    }


# =============================================================================
# Output
# =============================================================================


def save_summary(
    summary: dict, cycle_start: datetime, cycle_end: datetime
) -> Path:
    """Save summary to timestamped JSON file."""
    # Save to tests/data/ directory for consistency with other test scripts
    data_dir = Path(__file__).parent.parent / "tests" / "data"
    # Handle symlinks and regular directories
    if not data_dir.exists() and not data_dir.is_symlink():
        data_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cycle_range = (
        f"{cycle_start.strftime('%Y%m%d')}-{cycle_end.strftime('%Y%m%d')}"
    )
    output_file = data_dir / f"{timestamp}_cycle_summary_{cycle_range}.json"

    with open(output_file, "w") as f:
        json.dump(summary, f, indent=2)

    logger.info(f"Saved cycle summary to: {output_file}")
    return output_file


# =============================================================================
# Main Entry Point
# =============================================================================


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Generate cycle summary data from Linear"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--current-cycle",
        action="store_true",
        help="Fetch data for current cycle (default)",
    )
    group.add_argument(
        "--previous-cycle",
        action="store_true",
        help="Fetch data for previous cycle",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging"
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    # Determine cycle
    tz = pytz.timezone(os.getenv("SYSTEM_TIMEZONE", "US/Eastern"))
    cycle_type = "previous" if args.previous_cycle else "current"
    cycle_start, cycle_end = get_cycle_bounds(cycle_type, tz)

    logger.info(
        f"Fetching data for {cycle_type} cycle: "
        f"{cycle_start.date()} to {cycle_end.date()}"
    )

    # Collect data
    summary = collect_cycle_data(cycle_start, cycle_end)

    # Output
    save_summary(summary, cycle_start, cycle_end)


if __name__ == "__main__":
    main()
