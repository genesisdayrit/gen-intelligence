#!/usr/bin/env python3
"""Create daily journal file in Obsidian vault via Dropbox.

Creates tomorrow's (or today's) journal file from a template in the vault's
_Templates/daily-templates/ folder. Skips creation if the file already exists.

Usage:
    python -m scripts.obsidian.workflows.file-creation.create_daily_journal
    python -m scripts.obsidian.workflows.file-creation.create_daily_journal --today
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


def _find_daily_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find folder ending with '_Daily' in the vault."""
    result = dbx.files_list_folder(vault_path)
    for entry in result.entries:
        if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Daily"):
            return entry.path_lower
    raise FileNotFoundError("Could not find a folder ending with '_Daily' in Dropbox")


def _find_templates_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find folder ending with '_Templates' in the vault."""
    result = dbx.files_list_folder(vault_path)
    for entry in result.entries:
        if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Templates"):
            return entry.path_lower
    raise FileNotFoundError("Could not find a folder ending with '_Templates' in the Obsidian vault")


def _get_template_content(dbx: dropbox.Dropbox, templates_folder: str) -> str:
    """Download the daily note template from Dropbox."""
    template_path = f"{templates_folder}/daily-templates/daily_note_properties.md"
    try:
        _, response = dbx.files_download(template_path)
        return response.content.decode('utf-8')
    except dropbox.exceptions.HttpError as e:
        logger.error(f"Error retrieving template from {template_path}: {e}")
        return ""


def create_daily_journal(use_today: bool = False) -> bool:
    """Create a daily journal file in the Obsidian vault.

    Args:
        use_today: If True, create journal for today. Otherwise, create for tomorrow.

    Returns:
        True if the file was created or already exists, False on error.
    """
    dropbox_vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not dropbox_vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return False

    try:
        dbx = _get_dropbox_client()
        daily_folder_path = _find_daily_folder(dbx, dropbox_vault_path)
        journal_folder_path = f"{daily_folder_path}/_Journal"

        # Determine target date
        system_tz = pytz.timezone(timezone_str)
        now_system = datetime.now(system_tz)
        days_offset = 0 if use_today else 1
        target_date = now_system + timedelta(days=days_offset)

        formatted_date = f"{target_date.strftime('%b')} {target_date.day}, {target_date.strftime('%Y')}"
        file_name = f"{formatted_date}.md"
        dropbox_file_path = f"{journal_folder_path}/{file_name}"

        # Check if file already exists
        try:
            dbx.files_get_metadata(dropbox_file_path)
            logger.info(f"Journal file for '{formatted_date}' already exists. No new file created.")
            return True
        except dropbox.exceptions.ApiError as e:
            if not isinstance(e.error, dropbox.files.GetMetadataError):
                raise

        # File doesn't exist — create it from template
        logger.info(f"File '{file_name}' does not exist in Dropbox. Creating it now.")
        templates_folder = _find_templates_folder(dbx, dropbox_vault_path)
        template_content = _get_template_content(dbx, templates_folder)
        filled_template = template_content.replace('{{date}}', formatted_date)

        dbx.files_upload(filled_template.encode('utf-8'), dropbox_file_path)
        logger.info(f"Successfully created file '{file_name}' in Dropbox using the template.")
        return True

    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Create daily journal file in Obsidian vault")
    parser.add_argument("--today", action="store_true",
                        help="Create journal file for today instead of tomorrow")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = create_daily_journal(use_today=args.today)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
