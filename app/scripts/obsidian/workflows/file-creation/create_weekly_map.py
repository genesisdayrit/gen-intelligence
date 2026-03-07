#!/usr/bin/env python3
"""Create weekly map file in Obsidian vault via Dropbox.

Creates a Weekly Map file for the Sunday after next using a template from
the vault's _Templates/weekly-templates/ folder. Skips if file already exists.

Usage:
    python -m scripts.obsidian.workflows.file-creation.create_weekly_map
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


def _find_weekly_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find folder ending with '_Weekly' in the vault."""
    result = dbx.files_list_folder(vault_path)
    for entry in result.entries:
        if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Weekly"):
            return entry.path_lower
    raise FileNotFoundError("Could not find a folder ending with '_Weekly' in Dropbox")


def _find_templates_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find folder ending with '_Templates' in the vault."""
    result = dbx.files_list_folder(vault_path)
    for entry in result.entries:
        if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Templates"):
            return entry.path_lower
    raise FileNotFoundError("Could not find a folder ending with '_Templates' in the Obsidian vault")


def _get_template_content(dbx: dropbox.Dropbox, templates_folder: str) -> str:
    """Download the weekly map template from Dropbox."""
    template_path = f"{templates_folder}/weekly-templates/weekly_map_template_w_placeholder.md"
    try:
        _, response = dbx.files_download(template_path)
        return response.content.decode('utf-8')
    except dropbox.exceptions.HttpError as e:
        logger.error(f"Error retrieving template from {template_path}: {e}")
        return ""


def create_weekly_map() -> bool:
    """Create a weekly map file in the Obsidian vault.

    Creates a Weekly Map file for the Sunday after next Sunday.

    Returns:
        True if the file was created or already exists, False on error.
    """
    dropbox_vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not dropbox_vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return False

    try:
        dbx = _get_dropbox_client()
        weekly_folder_path = _find_weekly_folder(dbx, dropbox_vault_path)
        weekly_maps_folder_path = f"{weekly_folder_path}/_Weekly-Maps"

        # Find templates folder and get template content
        templates_folder_path = _find_templates_folder(dbx, dropbox_vault_path)
        template_content = _get_template_content(dbx, templates_folder_path)

        # Calculate the title date (Sunday after the upcoming Sunday)
        system_tz = pytz.timezone(timezone_str)
        today = datetime.now(system_tz)
        days_until_sunday = (6 - today.weekday()) % 7
        next_sunday = today + timedelta(days=days_until_sunday)
        sunday_after_next = next_sunday + timedelta(days=7)
        title_date = sunday_after_next.strftime("%Y-%m-%d")

        file_name = f"Weekly Map {title_date}.md"
        dropbox_file_path = f"{weekly_maps_folder_path}/{file_name}"

        # Check if file already exists
        try:
            dbx.files_get_metadata(dropbox_file_path)
            logger.info(f"Weekly map file for '{title_date}' already exists. No new file created.")
            return True
        except dropbox.exceptions.ApiError as e:
            if not isinstance(e.error, dropbox.files.GetMetadataError):
                raise

        # File doesn't exist — create it from template
        logger.info(f"File '{file_name}' does not exist in Dropbox. Creating it now.")
        dbx.files_upload(template_content.encode('utf-8'), dropbox_file_path)
        logger.info(f"Successfully created weekly map file '{file_name}' in Dropbox.")
        return True

    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Create weekly map file in Obsidian vault")
    parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = create_weekly_map()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
