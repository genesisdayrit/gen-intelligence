"""Dropbox helper for updating Telegram log entries in Obsidian journal."""

import os
import re

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

TELEGRAM_LOGS_HEADER = "### Telegram Logs:"


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


def _get_today_journal_path(journal_folder_path: str) -> str:
    """Get file path for today's journal."""
    system_tz = pytz.timezone(timezone_str)
    from datetime import datetime
    now = datetime.now(system_tz)
    formatted_date = f"{now.strftime('%b')} {now.day}, {now.strftime('%Y')}"
    return f"{journal_folder_path}/{formatted_date}.md"


def _get_journal_content(dbx: dropbox.Dropbox, file_path: str) -> str:
    """Fetch journal content from Dropbox."""
    try:
        _, response = dbx.files_download(file_path)
        return response.content.decode('utf-8')
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.DownloadError):
            raise FileNotFoundError(f"Journal not found: {file_path}")
        raise


def update_telegram_log(message_id: int, new_text: str) -> bool:
    """Update a Telegram log entry in today's journal by message_id.

    Looks up the original timestamp from Redis, finds the matching entry,
    and updates it with the new text.

    Returns True if entry was updated, False if not found.
    """
    # Look up the original timestamp from Redis
    timestamp = redis_client.get(f'telegram:msg:{message_id}')
    if not timestamp:
        # Message not tracked (sent before tracking was enabled, or TTL expired)
        return False

    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

    dbx = _get_dropbox_client()
    daily_folder = _find_daily_folder(dbx, vault_path)
    journal_folder = f"{daily_folder}/_Journal"
    file_path = _get_today_journal_path(journal_folder)

    try:
        content = _get_journal_content(dbx, file_path)
    except FileNotFoundError:
        # No journal file for today
        return False

    # Check if Telegram section exists
    if TELEGRAM_LOGS_HEADER not in content:
        return False

    # Find and update the line with matching timestamp
    lines = content.split('\n')
    updated_lines = []
    entry_updated = False
    in_telegram_section = False

    # Pattern to match the timestamp at the start of a log entry
    timestamp_pattern = re.compile(rf'^\[{re.escape(timestamp)}\]')

    for line in lines:
        if line.strip() == TELEGRAM_LOGS_HEADER:
            in_telegram_section = True
            updated_lines.append(line)
            continue

        if in_telegram_section:
            # Check if this line has the matching timestamp
            if timestamp_pattern.match(line) and not entry_updated:
                # Replace with new content, preserving timestamp
                updated_lines.append(f"[{timestamp}] {new_text}")
                entry_updated = True
                continue
            # Check if we've exited the section (hit another header)
            if line.startswith('#') or line.strip() == '---':
                in_telegram_section = False

        updated_lines.append(line)

    if not entry_updated:
        return False

    updated_content = '\n'.join(updated_lines)

    dbx.files_upload(
        updated_content.encode('utf-8'),
        file_path,
        mode=dropbox.files.WriteMode.overwrite
    )

    return True
