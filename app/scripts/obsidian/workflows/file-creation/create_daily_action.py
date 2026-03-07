#!/usr/bin/env python3
"""Create daily action file in Obsidian vault via Dropbox.

Creates tomorrow's (or today's) daily action file with YAML frontmatter
containing relationship links (journal, weekly cycle, long cycle, weekly map)
and structured content prompts. Skips creation if the file already exists.

Usage:
    python -m scripts.obsidian.workflows.file-creation.create_daily_action
    python -m scripts.obsidian.workflows.file-creation.create_daily_action --today
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


def _get_journal_filename(use_today: bool = False) -> str:
    """Format target day's date to match journal filename format (e.g. 'Mar 6, 2026')."""
    target_day = _get_target_day(use_today)
    try:
        return target_day.strftime('%b %-d, %Y')
    except ValueError:
        return target_day.strftime('%b %#d, %Y')


def _get_cycle_date_range(use_today: bool = False) -> str:
    """Get weekly cycle date range for the target day's date."""
    target_day = _get_target_day(use_today)
    days_since_wednesday = (target_day.weekday() - 2) % 7
    cycle_start = target_day - timedelta(days=days_since_wednesday)
    cycle_end = cycle_start + timedelta(days=6)
    return f"{cycle_start.strftime('%b. %d')} - {cycle_end.strftime('%b. %d, %Y')}"


def _get_week_ending_sunday(use_today: bool = False) -> str:
    """Get the date of the Sunday ending the week that contains the target day."""
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
    """Check if target_date falls within a date range in the filename."""
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


# ===== Relationship Discovery =====

def _find_weekly_cycle_link(dbx: dropbox.Dropbox, vault_path: str, use_today: bool = False) -> str:
    """Find weekly cycle file link for the target day's date."""
    try:
        cycles_folder = _find_folder_in_path(dbx, vault_path, "_Cycles")
        if not cycles_folder:
            return ""

        weekly_cycles_folder = _find_folder_in_path(dbx, cycles_folder, "_Weekly-Cycles")
        if not weekly_cycles_folder:
            return ""

        cycle_date_range = _get_cycle_date_range(use_today)
        file_path, file_name = _lookup_file_in_folder(dbx, weekly_cycles_folder, cycle_date_range)

        if file_path and file_name:
            base_name = file_name
            if base_name.lower().endswith('.md'):
                base_name = base_name[:-3]
            return f"[[{base_name}]]"

        logger.warning(f"No weekly cycle file found for date range: {cycle_date_range}")
        return ""

    except Exception as e:
        logger.error(f"Error finding weekly cycle link: {e}")
        return ""


def _find_long_cycle_link(dbx: dropbox.Dropbox, vault_path: str, use_today: bool = False) -> str:
    """Find long cycle file link for the target day's date."""
    try:
        target_day = _get_target_day(use_today).date()

        cycles_folder = _find_folder_in_path(dbx, vault_path, "_Cycles")
        if not cycles_folder:
            return ""

        six_week_folder = _find_folder_in_path(dbx, cycles_folder, "_6-Week-Cycles")
        if not six_week_folder:
            return ""

        entries = _list_all_entries(dbx, six_week_folder)
        for entry in entries:
            if isinstance(entry, dropbox.files.FileMetadata):
                if _parse_date_range_from_filename(entry.name, target_day):
                    filename = entry.name
                    if filename.lower().endswith('.md'):
                        filename = filename[:-3]
                    logger.info(f"Found matching long cycle file: {filename}")
                    return f"[[{filename}]]"

        logger.warning(f"No long cycle file found containing target day's date: {target_day}")
        return ""

    except Exception as e:
        logger.error(f"Error finding long cycle link: {e}")
        return ""


def _find_weekly_map_link(dbx: dropbox.Dropbox, vault_path: str, use_today: bool = False) -> str:
    """Find weekly map file link for the target day's date."""
    try:
        weekly_folder = _find_folder_in_path(dbx, vault_path, "_Weekly")
        if not weekly_folder:
            return ""

        weekly_maps_folder = _find_folder_in_path(dbx, weekly_folder, "_Weekly-Maps")
        if not weekly_maps_folder:
            return ""

        filenames = _get_week_ending_filenames(use_today)
        weekly_map_filename = filenames["weekly_map"]
        file_path, file_name = _lookup_file_in_folder(dbx, weekly_maps_folder, weekly_map_filename)

        if file_path and file_name:
            base_name = file_name
            if base_name.lower().endswith('.md'):
                base_name = base_name[:-3]
            return f"[[{base_name}]]"

        logger.warning(f"No weekly map file found for: {weekly_map_filename}")
        return ""

    except Exception as e:
        logger.error(f"Error finding weekly map link: {e}")
        return ""


# ===== File Creation =====

def _generate_yaml_properties(dbx: dropbox.Dropbox, vault_path: str, use_today: bool = False) -> dict:
    """Generate the YAML properties with relationship links."""
    journal_filename = _get_journal_filename(use_today)
    journal_link = f"[[{journal_filename}]]"

    weekly_cycle_link = _find_weekly_cycle_link(dbx, vault_path, use_today)
    weekly_cycle_list = [weekly_cycle_link] if weekly_cycle_link else []

    long_cycle_link = _find_long_cycle_link(dbx, vault_path, use_today)
    long_cycle_list = [long_cycle_link] if long_cycle_link else []

    weekly_map_link = _find_weekly_map_link(dbx, vault_path, use_today)
    weekly_map_list = [weekly_map_link] if weekly_map_link else []

    return {
        'journal': journal_link,
        'weekly_cycle': weekly_cycle_list,
        'long_cycle': long_cycle_list,
        'weekly_map': weekly_map_list
    }


def _find_daily_action_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find the _Daily-Action folder inside the vault's _Daily folder."""
    daily_folder = _find_folder_in_path(dbx, vault_path, "_Daily")
    if not daily_folder:
        raise FileNotFoundError("Could not find a folder ending with '_Daily' in Dropbox")

    action_folder = _find_folder_in_path(dbx, daily_folder, "_Daily-Action")
    if not action_folder:
        raise FileNotFoundError("Could not find a folder ending with '_Daily-Action' in Dropbox")

    return action_folder


def create_daily_action(use_today: bool = False) -> bool:
    """Create a daily action file in the Obsidian vault.

    Args:
        use_today: If True, create action file for today. Otherwise, create for tomorrow.

    Returns:
        True if the file was created or already exists, False on error.
    """
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return False

    try:
        dbx = _get_dropbox_client()
        daily_action_folder = _find_daily_action_folder(dbx, vault_path)

        # Determine target date
        target_day = _get_target_day(use_today)
        file_name = f"DA {target_day.strftime('%Y-%m-%d')}.md"
        dropbox_file_path = f"{daily_action_folder}/{file_name}"

        # Check if file already exists
        try:
            dbx.files_get_metadata(dropbox_file_path)
            logger.info(f"Daily action file for '{target_day.strftime('%Y-%m-%d')}' already exists. No new file created.")
            return True
        except dropbox.exceptions.ApiError as e:
            if not isinstance(e.error, dropbox.files.GetMetadataError):
                raise

        # File doesn't exist — create it
        logger.info(f"File '{file_name}' does not exist in Dropbox. Creating it now.")

        # Generate YAML frontmatter
        yaml_props = _generate_yaml_properties(dbx, vault_path, use_today)
        yaml_metadata = {
            '_Journal': yaml_props['journal'],
            '_Weekly-Cycle': yaml_props['weekly_cycle'],
            '_Long-Cycle': yaml_props['long_cycle'],
            '_Weekly-Map': yaml_props['weekly_map']
        }
        yaml_str = yaml.safe_dump(yaml_metadata, default_flow_style=False, sort_keys=False)
        yaml_section = f"---\n{yaml_str}---\n\n"

        # Content structure
        main_content = (
            "Vision Objective 1:\n"
            "Vision Objective 2:\n"
            "Vision Objective 3:\n\n"
            "One thing that you can do to improve today:\n\n"
            "Mindset Objective:\n"
            "Body Objective:\n"
            "Social Objective:\n\n"
            "Gratitude:\n\n"
            "---\n\n"
            "What is the highest leverage thing that you can do today to move the ball forward on what you need to?\n"
            "If you only had 2 hours to work today, what would you need to get done to move forward towards your goals or master vision?"
        )

        content = yaml_section + main_content
        dbx.files_upload(content.encode('utf-8'), dropbox_file_path)
        logger.info(f"Successfully created daily action file '{file_name}' in Dropbox.")
        return True

    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Create daily action file in Obsidian vault")
    parser.add_argument("--today", action="store_true",
                        help="Create action file for today instead of tomorrow")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = create_daily_action(use_today=args.today)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
