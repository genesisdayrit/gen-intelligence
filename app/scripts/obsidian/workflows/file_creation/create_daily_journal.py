#!/usr/bin/env python3
"""Create daily journal file in Obsidian vault via Dropbox.

Creates tomorrow's (or today's) journal file from a template in the vault's
_Templates/daily-templates/ folder. Skips creation if the file already exists.

Usage:
    python -m scripts.obsidian.workflows.file_creation.create_daily_journal
    python -m scripts.obsidian.workflows.file_creation.create_daily_journal --today
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

import dropbox
import pytz
from dotenv import load_dotenv
from services.dropbox.client import get_dropbox_client as _get_dropbox_client

load_dotenv()

logger = logging.getLogger(__name__)


# Timezone
timezone_str = os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles")



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
