"""Dropbox journal helper for writing to Obsidian daily notes."""

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

TELEGRAM_LOGS_HEADER = "### Telegram Logs:"
LOG_ENTRY_PATTERN = re.compile(r'^\[\d{2}:\d{2}')


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


def append_telegram_log(message_text: str) -> None:
    """Add a message to today's Telegram Logs section in Obsidian journal.

    Creates the section if it doesn't exist.
    """
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

    dbx = _get_dropbox_client()
    daily_folder = _find_daily_folder(dbx, vault_path)
    journal_folder = f"{daily_folder}/_Journal"
    file_path = _get_today_journal_path(journal_folder)
    content = _get_journal_content(dbx, file_path)

    # Find section and insert bullet
    lines = content.split('\n')
    new_lines = []
    section_found = False
    insert_index = None

    for i, line in enumerate(lines):
        new_lines.append(line)

        if line.strip() == TELEGRAM_LOGS_HEADER:
            section_found = True
            insert_index = i + 1
            continue

        if section_found:
            # Log entry starts with [HH:MM pattern
            if LOG_ENTRY_PATTERN.match(line):
                insert_index = i + 1
            # Next heading or markdown separator = end of section
            elif line.startswith('#') or line.strip() == '---':
                break
            # Non-empty content = update insert position
            elif line.strip():
                insert_index = i + 1
            # Empty lines = don't advance (insert after last content, not before next section)

    if not section_found:
        updated_content = content.rstrip() + "\n\n\n" + TELEGRAM_LOGS_HEADER + "\n" + f"{message_text}\n"
    else:
        # Insert new entry directly after last content (no blank lines between entries)
        new_lines.insert(insert_index, message_text)
        updated_content = '\n'.join(new_lines)

    dbx.files_upload(
        updated_content.encode('utf-8'),
        file_path,
        mode=dropbox.files.WriteMode.overwrite
    )
