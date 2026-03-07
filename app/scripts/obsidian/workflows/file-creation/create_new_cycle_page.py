#!/usr/bin/env python3
"""Create new weekly cycle file in Obsidian vault via Dropbox.

Creates a numbered weekly cycle file with a Wed-Tue date range in the
vault's _Cycles/_Weekly-Cycles folder. Auto-increments the cycle number
and skips creation if a file with the same date range exists.

Usage:
    python -m scripts.obsidian.workflows.file-creation.create_new_cycle_page
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

import dropbox
import pytz
import redis
import requests
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


def _find_cycles_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find folder ending with '_Cycles' in the vault."""
    result = dbx.files_list_folder(vault_path)
    for entry in result.entries:
        if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Cycles"):
            return entry.path_lower
    raise FileNotFoundError("Could not find a folder ending with '_Cycles' in Dropbox")


def _list_all_entries(dbx: dropbox.Dropbox, folder_path: str) -> list:
    """List all entries in a Dropbox folder, handling pagination."""
    entries = []
    try:
        response = dbx.files_list_folder(folder_path)
        entries.extend(response.entries)
        while response.has_more:
            response = dbx.files_list_folder_continue(response.cursor)
            entries.extend(response.entries)
    except dropbox.exceptions.ApiError as e:
        logger.error(f"Error fetching folder list from {folder_path}: {e}")
    return entries


def _fetch_last_cycle_number(dbx: dropbox.Dropbox, cycles_folder: str) -> int:
    """Get the highest cycle number from existing files."""
    entries = _list_all_entries(dbx, cycles_folder)
    cycle_files = [
        entry.name for entry in entries
        if isinstance(entry, dropbox.files.FileMetadata)
        and entry.name.startswith("Cycle ")
    ]
    if not cycle_files:
        return 0
    cycle_numbers = []
    for f in cycle_files:
        try:
            cycle_numbers.append(int(f.split(" ")[1]))
        except (IndexError, ValueError):
            continue
    return max(cycle_numbers) if cycle_numbers else 0


def _date_range_exists(dbx: dropbox.Dropbox, folder_path: str, date_range: str) -> bool:
    """Check if a file with the given date range already exists."""
    entries = _list_all_entries(dbx, folder_path)
    for entry in entries:
        if isinstance(entry, dropbox.files.FileMetadata) and date_range in entry.name:
            return True
    return False


def create_new_cycle_page() -> bool:
    """Create a new weekly cycle file in the Obsidian vault.

    Creates a numbered cycle file with a Wed-Tue date range.

    Returns:
        True if the file was created or already exists, False on error.
    """
    dropbox_vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not dropbox_vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return False

    try:
        dbx = _get_dropbox_client()
        cycles_folder_path = _find_cycles_folder(dbx, dropbox_vault_path)
        weekly_cycles_folder_path = f"{cycles_folder_path}/_Weekly-Cycles"

        # Verify folder exists
        try:
            dbx.files_get_metadata(weekly_cycles_folder_path)
        except dropbox.exceptions.ApiError as e:
            if isinstance(e.error, dropbox.files.GetMetadataError):
                raise FileNotFoundError("'_Weekly-Cycles' subfolder not found")
            raise

        # Get next cycle number
        last_cycle_number = _fetch_last_cycle_number(dbx, weekly_cycles_folder_path)
        new_cycle_number = last_cycle_number + 1

        # Calculate the next Wednesday
        system_tz = pytz.timezone(timezone_str)
        today = datetime.now(system_tz)
        days_until_next_wednesday = (2 - today.weekday()) % 7
        if days_until_next_wednesday == 0:
            days_until_next_wednesday = 7
        next_wednesday = today + timedelta(days=days_until_next_wednesday)
        following_tuesday = next_wednesday + timedelta(days=6)

        formatted_wednesday = next_wednesday.strftime("%b. %d")
        formatted_tuesday = following_tuesday.strftime("%b. %d, %Y")

        date_range = f"({formatted_wednesday} - {formatted_tuesday})"

        # Check if a file with the same date range already exists
        if _date_range_exists(dbx, weekly_cycles_folder_path, date_range):
            logger.info(f"A file with the date range '{date_range}' already exists. Skipping creation.")
            return True

        # Create the file
        file_name = f"Cycle {new_cycle_number} {date_range}.md"
        dropbox_file_path = f"{weekly_cycles_folder_path}/{file_name}"

        file_content = (
            f"Cycle Start Date: {next_wednesday.strftime('%Y-%m-%d')}\n"
            f"Cycle End Date: {following_tuesday.strftime('%Y-%m-%d')}\n"
        )

        dbx.files_upload(file_content.encode('utf-8'), dropbox_file_path)
        logger.info(f"Successfully created file '{file_name}' in Dropbox.")
        return True

    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Create new weekly cycle file in Obsidian vault")
    parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = create_new_cycle_page()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
