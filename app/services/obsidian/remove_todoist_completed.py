"""Dropbox helper for removing uncompleted Todoist tasks from Daily Action notes."""

import os
import re
from datetime import datetime

import dropbox
import pytz
import redis
import requests
from dotenv import load_dotenv

load_dotenv()

# Redis configuration
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', None)
redis_client = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)

# Timezone
timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")

TODOIST_COMPLETED_HEADER = "### Completed Tasks on Todoist:"


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

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Daily"):
                return entry.path_lower

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    raise FileNotFoundError("Could not find '_Daily' folder in Dropbox")


def _find_daily_action_folder(dbx: dropbox.Dropbox, daily_folder_path: str) -> str:
    """Find folder ending with '_Daily-Action' in the daily folder."""
    result = dbx.files_list_folder(daily_folder_path)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Daily-Action"):
                return entry.path_lower

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    raise FileNotFoundError("Could not find '_Daily-Action' folder in Dropbox")


def _get_today_daily_action_path(daily_action_folder_path: str) -> str:
    """Get file path for today's Daily Action."""
    system_tz = pytz.timezone(timezone_str)
    now = datetime.now(system_tz)
    formatted_date = now.strftime('%Y-%m-%d')
    return f"{daily_action_folder_path}/DA {formatted_date}.md"


def _get_daily_action_content(dbx: dropbox.Dropbox, file_path: str) -> str:
    """Fetch Daily Action content from Dropbox."""
    try:
        _, response = dbx.files_download(file_path)
        return response.content.decode('utf-8')
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.DownloadError):
            raise FileNotFoundError(f"Daily Action not found: {file_path}")
        raise


def remove_todoist_completed(task_content: str) -> bool:
    """Remove an uncompleted task from today's Todoist section in Daily Action.

    Searches for any line containing the task content (ignoring timestamp) and removes it.
    Returns True if a task was removed, False if task was not found.
    """
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

    dbx = _get_dropbox_client()
    daily_folder = _find_daily_folder(dbx, vault_path)
    daily_action_folder = _find_daily_action_folder(dbx, daily_folder)
    file_path = _get_today_daily_action_path(daily_action_folder)

    try:
        content = _get_daily_action_content(dbx, file_path)
    except FileNotFoundError:
        # No Daily Action file for today, nothing to remove
        return False

    # Check if Todoist section exists
    if TODOIST_COMPLETED_HEADER not in content:
        return False

    # Find and remove the line containing the task content
    lines = content.split('\n')
    updated_lines = []
    task_removed = False
    in_todoist_section = False

    for line in lines:
        if line.strip() == TODOIST_COMPLETED_HEADER:
            in_todoist_section = True
            updated_lines.append(line)
            continue

        if in_todoist_section:
            # Check if this line contains the task content (after timestamp)
            # Pattern: [HH:MM AM/PM] task content
            if re.match(r'^\[\d{2}:\d{2}', line) and task_content in line:
                # Skip this line (remove it)
                task_removed = True
                continue
            # Check if we've exited the section (hit another header or non-log content)
            if line.strip() and not re.match(r'^\[\d{2}:\d{2}', line) and line.strip() != '':
                in_todoist_section = False

        updated_lines.append(line)

    if not task_removed:
        return False

    # Check if the section is now empty (only header with no entries)
    # If so, remove the entire section
    final_lines = []
    skip_next_empty = False
    i = 0
    while i < len(updated_lines):
        line = updated_lines[i]
        if line.strip() == TODOIST_COMPLETED_HEADER:
            # Check if the section is empty (next lines are empty or start new section)
            section_has_entries = False
            for j in range(i + 1, len(updated_lines)):
                next_line = updated_lines[j]
                if re.match(r'^\[\d{2}:\d{2}', next_line):
                    section_has_entries = True
                    break
                if next_line.strip() and not next_line.strip() == '':
                    # Hit non-empty, non-log line = section ended
                    break

            if not section_has_entries:
                # Skip the header and any following empty lines
                i += 1
                while i < len(updated_lines) and updated_lines[i].strip() == '':
                    i += 1
                continue

        final_lines.append(line)
        i += 1

    updated_content = '\n'.join(final_lines)

    dbx.files_upload(
        updated_content.encode('utf-8'),
        file_path,
        mode=dropbox.files.WriteMode.overwrite
    )

    return True
