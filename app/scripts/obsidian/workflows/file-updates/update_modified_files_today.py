#!/usr/bin/env python3
"""Update Journal frontmatter for recently modified files in Obsidian vault.

Scans configured Dropbox folders for files modified since the last run
(or within the last ~24 hours), then adds or updates the 'Journal:' YAML
frontmatter property with the file's modification date as an Obsidian wikilink.

Paths to scan are loaded from a config file (paths_to_check.txt) located
alongside this script, or can be overridden via OBSIDIAN_PATHS_FILE env var.

Usage:
    python -m scripts.obsidian.workflows.file-updates.update_modified_files_today
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

# Redis key for tracking last run
REDIS_LAST_RUN_KEY = "last_run_folder_journal_relations_at"

# Default paths to check (used if no paths file is found)
DEFAULT_PATHS = [
    "/obsidian/personal/03_writing/_drafts",
    "/obsidian/personal/03_writing/_published",
    "/obsidian/personal/03_writing/_thoughts+sketches",
    "/obsidian/personal/03_writing/_tweets",
    "/obsidian/personal/03_writing/music",
    "/obsidian/personal/03_writing/_long-form",
    "/obsidian/personal/05_knowledge-hub",
    "/obsidian/personal/06_notes+ideas",
    "/obsidian/personal/07_experiences+events+meetings+sessions",
    "/obsidian/personal/13_places",
    "/obsidian/personal/14_crm/_deals",
    "/obsidian/personal/14_crm/_people",
    "/obsidian/personal",
]


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


# ===== Redis Timestamp Helpers =====

def _get_last_run_time() -> datetime | None:
    """Retrieve the last run timestamp from Redis in UTC."""
    last_run_str = redis_client.get(REDIS_LAST_RUN_KEY)
    if last_run_str is None:
        return None
    try:
        return datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _set_last_run_time(dt_utc: datetime):
    """Store the given UTC datetime in Redis as an ISO 8601 string."""
    redis_client.set(REDIS_LAST_RUN_KEY, dt_utc.isoformat())


# ===== Path Loading =====

def _load_paths() -> list[str]:
    """Load folder paths to scan.

    Checks for a paths file in this order:
    1. OBSIDIAN_PATHS_FILE env var
    2. paths_to_check.txt alongside this script
    3. Falls back to DEFAULT_PATHS
    """
    paths_file = os.getenv('OBSIDIAN_PATHS_FILE')
    if not paths_file:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        paths_file = os.path.join(script_dir, "paths_to_check.txt")

    if os.path.exists(paths_file):
        try:
            with open(paths_file, 'r') as f:
                paths = [line.strip() for line in f.readlines() if line.strip()]
            if paths:
                logger.info(f"Loaded {len(paths)} paths from {paths_file}")
                return paths
        except Exception as e:
            logger.warning(f"Error reading paths file: {e}")

    logger.info(f"Using {len(DEFAULT_PATHS)} default paths")
    return DEFAULT_PATHS


# ===== File Filtering =====

def _get_modified_files_since_cutoff(dbx: dropbox.Dropbox, paths: list[str], cutoff_dt: datetime) -> list[str]:
    """Get list of files modified after the cutoff datetime."""
    modified_files = []

    for path in paths:
        try:
            logger.info(f"Checking path: {path}")
            response = dbx.files_list_folder(path)

            while True:
                for entry in response.entries:
                    if isinstance(entry, dropbox.files.FileMetadata):
                        client_modified_utc = entry.client_modified
                        if client_modified_utc.tzinfo is None:
                            client_modified_utc = client_modified_utc.replace(tzinfo=pytz.utc)
                        if client_modified_utc > cutoff_dt:
                            modified_files.append(entry.path_lower)

                if not response.has_more:
                    break
                response = dbx.files_list_folder_continue(response.cursor)

        except dropbox.exceptions.ApiError as e:
            logger.error(f"Error accessing path {path}: {e}")

    return modified_files


# ===== Journal Updater =====

def _update_journal_property(dbx: dropbox.Dropbox, file_path: str):
    """Download file, add/update Journal frontmatter with modification date, re-upload."""
    try:
        metadata, response = dbx.files_download(file_path)
        content = response.content.decode('utf-8')
        original_path_display = metadata.path_display

        # Force client_modified to UTC if naive
        client_modified_utc = metadata.client_modified
        if client_modified_utc.tzinfo is None:
            client_modified_utc = client_modified_utc.replace(tzinfo=pytz.utc)

        # Convert to local timezone
        tz_str = os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles")
        try:
            local_tz = pytz.timezone(tz_str)
        except pytz.UnknownTimeZoneError:
            logger.warning(f"Unknown time zone '{tz_str}', falling back to 'America/Los_Angeles'")
            local_tz = pytz.timezone("America/Los_Angeles")

        client_modified_local = client_modified_utc.astimezone(local_tz)

        try:
            formatted_date = client_modified_local.strftime("%b %-d, %Y")
        except ValueError:
            formatted_date = client_modified_local.strftime("%b %#d, %Y")

        # Parse and update frontmatter
        properties_match = re.search(r'---(.*?)---', content, re.DOTALL)
        if properties_match:
            properties_section = properties_match.group(1)

            # Check if date already exists
            if f"[[{formatted_date}]]" in properties_section:
                logger.info(f"Date [[{formatted_date}]] already exists for: {file_path}")
                return

            # Look for existing Journal property
            journal_match = re.search(r'Journal:\s*(.*?)(?=\n\S|$)', properties_section, re.DOTALL)
            if journal_match:
                journal_entries_raw = journal_match.group(1).splitlines()

                journal_values = []
                for line in journal_entries_raw:
                    line = line.strip()
                    if line:
                        if line.startswith('- '):
                            line = line[2:].strip()
                        if line.startswith('"') and line.endswith('"'):
                            line = line[1:-1]
                        if line.startswith('[[') and line.endswith(']]'):
                            journal_values.append(line)

                new_entry = f"[[{formatted_date}]]"
                if new_entry not in journal_values:
                    journal_values.append(new_entry)

                formatted_entries = [f'  - "{value}"' for value in journal_values]
                updated_journal = "Journal:\n" + "\n".join(formatted_entries)
                updated_properties = re.sub(
                    r'Journal:\s*(.*?)(?=\n\S|$)',
                    updated_journal,
                    properties_section,
                    flags=re.DOTALL
                )
            else:
                updated_properties = properties_section + f'\nJournal:\n    - "[[{formatted_date}]]"'

            body_after_frontmatter = content.split('---', 2)[2].strip()
            updated_content = f"---\n{updated_properties.strip()}\n---\n{body_after_frontmatter}"
        else:
            updated_content = f'---\nJournal:\n    - "[[{formatted_date}]]"\n---\n{content}'

        # Overwrite in Dropbox
        dbx.files_upload(
            updated_content.encode('utf-8'),
            original_path_display,
            mode=dropbox.files.WriteMode.overwrite
        )
        logger.info(f"Updated Journal property for file: {metadata.name}")

    except Exception as e:
        logger.error(f"Error updating file {file_path}: {e}")


# ===== Main =====

def update_modified_files_today() -> bool:
    """Scan for recently modified files and update their Journal frontmatter.

    Returns:
        True on success, False on error.
    """
    try:
        dbx = _get_dropbox_client()
        paths_to_check = _load_paths()

        if not paths_to_check:
            logger.error("No paths to check.")
            return False

        # Determine cutoff
        now_utc = datetime.now(pytz.utc)
        cutoff_23h55 = now_utc - timedelta(hours=23, minutes=55)

        last_run_dt = _get_last_run_time()
        if last_run_dt is None:
            logger.info("No previous run timestamp found in Redis; defaulting to 24h ago.")
            last_run_dt = now_utc - timedelta(hours=24)

        final_cutoff = max(cutoff_23h55, last_run_dt)
        logger.info(f"Using cutoff UTC datetime: {final_cutoff.isoformat()}")

        # Fetch and process modified files
        modified_files = _get_modified_files_since_cutoff(dbx, paths_to_check, final_cutoff)

        if modified_files:
            logger.info(f"Found {len(modified_files)} modified file(s) in cutoff window.")
            for file_path in modified_files:
                logger.info(f"Processing file: {file_path}")
                _update_journal_property(dbx, file_path)
        else:
            logger.info("No files were modified in the cutoff window.")

        # Update last run timestamp
        _set_last_run_time(now_utc)
        logger.info("Script completed successfully. Updated last run time in Redis.")
        return True

    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Update Journal frontmatter for recently modified Obsidian files"
    )
    parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = update_modified_files_today()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
