#!/usr/bin/env python3
"""Backfill completed Todoist tasks into Obsidian notes.

Fetches completed tasks from the Todoist API and writes them to both
Daily Action and Weekly Cycle notes in Obsidian (via Dropbox).
Deduplicates against tasks already present in the notes.

Usage:
    python -m scripts.todoist.backfill_completions              # Today only (default)
    python -m scripts.todoist.backfill_completions --days 3     # Last 3 days
    python -m scripts.todoist.backfill_completions --dry-run    # Preview only
    python -m scripts.todoist.backfill_completions --days 7 --dry-run
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from dotenv import load_dotenv

# Add app directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from services.todoist.fetch_completions import fetch_completed_tasks
from services.obsidian.append_completed_task import append_completed_task
from services.obsidian.utils.date_helpers import get_effective_date

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Backfill completed Todoist tasks into Obsidian notes"
    )
    parser.add_argument(
        "--days",
        type=int,
        metavar="N",
        help="Backfill completions from the last N days (default: today only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview tasks without writing to Obsidian",
    )
    return parser.parse_args()


def compute_date_range(days: int | None, tz) -> tuple[datetime, datetime]:
    """Compute the (since, until) range for the Todoist API query.

    For today: effective date start (respecting 3am rollover) to now.
    For --days N: N days ago at midnight to now.
    """
    now = datetime.now(tz)

    if days is None:
        # Today only: use effective date (handles midnight-3am rollover)
        effective = get_effective_date(now)
        since = tz.localize(datetime(effective.year, effective.month, effective.day, 0, 0, 0))
    else:
        start_date = now - timedelta(days=days)
        since = start_date.replace(hour=0, minute=0, second=0, microsecond=0)

    return since, now


def parse_completed_at(completed_at_str: str, tz) -> datetime:
    """Parse Todoist completed_at ISO 8601 UTC string to a tz-aware datetime."""
    dt = datetime.fromisoformat(completed_at_str.replace("Z", "+00:00"))
    return dt.astimezone(tz)


def main():
    args = parse_args()

    timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")
    system_tz = pytz.timezone(timezone_str)
    logger.info(f"Using timezone: {timezone_str}")

    since, until = compute_date_range(args.days, system_tz)
    logger.info(f"Fetching completions from {since.isoformat()} to {until.isoformat()}")

    tasks = fetch_completed_tasks(since, until)

    if not tasks:
        logger.info("No completed tasks found in the given range.")
        return

    # Sort by completed_at for chronological processing
    tasks.sort(key=lambda t: t.get("completed_at", ""))

    logger.info(f"Found {len(tasks)} completed tasks")

    if args.dry_run:
        print(f"\n{'=' * 60}")
        print(f"DRY RUN - {len(tasks)} tasks found")
        print(f"{'=' * 60}")
        for task in tasks:
            content = task.get("content", "Unknown task")
            completed_at_str = task.get("completed_at", "")
            if completed_at_str:
                target_dt = parse_completed_at(completed_at_str, system_tz)
                effective_date = get_effective_date(target_dt)
                print(f"  {effective_date.strftime('%Y-%m-%d')} [{target_dt.strftime('%H:%M %p')}] {content}")
            else:
                print(f"  [no timestamp] {content}")
        print(f"{'=' * 60}\n")
        return

    success_count = 0
    skip_count = 0
    error_count = 0

    for task in tasks:
        content = task.get("content", "Unknown task")
        completed_at_str = task.get("completed_at")

        if not completed_at_str:
            logger.warning(f"Task missing completed_at, skipping: {content}")
            skip_count += 1
            continue

        target_dt = parse_completed_at(completed_at_str, system_tz)

        try:
            result = append_completed_task(content, target_dt)

            da_ok = result["daily_action_success"]
            wc_ok = result["weekly_cycle_success"]

            if da_ok or wc_ok:
                success_count += 1
                logger.info(f"Wrote: [{target_dt.strftime('%H:%M %p')}] {content}")
            else:
                error_count += 1
                logger.error(f"Failed both writes for: {content}")
                if result["daily_action_error"]:
                    logger.error(f"  DA error: {result['daily_action_error']}")
                if result["weekly_cycle_error"]:
                    logger.error(f"  WC error: {result['weekly_cycle_error']}")
        except Exception as e:
            error_count += 1
            logger.error(f"Error processing task '{content}': {e}")

    logger.info(f"Done: {success_count} written, {skip_count} skipped, {error_count} errors")


if __name__ == "__main__":
    main()
