#!/usr/bin/env python3
"""Create weekly newsletter file in Obsidian vault via Dropbox.

Creates a newsletter file for the Sunday after next in the vault's
_Weekly/_Newsletters folder. Skips creation if the file already exists.

Usage:
    python -m scripts.obsidian.workflows.file-creation.create_newsletter_page
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


def create_newsletter_page() -> bool:
    """Create a weekly newsletter file in the Obsidian vault.

    Creates a newsletter file for the Sunday after the next upcoming Sunday.

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
        newsletter_folder_path = f"{weekly_folder_path}/_Newsletters"

        # Verify _Newsletters folder exists
        try:
            dbx.files_get_metadata(newsletter_folder_path)
        except dropbox.exceptions.ApiError as e:
            if isinstance(e.error, dropbox.files.GetMetadataError):
                raise FileNotFoundError("'_Newsletters' subfolder not found")
            raise

        # Calculate the Sunday after next upcoming Sunday
        system_tz = pytz.timezone(timezone_str)
        today = datetime.now(system_tz)
        days_until_next_sunday = (6 - today.weekday()) % 7
        if days_until_next_sunday == 0:
            days_until_next_sunday = 7
        second_upcoming_sunday = today + timedelta(days=days_until_next_sunday + 7)
        formatted_date = second_upcoming_sunday.strftime("%b. %d, %Y")

        file_name = f"Weekly Newsletter {formatted_date}.md"
        dropbox_file_path = f"{newsletter_folder_path}/{file_name}"

        # Check if file already exists
        try:
            dbx.files_get_metadata(dropbox_file_path)
            logger.info(f"File '{file_name}' already exists in Dropbox. Skipping creation.")
            return True
        except dropbox.exceptions.ApiError as e:
            if not isinstance(e.error, dropbox.files.GetMetadataError):
                raise

        # File doesn't exist — create it
        logger.info(f"File '{file_name}' does not exist in Dropbox. Creating it now.")
        dbx.files_upload(b"", dropbox_file_path)
        logger.info(f"Successfully created empty file '{file_name}' in Dropbox.")
        return True

    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Create weekly newsletter file in Obsidian vault")
    parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = create_newsletter_page()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
