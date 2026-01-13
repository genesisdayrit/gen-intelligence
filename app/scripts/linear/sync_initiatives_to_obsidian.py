#!/usr/bin/env python3
"""Sync all Linear Initiatives and Projects to Obsidian vault via Dropbox.

Syncs:
- Initiatives (organized by status folders)
- Initiative Updates
- Initiative Documents
- Projects under initiatives
- Project Updates
- Project Documents
- Project Issues

Usage:
    python -m scripts.linear.sync_initiatives_to_obsidian
    python -m scripts.linear.sync_initiatives_to_obsidian --include-archived
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from scripts.linear.sync_utils import (
    fetch_all_initiative_data,
    find_initiatives_base_path,
    get_dropbox_client,
    sync_initiative,
)

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def sync_all(include_archived: bool = False) -> dict:
    """Main sync function.

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

    # Fetch all initiative data from Linear
    try:
        initiatives = fetch_all_initiative_data(include_archived)
    except Exception as e:
        logger.error(f"Failed to fetch initiatives from Linear: {e}")
        stats["errors"].append(f"Linear API error: {e}")
        return stats

    if not initiatives:
        logger.info("No initiatives found")
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

    # Sync each initiative
    for initiative in initiatives:
        try:
            sync_initiative(dbx, base_path, initiative, stats)
        except Exception as e:
            initiative_name = initiative.get("name", "Unknown")
            logger.error(f"Error syncing initiative '{initiative_name}': {e}")
            stats["errors"].append(f"Initiative '{initiative_name}': {e}")

    return stats


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
        description="Sync Linear Initiatives and Projects to Obsidian"
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived initiatives from Linear"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info("Starting Linear to Obsidian sync...")

    stats = sync_all(include_archived=args.include_archived)

    print_summary(stats)

    if stats["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
