#!/usr/bin/env python3
"""Create new weekly cycle file in Obsidian vault via Dropbox.

Creates a numbered weekly cycle file with a Wed-Tue date range in the
vault's _Cycles/_Weekly-Cycles folder. Auto-increments the cycle number
and skips creation if a file with the same date range exists.

Scheduled Tuesday 01:30 ``SYSTEM_TZ`` creates next week's cycle (the
Wednesday after ``now``). ``ensure_weekly_cycle_file`` can also create
the Wed-Tue note containing a target date when
``update_daily_journal_properties`` self-heals a missing ``_Cycles`` link.

Usage:
    python -m scripts.obsidian.workflows.file_creation.create_new_cycle_page
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta

import dropbox
import pytz
from dotenv import load_dotenv

from services.dropbox.client import get_dropbox_client as _get_dropbox_client
from services.obsidian.utils.dropbox_rev_safe import create_or_defer

load_dotenv()

logger = logging.getLogger(__name__)

timezone_str = os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles")

# Exact headings Weekly Cycle writers look up (`line.strip() == "### Wednesday -"`).
WEEKLY_CYCLE_DAY_NAMES = (
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
    "Monday",
    "Tuesday",
)
WEEKLY_CYCLE_DAY_HEADINGS = tuple(
    f"### {name} -" for name in WEEKLY_CYCLE_DAY_NAMES
)


def _folder_path_display(entry: dropbox.files.FolderMetadata) -> str:
    """Prefer Dropbox path_display so creates keep vault casing."""
    return getattr(entry, "path_display", None) or entry.path_lower


def _list_folder_entries(dbx: dropbox.Dropbox, folder_path: str) -> list:
    """List all entries in a Dropbox folder, handling pagination."""
    entries = []
    result = dbx.files_list_folder(folder_path)
    entries.extend(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    return entries


def _find_cycles_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find folder ending with '_Cycles' in the vault (path_display)."""
    for entry in _list_folder_entries(dbx, vault_path):
        if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Cycles"):
            return _folder_path_display(entry)
    raise FileNotFoundError("Could not find a folder ending with '_Cycles' in Dropbox")


def _resolve_weekly_cycles_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Return the ``_Weekly-Cycles`` folder path_display under ``*_Cycles``."""
    cycles_folder_path = _find_cycles_folder(dbx, vault_path)
    for entry in _list_folder_entries(dbx, cycles_folder_path):
        if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith(
            "_Weekly-Cycles"
        ):
            return _folder_path_display(entry)
    raise FileNotFoundError("'_Weekly-Cycles' subfolder not found")


def _as_datetime(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, datetime.min.time())


def _stem(filename: str) -> str:
    if filename.lower().endswith(".md"):
        return filename[:-3]
    return filename


def _now() -> datetime:
    return datetime.now(pytz.timezone(timezone_str))


def next_cycle_wednesday(now: datetime) -> datetime:
    """Wednesday that starts the next weekly cycle.

    A Wednesday run still targets the following week, matching the
    Tuesday 01:30 scheduled job (next Wednesday after ``now``).
    """
    days_until_next_wednesday = (2 - now.weekday()) % 7
    if days_until_next_wednesday == 0:
        days_until_next_wednesday = 7
    return now + timedelta(days=days_until_next_wednesday)


def cycle_bounds_for_date(target: datetime | date) -> tuple[datetime, datetime]:
    """Wed-Tue bounds of the weekly cycle that contains ``target``."""
    day = _as_datetime(target)
    days_since_wednesday = (day.weekday() - 2) % 7
    cycle_start = day - timedelta(days=days_since_wednesday)
    cycle_end = cycle_start + timedelta(days=6)
    return cycle_start, cycle_end


def format_cycle_date_range(cycle_start: datetime, cycle_end: datetime) -> str:
    """Format ``(Oct. 28 - Nov. 03, 2026)`` for the cycle filename."""
    formatted_wednesday = cycle_start.strftime("%b. %d")
    formatted_tuesday = cycle_end.strftime("%b. %d, %Y")
    return f"({formatted_wednesday} - {formatted_tuesday})"


def cycle_filename(cycle_number: int, date_range: str) -> str:
    return f"Cycle {cycle_number} {date_range}.md"


def _fetch_last_cycle_number(entries: list) -> int:
    """Get the highest cycle number from existing files."""
    cycle_numbers = []
    for entry in entries:
        if not isinstance(entry, dropbox.files.FileMetadata):
            continue
        if not entry.name.startswith("Cycle "):
            continue
        try:
            cycle_numbers.append(int(entry.name.split(" ")[1]))
        except (IndexError, ValueError):
            continue
    return max(cycle_numbers) if cycle_numbers else 0


def _find_existing_cycle_filename(entries: list, date_range: str) -> str | None:
    """Return the on-disk filename if a cycle note for ``date_range`` exists."""
    needle = date_range.lower()
    for entry in entries:
        if isinstance(entry, dropbox.files.FileMetadata) and needle in entry.name.lower():
            return entry.name
    return None


def build_cycle_content(cycle_start: datetime, cycle_end: datetime) -> str:
    """Build create-only cycle note body plus Weekly Cycle day headings.

    Writers look up exact lines ``### Wednesday -`` … ``### Tuesday -``.
    ``---`` separators match the documented Weekly Cycle note shape so
    completed-task inserts can find the end of a day section.
    """
    lines = [
        f"Cycle Start Date: {cycle_start.strftime('%Y-%m-%d')}",
        f"Cycle End Date: {cycle_end.strftime('%Y-%m-%d')}",
        "",
    ]
    for index, heading in enumerate(WEEKLY_CYCLE_DAY_HEADINGS):
        lines.append(heading)
        lines.append("")
        if index < len(WEEKLY_CYCLE_DAY_HEADINGS) - 1:
            lines.append("---")
            lines.append("")
    return "\n".join(lines)


def ensure_weekly_cycle_file(
    dbx: dropbox.Dropbox,
    vault_path: str,
    target_date: datetime | date,
) -> str | None:
    """Create the Wed-Tue cycle note containing ``target_date`` if missing.

    Uses the on-disk ``path_display`` of ``*_Cycles/_Weekly-Cycles`` so the
    upload path keeps vault casing. Cycle number is previous max + 1.
    Create-only (``WriteMode.add``); never overwrites an existing date
    range. Returns the wikilink stem, preserving existing casing when the
    file is already there. ``None`` on error.
    """
    cycle_start, cycle_end = cycle_bounds_for_date(target_date)
    date_range = format_cycle_date_range(cycle_start, cycle_end)

    try:
        weekly_cycles_folder_path = _resolve_weekly_cycles_folder(dbx, vault_path)
    except FileNotFoundError as e:
        logger.error("Error: %s", e)
        return None

    entries = _list_folder_entries(dbx, weekly_cycles_folder_path)
    existing_name = _find_existing_cycle_filename(entries, date_range)
    if existing_name:
        logger.info(
            "A file with the date range '%s' already exists. Skipping creation.",
            date_range,
        )
        return _stem(existing_name)

    new_cycle_number = _fetch_last_cycle_number(entries) + 1
    file_name = cycle_filename(new_cycle_number, date_range)
    upload_path = f"{weekly_cycles_folder_path}/{file_name}"
    file_content = build_cycle_content(cycle_start, cycle_end)
    created = create_or_defer(
        dbx,
        upload_path,
        file_content,
        defer={
            "source": "create_new_cycle_page",
            "kind": "weekly_cycle",
            "payload_ref": file_name,
            "target": upload_path,
        },
    )
    if not created:
        existing_name = _find_existing_cycle_filename(
            _list_folder_entries(dbx, weekly_cycles_folder_path),
            date_range,
        )
        if existing_name:
            return _stem(existing_name)
        logger.warning("Could not create or find weekly cycle file '%s'.", file_name)
        return None

    logger.info("Successfully created file '%s' in Dropbox.", file_name)
    return _stem(file_name)


def create_new_cycle_page(target_date: datetime | date | None = None) -> bool:
    """Create a weekly cycle file in the Obsidian vault.

    When ``target_date`` is omitted, creates next week's cycle (Wednesday
    after ``now``), matching the Tuesday 01:30 scheduled job. When a date
    is given, creates the Wed-Tue cycle that contains that date.

    Returns:
        True if the file was created or already exists, False on error.
    """
    dropbox_vault_path = os.getenv("DROPBOX_OBSIDIAN_VAULT_PATH")
    if not dropbox_vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return False

    try:
        dbx = _get_dropbox_client()
        target = target_date if target_date is not None else next_cycle_wednesday(_now())
        stem = ensure_weekly_cycle_file(dbx, dropbox_vault_path, target)
        return stem is not None
    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Create new weekly cycle file in Obsidian vault")
    parser.add_argument(
        "--date",
        help="Create the cycle containing this YYYY-MM-DD instead of next Wednesday",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    target = None
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d")
    success = create_new_cycle_page(target_date=target)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
