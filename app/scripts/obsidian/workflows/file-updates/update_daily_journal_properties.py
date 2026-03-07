#!/usr/bin/env python3
"""Update YAML frontmatter properties in an Obsidian daily journal file.

Finds tomorrow's (or today's) journal file in the Dropbox-synced vault, then
populates its YAML frontmatter with dynamic properties: day of week, date,
weekly/cycle relationship links, Daily Action, and "On this Day" references.

Usage:
    python -m scripts.obsidian.workflows.file-updates.update_daily_journal_properties
    python -m scripts.obsidian.workflows.file-updates.update_daily_journal_properties --today
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta

import dropbox
import pytz
import redis
import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Redis configuration
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', None)
redis_client = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)

# Timezone
timezone_str = os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles")


# ===== Dropbox Client =====

def _refresh_access_token() -> str:
    """Refresh the Dropbox access token using the refresh token."""
    client_id = os.getenv('DROPBOX_ACCESS_KEY')
    client_secret = os.getenv('DROPBOX_ACCESS_SECRET')
    refresh_token = os.getenv('DROPBOX_REFRESH_TOKEN')

    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError("Missing Dropbox credentials in .env file")

    response = requests.post(
        'https://api.dropbox.com/oauth2/token',
        data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': client_id,
            'client_secret': client_secret
        }
    )

    if response.status_code == 200:
        data = response.json()
        access_token = data.get('access_token')
        expires_in = data.get('expires_in')
        redis_client.set('DROPBOX_ACCESS_TOKEN', access_token, ex=expires_in)
        return access_token
    else:
        raise EnvironmentError(f"Failed to refresh token: {response.status_code}")


def _get_dropbox_client() -> dropbox.Dropbox:
    """Get authenticated Dropbox client."""
    access_token = redis_client.get('DROPBOX_ACCESS_TOKEN')
    if not access_token:
        access_token = _refresh_access_token()
    return dropbox.Dropbox(access_token)


# ===== Date Helper Functions =====

def _get_target_day(use_today: bool = False) -> datetime:
    """Return target day's datetime object in the configured timezone."""
    days_offset = 0 if use_today else 1
    return datetime.now(pytz.timezone(timezone_str)) + timedelta(days=days_offset)


def _get_day_of_week(use_today: bool = False) -> str:
    return _get_target_day(use_today).strftime('%A')


def _get_target_iso_date(use_today: bool = False) -> str:
    return _get_target_day(use_today).strftime('%Y-%m-%d')


def _get_target_filename(use_today: bool = False) -> str:
    """Format target day's date to match the journal filename format (e.g. 'Mar 6, 2026.md')."""
    target_day = _get_target_day(use_today)
    try:
        return target_day.strftime('%b %-d, %Y.md')
    except Exception:
        return target_day.strftime('%b %#d, %Y.md')


def _get_week_ending_sunday(use_today: bool = False) -> str:
    target_day = _get_target_day(use_today)
    days_until_sunday = (6 - target_day.weekday()) % 7
    week_ending = target_day + timedelta(days=days_until_sunday)
    return week_ending.strftime('%Y-%m-%d')


def _get_week_ending_filenames(use_today: bool = False) -> dict[str, str]:
    week_ending = _get_week_ending_sunday(use_today)
    return {
        "week_ending": f"Week-Ending-{week_ending}",
        "weekly_map": f"Weekly Map {week_ending}"
    }


def _get_cycle_date_range(use_today: bool = False) -> str:
    target_day = _get_target_day(use_today)
    days_since_wednesday = (target_day.weekday() - 2) % 7
    cycle_start = target_day - timedelta(days=days_since_wednesday)
    cycle_end = cycle_start + timedelta(days=6)
    return f"{cycle_start.strftime('%b. %d')} - {cycle_end.strftime('%b. %d, %Y')}"


def _get_weekly_newsletter_filename(use_today: bool = False) -> str:
    week_end_date_str = _get_week_ending_sunday(use_today)
    week_end_date = datetime.strptime(week_end_date_str, '%Y-%m-%d')
    return week_end_date.strftime("Weekly Newsletter %b. %d, %Y")


def _get_one_year_ago_date(use_today: bool = False) -> datetime:
    """Calculate one year ago from target day with proper leap year handling."""
    target_day = _get_target_day(use_today)
    try:
        return target_day.replace(year=target_day.year - 1)
    except ValueError:
        if target_day.month == 2 and target_day.day == 29:
            logger.info(f"Leap year adjustment: Feb 29 -> Feb 28 for year {target_day.year - 1}")
            return target_day.replace(year=target_day.year - 1, day=28)
        raise


def _get_one_year_ago_filename(use_today: bool = False) -> str:
    """Format one year ago date for the 'On this Day' property."""
    one_year_ago = _get_one_year_ago_date(use_today)
    try:
        return one_year_ago.strftime('%b %-d, %Y')
    except Exception:
        return one_year_ago.strftime('%b %#d, %Y')


# ===== Dropbox File/Folder Helpers =====

def _list_all_entries(dbx: dropbox.Dropbox, base_path: str) -> list:
    """List all entries in a Dropbox folder, handling pagination."""
    entries = []
    try:
        response = dbx.files_list_folder(base_path)
        entries.extend(response.entries)
        while response.has_more:
            response = dbx.files_list_folder_continue(response.cursor)
            entries.extend(response.entries)
    except dropbox.exceptions.ApiError as e:
        logger.error(f"Error fetching folder list from {base_path}: {e}")
    return entries


def _find_folder_in_path(dbx: dropbox.Dropbox, base_path: str, search_term: str) -> str | None:
    """Search for a folder whose name contains search_term (case-insensitive)."""
    entries = _list_all_entries(dbx, base_path)
    for entry in entries:
        if isinstance(entry, dropbox.files.FolderMetadata):
            if search_term.lower() in entry.name.lower():
                return entry.path_lower
    logger.warning(f"No folder containing '{search_term}' found in '{base_path}'.")
    return None


def _lookup_file_in_folder(dbx: dropbox.Dropbox, folder_path: str, file_template: str) -> tuple[str | None, str | None]:
    """Search for a file whose name contains file_template (case-insensitive)."""
    entries = _list_all_entries(dbx, folder_path)
    for entry in entries:
        if isinstance(entry, dropbox.files.FileMetadata):
            if file_template.lower() in entry.name.lower():
                return entry.path_lower, entry.name
    logger.warning(f"No file containing '{file_template}' found in '{folder_path}'.")
    return None, None


def _parse_date_range_from_filename(filename: str, target_date) -> bool:
    """Check if target_date falls within a date range in the filename.

    Handles filenames like '6-Week Cycle (2025.01.15 - 2025.02.25).md'.
    """
    pattern = r'\((\d{4}\.\d{2}\.\d{2}) - (\d{4}\.\d{2}\.\d{2})\)'
    match = re.search(pattern, filename)
    if not match:
        return False

    start_str, end_str = match.groups()
    try:
        start_date = datetime.strptime(start_str, '%Y.%m.%d').date()
        end_date = datetime.strptime(end_str, '%Y.%m.%d').date()
        return start_date <= target_date <= end_date
    except ValueError as e:
        logger.warning(f"Could not parse dates from filename '{filename}': {e}")
        return False


# ===== Dynamic Mapping Logic =====

def _get_long_cycle_filename(dbx: dropbox.Dropbox, vault_path: str, use_today: bool = False) -> str | None:
    """Find the 6-week cycle file that contains the target day's date."""
    target_day = _get_target_day(use_today).date()

    cycles_folder = _find_folder_in_path(dbx, vault_path, "_Cycles")
    if not cycles_folder:
        logger.error("Could not find _Cycles folder in vault.")
        return None

    six_week_folder = _find_folder_in_path(dbx, cycles_folder, "_6-Week-Cycles")
    if not six_week_folder:
        logger.error("Could not find _6-Week-Cycles folder.")
        return None

    entries = _list_all_entries(dbx, six_week_folder)
    for entry in entries:
        if isinstance(entry, dropbox.files.FileMetadata):
            if _parse_date_range_from_filename(entry.name, target_day):
                filename = entry.name
                if filename.lower().endswith('.md'):
                    filename = filename[:-3]
                logger.info(f"Found matching long cycle file: {filename}")
                return filename

    logger.warning(f"No long cycle file found containing target day's date: {target_day}")
    return None


def _process_mapping(dbx: dropbox.Dropbox, mapping: dict, vault_path: str) -> dict | None:
    """Process a single mapping: find parent folder, target folder, and file."""
    file_string = mapping["target_file_string"]

    parent_folder = _find_folder_in_path(dbx, vault_path, mapping["parent_folder"])
    if not parent_folder:
        logger.error(f"Parent folder containing '{mapping['parent_folder']}' not found.")
        return None

    target_folder = _find_folder_in_path(dbx, parent_folder, mapping["target_folder"])
    if not target_folder:
        logger.error(f"Target folder containing '{mapping['target_folder']}' not found.")
        return None

    file_path, file_name = _lookup_file_in_folder(dbx, target_folder, file_string)
    if not file_path:
        logger.error(f"File for mapping key '{mapping['key']}' not found.")
        return None

    base_name = file_name
    if base_name.lower().endswith('.md'):
        base_name = base_name[:-3]

    return {
        "key": mapping["key"],
        "file_path": file_path,
        "file_name": file_name,
        "relationship": f"[[{base_name}]]"
    }


def _get_dynamic_mappings(dbx: dropbox.Dropbox, vault_path: str, use_today: bool = False) -> dict[str, str]:
    """Generate dynamic mappings using date transforms and Dropbox file lookups."""
    week_ending = _get_week_ending_sunday(use_today)
    filenames = _get_week_ending_filenames(use_today)
    cycle_date_range = _get_cycle_date_range(use_today)
    weekly_newsletter = _get_weekly_newsletter_filename(use_today)
    long_cycle_filename = _get_long_cycle_filename(dbx, vault_path, use_today)

    mappings = [
        {"key": "Weeks", "parent_folder": "_Weekly", "target_folder": "_Weeks", "target_file_string": week_ending},
        {"key": "Weekly Map", "parent_folder": "_Weekly", "target_folder": "_Weekly-Maps", "target_file_string": filenames["weekly_map"]},
        {"key": "_Cycles", "parent_folder": "_Cycles", "target_folder": "_Weekly-Cycles", "target_file_string": cycle_date_range},
        {"key": "_Weekly Health Reviews", "parent_folder": "_Weekly", "target_folder": "_Weekly-Health-Review", "target_file_string": cycle_date_range},
        {"key": "Newsletter", "parent_folder": "_Weekly", "target_folder": "_Newsletters", "target_file_string": weekly_newsletter},
    ]

    if long_cycle_filename:
        mappings.append({"key": "_Long-Cycle", "parent_folder": "_Cycles", "target_folder": "_6-Week-Cycles", "target_file_string": long_cycle_filename})

    dynamic_mappings = {}
    for mapping in mappings:
        result = _process_mapping(dbx, mapping, vault_path)
        if result:
            dynamic_mappings[result["key"]] = result["relationship"]
    return dynamic_mappings


# ===== YAML Metadata =====

def _extract_yaml_metadata(file_content: str) -> tuple[dict | None, str | None]:
    """Extract YAML front matter from file content."""
    lines = file_content.splitlines()
    if lines and lines[0].strip() == "---":
        yaml_lines = []
        content_start = None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                content_start = i + 1
                break
            yaml_lines.append(line)
        yaml_str = "\n".join(yaml_lines)
        try:
            metadata = yaml.safe_load(yaml_str) or {}
            remaining_content = "\n".join(lines[content_start:]) if content_start is not None else ""
            return metadata, remaining_content
        except yaml.YAMLError as e:
            logger.error(f"Error parsing YAML: {e}")
            return None, None
    return {}, file_content


def _update_yaml_metadata(metadata: dict, dynamic_mappings: dict, use_today: bool = False) -> dict:
    """Update YAML metadata with date info, dynamic mappings, Daily Action, and On this Day."""
    metadata["Day of Week"] = _get_day_of_week(use_today)
    metadata["Date"] = _get_target_iso_date(use_today)

    list_keys = {"Weeks", "_Weekly Health Reviews", "_Cycles", "_Long-Cycle", "Daily Action", "On this Day"}

    for key, relationship in dynamic_mappings.items():
        if key in list_keys:
            metadata[key] = [relationship]
        else:
            metadata[key] = relationship

    daily_action = f"[[DA {_get_target_iso_date(use_today)}]]"
    metadata["Daily Action"] = [daily_action]

    one_year_ago_filename = _get_one_year_ago_filename(use_today)
    metadata["On this Day"] = [f"[[{one_year_ago_filename}]]"]

    return metadata


# ===== Main Logic =====

def update_daily_journal_properties(use_today: bool = False) -> bool:
    """Update YAML properties in the target day's journal file.

    Args:
        use_today: If True, update today's journal. Otherwise, update tomorrow's.

    Returns:
        True on success, False on error.
    """
    vault_path = os.getenv("DROPBOX_OBSIDIAN_VAULT_PATH")
    if not vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return False

    day_desc = "today's" if use_today else "tomorrow's"

    try:
        dbx = _get_dropbox_client()

        # Step 1: Find the journal file
        logger.info(f"Checking if {day_desc} journal exists...")
        daily_folder = _find_folder_in_path(dbx, vault_path, "_Daily")
        if not daily_folder:
            raise FileNotFoundError("Could not find the _Daily folder in the vault.")
        journal_folder = _find_folder_in_path(dbx, daily_folder, "_Journal")
        if not journal_folder:
            raise FileNotFoundError("Could not find the _Journal folder within _Daily.")

        target_filename = _get_target_filename(use_today)
        entries = _list_all_entries(dbx, journal_folder)
        journal_file_path = None
        journal_file_name = None
        for entry in entries:
            if isinstance(entry, dropbox.files.FileMetadata) and entry.name.lower() == target_filename.lower():
                journal_file_path = entry.path_lower
                journal_file_name = entry.name
                break
        if not journal_file_path:
            raise FileNotFoundError(f"No journal file found for target date ({target_filename})")

        logger.info(f"{day_desc.capitalize()} journal found at: {journal_file_path}")

        # Step 2: Download and parse YAML frontmatter
        _, response = dbx.files_download(journal_file_path)
        file_content = response.content.decode('utf-8')

        metadata, remaining_content = _extract_yaml_metadata(file_content)
        if metadata is None:
            logger.error("No valid YAML metadata found in journal file.")
            return False

        # Step 3: Generate dynamic mappings
        logger.info("Journal found — proceeding with dynamic mappings lookup...")
        dynamic_mappings = _get_dynamic_mappings(dbx, vault_path, use_today)
        logger.info(f"Dynamic mappings: {dynamic_mappings}")

        # Step 4: Update YAML metadata
        updated_metadata = _update_yaml_metadata(metadata, dynamic_mappings, use_today)

        # Step 5: Upload updated file
        yaml_str = yaml.safe_dump(updated_metadata, default_flow_style=False, sort_keys=False)
        new_content = f"---\n{yaml_str}---\n{remaining_content}"
        upload_path = os.path.join(os.path.dirname(journal_file_path), journal_file_name)
        dbx.files_upload(
            new_content.encode('utf-8'),
            upload_path,
            mode=dropbox.files.WriteMode.overwrite
        )
        logger.info(f"Updated file uploaded successfully: {upload_path}")
        return True

    except FileNotFoundError as e:
        logger.warning(f"{day_desc.capitalize()} journal not found — skipping update: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Update daily properties in journal file")
    parser.add_argument("--today", action="store_true",
                        help="Update today's journal instead of tomorrow's")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = update_daily_journal_properties(use_today=args.today)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
