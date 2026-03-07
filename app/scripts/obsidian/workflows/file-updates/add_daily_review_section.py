#!/usr/bin/env python3
"""Add a Daily Review section to today's daily action file in Obsidian.

Downloads the current daily action file (DA YYYY-MM-DD.md), inserts a
Daily Review section after the YAML frontmatter, and re-uploads the file.
Skips if the section already exists.

Usage:
    python -m scripts.obsidian.workflows.file-updates.add_daily_review_section
"""

import argparse
import logging
import os
import sys
from datetime import datetime

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


# ===== Dropbox File/Folder Helpers =====

def _find_folder_in_path(dbx: dropbox.Dropbox, base_path: str, search_term: str) -> str | None:
    """Search for a folder whose name contains search_term (case-insensitive)."""
    entries = []
    try:
        response = dbx.files_list_folder(base_path)
        entries.extend(response.entries)
        while response.has_more:
            response = dbx.files_list_folder_continue(response.cursor)
            entries.extend(response.entries)
    except Exception as e:
        logger.error(f"Error fetching folder list from {base_path}: {e}")
        return None

    for entry in entries:
        if isinstance(entry, dropbox.files.FolderMetadata):
            if search_term.lower() in entry.name.lower():
                return entry.path_lower
    logger.warning(f"No folder containing '{search_term}' found in '{base_path}'.")
    return None


# ===== YAML Parsing =====

def _parse_yaml_frontmatter(content: str) -> tuple[str, str]:
    """Parse YAML frontmatter from markdown content.

    Returns a tuple of (yaml_section, main_content).
    """
    if not content.startswith('---\n'):
        return "", content

    lines = content.split('\n')
    yaml_end_index = -1

    for i, line in enumerate(lines[1:], 1):
        if line.strip() == '---':
            yaml_end_index = i
            break

    if yaml_end_index == -1:
        return "", content

    yaml_lines = lines[:yaml_end_index + 1]
    yaml_section = '\n'.join(yaml_lines) + '\n\n'

    main_content_lines = lines[yaml_end_index + 1:]
    main_content = '\n'.join(main_content_lines).lstrip('\n')

    return yaml_section, main_content


# ===== Main Logic =====

DAILY_REVIEW_CONTENT = (
    "Daily Review:\n\n"
    "Win 1:\n\n"
    "Win 2 (What part of today was easiest, most enjoyable, and most effective in the direction of my dream reality?):\n\n"
    "Win 3 (What proof from today demonstrates that my Master Vision is unfolding before my eyes? And how did I create this win for myself?):\n\n"
    "What did not go well today...\n"
    "Be as brief or as detailed as you like:\n\n"
    "What concrete steps will you take to improve and make your life easier?\n\n"
    "Lastly, what are a few things you are grateful for?\n"
    "Think of something new or different than usual!\n\n"
    "---\n\n"
)


def add_daily_review_section() -> bool:
    """Add a Daily Review section to today's daily action file.

    Returns:
        True on success, False on error.
    """
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return False

    try:
        dbx = _get_dropbox_client()

        # Find the daily action folder
        daily_folder = _find_folder_in_path(dbx, vault_path, "_Daily")
        if not daily_folder:
            raise FileNotFoundError("Could not find a folder ending with '_Daily' in Dropbox")

        action_folder = _find_folder_in_path(dbx, daily_folder, "_Daily-Action")
        if not action_folder:
            raise FileNotFoundError("Could not find a folder ending with '_Daily-Action' in Dropbox")

        # Build today's file path
        today_date_str = datetime.now(pytz.timezone(timezone_str)).strftime('%Y-%m-%d')
        file_name = f"DA {today_date_str}.md"
        dropbox_file_path = f"{action_folder}/{file_name}"

        # Download current content
        _, response = dbx.files_download(dropbox_file_path)
        current_content = response.content.decode('utf-8')

        # Check if daily review section already exists
        if "Daily Review:" in current_content:
            logger.info(f"Daily Review section already exists in '{file_name}'. No changes made.")
            return True

        # Parse and insert review section after YAML frontmatter
        yaml_section, main_content = _parse_yaml_frontmatter(current_content)
        updated_content = yaml_section + DAILY_REVIEW_CONTENT + main_content

        # Upload updated file
        dbx.files_upload(
            updated_content.encode('utf-8'),
            dropbox_file_path,
            mode=dropbox.files.WriteMode.overwrite
        )
        logger.info(f"Successfully added 'Daily Review' section to '{file_name}'.")
        return True

    except Exception as e:
        logger.error(f"Could not find today's file in Dropbox: {e}")
        return False
    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Add Daily Review section to today's daily action file")
    parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = add_daily_review_section()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
