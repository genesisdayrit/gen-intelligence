#!/usr/bin/env python3
"""Sync a single Linear Initiative to Obsidian vault via Dropbox.

Can be triggered by:
- CLI with --initiative-id or --project-id
- Webhook handlers in app/main.py

Usage:
    python -m scripts.linear.sync_single_initiative --initiative-id <id>
    python -m scripts.linear.sync_single_initiative --project-id <id>
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from scripts.linear.sync_utils import (
    enrich_initiative_data,
    fetch_single_initiative,
    find_initiatives_base_path,
    get_dropbox_client,
    get_initiative_id_for_project,
    sync_initiative,
)

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def sync_initiative_by_id(initiative_id: str) -> dict:
    """Fetch and sync a single initiative by ID.

    Returns summary statistics.
    """
    stats = {
        "initiatives_created": 0,
        "initiatives_updated": 0,
        "initiatives_moved": 0,
        "skipped_archived": 0,
        "projects_created": 0,
        "projects_updated": 0,
        "documents_created": 0,
        "documents_updated": 0,
        "errors": [],
    }

    # Fetch initiative from Linear
    try:
        initiative = fetch_single_initiative(initiative_id)
        if not initiative:
            logger.error(f"Initiative not found: {initiative_id}")
            stats["errors"].append(f"Initiative not found: {initiative_id}")
            return stats

        # Enrich with updates, documents, projects
        logger.info(f"Fetching data for initiative: {initiative['name']}")
        enrich_initiative_data(initiative)

    except Exception as e:
        logger.error(f"Failed to fetch initiative from Linear: {e}")
        stats["errors"].append(f"Linear API error: {e}")
        return stats

    # Get Dropbox client
    try:
        dbx = get_dropbox_client()
    except Exception as e:
        logger.error(f"Failed to get Dropbox client: {e}")
        stats["errors"].append(f"Dropbox error: {e}")
        return stats

    # Find _Initiatives base path
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH not set")
        stats["errors"].append("DROPBOX_OBSIDIAN_VAULT_PATH not set")
        return stats

    base_path = find_initiatives_base_path(dbx, vault_path)
    if not base_path:
        stats["errors"].append("Could not find _Initiatives folder")
        return stats

    logger.info(f"Syncing to: {base_path}")

    # Sync the initiative
    try:
        sync_initiative(dbx, base_path, initiative, stats)
    except Exception as e:
        initiative_name = initiative.get("name", "Unknown")
        logger.error(f"Error syncing initiative '{initiative_name}': {e}")
        stats["errors"].append(f"Initiative '{initiative_name}': {e}")

    return stats


def sync_initiative_for_project(project_id: str) -> dict:
    """Get parent initiative for a project, then sync the whole initiative.

    Returns summary statistics.
    """
    stats = {
        "initiatives_created": 0,
        "initiatives_updated": 0,
        "initiatives_moved": 0,
        "skipped_archived": 0,
        "projects_created": 0,
        "projects_updated": 0,
        "documents_created": 0,
        "documents_updated": 0,
        "errors": [],
    }

    # Get the parent initiative ID
    try:
        initiative_id = get_initiative_id_for_project(project_id)
        if not initiative_id:
            logger.warning(f"No parent initiative found for project: {project_id}")
            stats["errors"].append(f"No parent initiative found for project: {project_id}")
            return stats

        logger.info(f"Found parent initiative: {initiative_id}")

    except Exception as e:
        logger.error(f"Failed to get parent initiative: {e}")
        stats["errors"].append(f"Linear API error: {e}")
        return stats

    # Sync the parent initiative (which includes this project)
    return sync_initiative_by_id(initiative_id)


def print_summary(stats: dict) -> None:
    """Print sync summary."""
    print("\n" + "=" * 50)
    print("SYNC SUMMARY")
    print("=" * 50)
    print(f"Initiatives created: {stats['initiatives_created']}")
    print(f"Initiatives updated: {stats['initiatives_updated']}")
    print(f"Initiatives moved:   {stats['initiatives_moved']}")
    print(f"Skipped (archived):  {stats['skipped_archived']}")
    print(f"Projects created:    {stats['projects_created']}")
    print(f"Projects updated:    {stats['projects_updated']}")
    print(f"Documents created:   {stats['documents_created']}")
    print(f"Documents updated:   {stats['documents_updated']}")

    if stats["errors"]:
        print(f"\nErrors: {len(stats['errors'])}")
        for error in stats["errors"]:
            print(f"  - {error}")
    else:
        print("\nNo errors!")
    print("=" * 50)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Sync a single Linear Initiative to Obsidian"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--initiative-id",
        help="Linear Initiative ID to sync"
    )
    group.add_argument(
        "--project-id",
        help="Linear Project ID (will sync parent initiative)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.initiative_id:
        logger.info(f"Syncing initiative: {args.initiative_id}")
        stats = sync_initiative_by_id(args.initiative_id)
    else:
        logger.info(f"Syncing initiative for project: {args.project_id}")
        stats = sync_initiative_for_project(args.project_id)

    print_summary(stats)

    if stats["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
