#!/usr/bin/env python3
"""Create weekly summary file in Obsidian vault via Dropbox.

Creates a Week-Ending file for the upcoming Sunday with dataview queries
linking journal entries, outgoing/incoming links, and categorized content.
Skips creation if the file already exists.

Usage:
    python -m scripts.obsidian.workflows.file-creation.create_weeks
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


def _format_date_short(dt: datetime) -> str:
    """Format date like 'Mar 6, 2026' for Obsidian wikilinks."""
    try:
        return dt.strftime('%b %-d, %Y')
    except ValueError:
        return dt.strftime('%b %#d, %Y')


def _build_week_content(preceding_monday: datetime, upcoming_sunday: datetime) -> str:
    """Build the weekly file content with dataview queries."""
    formatted_sunday = upcoming_sunday.strftime("%Y-%m-%d")
    formatted_monday = preceding_monday.strftime("%Y-%m-%d")

    # Build list of day references for the week (Mon-Sun)
    days = [preceding_monday + timedelta(days=i) for i in range(7)]
    day_refs = [f"[[{_format_date_short(d)}]]" for d in days]

    def outgoing_from_clause():
        return "\nOR ".join(f"outgoing({ref})" for ref in day_refs)

    def incoming_from_clause():
        return "\nOR ".join(ref for ref in day_refs)

    sections = [
        ("All Outgoing Links for the Week", outgoing_from_clause(), None),
        ("All Incoming Links for the Week", incoming_from_clause(), None),
        ("Experiences / Events / Meetings / Sessions", None, "07_Experiences+Events+Meetings+Sessions"),
        ("CRM", None, "14_CRM"),
        ("Knowledge Hub", None, "05_Knowledge-Hub"),
        ("Writing", None, "03_Writing"),
        ("Notes & Ideas", None, "06_Notes+Ideas"),
    ]

    content = f"# Weekly Artifacts: {formatted_monday} to {formatted_sunday}\n\n"

    # Journal entries section
    content += f"""### Journal Entries
```dataview
LIST
FROM "01_Daily/_Journal"
WHERE 
date >= date("{formatted_monday}")
and date <= date("{formatted_sunday}")
SORT file.mtime DESC
```\n\n"""

    for title, custom_from, folder_filter in sections:
        if custom_from is not None:
            # Simple section (all outgoing / all incoming)
            content += f"""### {title}
```dataview
LIST 
FROM {custom_from}
SORT file.mtime DESC
```\n\n"""
        else:
            # Section with both outgoing and incoming, filtered by folder
            content += f"""### {title}
**Outgoing Links:**
```dataview
LIST 
FROM {outgoing_from_clause()}
WHERE contains(file.folder, "{folder_filter}")
SORT file.mtime DESC
```

**Incoming Links:**
```dataview
LIST 
FROM {incoming_from_clause()}
WHERE contains(file.folder, "{folder_filter}")
SORT file.mtime DESC
```\n\n"""

    # Final all incoming links section
    content += f"""### All Incoming Links for the Week
```dataview
LIST
FROM {incoming_from_clause()}
SORT file.mtime DESC
```\n"""

    return content


def create_weeks() -> bool:
    """Create a weekly summary file in the Obsidian vault.

    Creates a Week-Ending file for the next upcoming Sunday.

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
        weekly_notes_folder_path = f"{weekly_folder_path}/_Weeks"

        # Verify _Weeks folder exists
        try:
            dbx.files_get_metadata(weekly_notes_folder_path)
        except dropbox.exceptions.ApiError as e:
            if isinstance(e.error, dropbox.files.GetMetadataError):
                raise FileNotFoundError("'_Weeks' subfolder not found")
            raise

        # Calculate the nearest upcoming Sunday
        system_tz = pytz.timezone(timezone_str)
        today = datetime.now(system_tz)
        days_until_sunday = (6 - today.weekday()) % 7
        if days_until_sunday == 0:
            days_until_sunday = 7
        upcoming_sunday = today + timedelta(days=days_until_sunday)
        preceding_monday = upcoming_sunday - timedelta(days=6)

        formatted_sunday = upcoming_sunday.strftime("%Y-%m-%d")
        file_name = f"Week-Ending-{formatted_sunday}.md"
        dropbox_file_path = f"{weekly_notes_folder_path}/{file_name}"

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
        file_content = _build_week_content(preceding_monday, upcoming_sunday)
        dbx.files_upload(file_content.encode('utf-8'), dropbox_file_path)
        logger.info(f"Successfully created file '{file_name}' with content in Dropbox.")
        return True

    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Create weekly summary file in Obsidian vault")
    parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = create_weeks()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
