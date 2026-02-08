#!/usr/bin/env python3
"""Append Manus AI tasks to this week's Obsidian weekly note.

Fetches tasks created this week (Monday-Sunday) from the Manus API
and appends them to the Week-Ending note in Obsidian via Dropbox.
Idempotent: re-running deduplicates by task URL.

Usage:
    python -m scripts.manus.append_weekly_tasks_to_obsidian             # Write to Obsidian
    python -m scripts.manus.append_weekly_tasks_to_obsidian --dry-run   # Preview only
"""

import argparse
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import dropbox
import httpx
import pytz
import redis
import requests
from dotenv import load_dotenv

# Add app directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Redis configuration
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', None)
redis_client = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)

MANUS_API_BASE = "https://api.manus.ai/v1"
MANUS_TASKS_HEADER = "## Manus Tasks"
MANUS_URL_PATTERN = re.compile(r'\[.*?\]\((https://manus\.im/task/\S+)\)')


def parse_args():
    parser = argparse.ArgumentParser(
        description="Append Manus AI tasks to this week's Obsidian weekly note"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview tasks without writing to Obsidian",
    )
    return parser.parse_args()


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


def compute_week_bounds(tz) -> tuple[date, date]:
    """Compute Monday and Sunday of the current week.

    Returns timezone-aware datetimes for Monday 00:00 and Sunday 23:59.
    """
    today = datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())  # weekday(): Mon=0, Sun=6
    sunday = monday + timedelta(days=6)
    return monday, sunday


def fetch_manus_tasks(created_after: datetime) -> list[dict]:
    """Fetch tasks from the Manus API created after the given datetime.

    Handles pagination via has_more / last_id cursor.
    Returns list of task dicts with keys: task_title, task_url, created_at.
    """
    api_key = os.getenv('MANUS_API_KEY')
    if not api_key:
        logger.error("MANUS_API_KEY not set in environment")
        return []

    url = f"{MANUS_API_BASE}/tasks"
    headers = {"API_KEY": api_key}
    created_after_ts = int(created_after.timestamp())

    all_tasks = []
    after_cursor = None

    with httpx.Client(timeout=30) as client:
        while True:
            params = {
                "createdAfter": created_after_ts,
                "limit": 100,
                "orderBy": "created_at",
                "order": "desc",
            }
            if after_cursor:
                params["after"] = after_cursor

            try:
                response = client.get(url, headers=headers, params=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Manus API error: {e.response.status_code} - {e.response.text}")
                return all_tasks
            except httpx.RequestError as e:
                logger.error(f"Manus API request failed: {e}")
                return all_tasks

            data = response.json()

            for task in data.get("data", []):
                metadata = task.get("metadata", {})
                created_at_raw = task.get("created_at")
                created_at = int(created_at_raw) if created_at_raw else None
                all_tasks.append({
                    "task_title": metadata.get("task_title", "Untitled Task"),
                    "task_url": metadata.get("task_url", ""),
                    "created_at": created_at,
                })

            if not data.get("has_more"):
                break

            after_cursor = data.get("last_id")
            if not after_cursor:
                break

    return all_tasks


def _find_weekly_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find folder ending with '_Weekly' in the vault."""
    result = dbx.files_list_folder(vault_path)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Weekly"):
                return entry.path_lower

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    raise FileNotFoundError("Could not find '_Weekly' folder in Dropbox")


def _find_weekly_file(dbx: dropbox.Dropbox, weeks_folder: str, sunday_date) -> tuple[str, str] | None:
    """Find the Week-Ending file for the given Sunday date.

    Returns (file_path_display, filename) or None if not found.
    """
    expected_name = f"Week-Ending-{sunday_date.strftime('%Y-%m-%d')}.md"

    result = dbx.files_list_folder(weeks_folder)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FileMetadata) and entry.name == expected_name:
                return entry.path_display, entry.name

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    return None


def _get_file_content(dbx: dropbox.Dropbox, file_path: str) -> str:
    """Download and return the content of a file from Dropbox."""
    _, response = dbx.files_download(file_path)
    return response.content.decode('utf-8')


def extract_existing_manus_urls(content: str) -> set[str]:
    """Extract Manus task URLs already present in the file's Manus Tasks section."""
    urls = set()

    lines = content.split('\n')
    in_section = False

    for line in lines:
        if line.strip() == MANUS_TASKS_HEADER:
            in_section = True
            continue
        if in_section and line.startswith('## '):
            break
        if in_section:
            match = MANUS_URL_PATTERN.search(line)
            if match:
                urls.add(match.group(1))

    return urls


def format_manus_section(tasks: list[dict], tz) -> str:
    """Format tasks into a Manus Tasks markdown section.

    Tasks are sorted by created_at ascending (oldest first).
    """
    # Sort oldest first for chronological order
    sorted_tasks = sorted(tasks, key=lambda t: t.get("created_at", 0))

    lines = [MANUS_TASKS_HEADER]
    for task in sorted_tasks:
        title = task["task_title"]
        url = task["task_url"]
        created_ts = task.get("created_at")
        if created_ts:
            dt = datetime.fromtimestamp(created_ts, tz=tz)
            date_str = dt.strftime('%Y-%m-%d')
        else:
            date_str = "unknown date"

        lines.append(f"- [{title}]({url}) - {date_str}")

    return '\n'.join(lines)


def main():
    args = parse_args()

    timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")
    system_tz = pytz.timezone(timezone_str)
    logger.info(f"Using timezone: {timezone_str}")

    monday, sunday = compute_week_bounds(system_tz)
    logger.info(f"Current week: {monday} (Mon) to {sunday} (Sun)")

    # Fetch tasks created from Monday 00:00 onward
    monday_start = system_tz.localize(datetime(monday.year, monday.month, monday.day, 0, 0, 0))
    tasks = fetch_manus_tasks(monday_start)

    if not tasks:
        logger.info("No Manus tasks found for this week.")
        return

    logger.info(f"Found {len(tasks)} Manus tasks")

    if args.dry_run:
        print(f"\n{'=' * 60}")
        print(f"DRY RUN - {len(tasks)} Manus tasks found (Week ending {sunday})")
        print(f"{'=' * 60}")
        sorted_tasks = sorted(tasks, key=lambda t: t.get("created_at", 0))
        for task in sorted_tasks:
            created_ts = task.get("created_at")
            if created_ts:
                dt = datetime.fromtimestamp(created_ts, tz=system_tz)
                date_str = dt.strftime('%Y-%m-%d')
            else:
                date_str = "unknown"
            print(f"  {date_str}  {task['task_title']}")
            print(f"              {task['task_url']}")
        print(f"{'=' * 60}\n")
        return

    # Connect to Dropbox and find the weekly note
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

    dbx = _get_dropbox_client()

    weekly_folder = _find_weekly_folder(dbx, vault_path)
    weeks_folder = f"{weekly_folder}/_Weeks"

    # Verify _Weeks subfolder exists
    try:
        dbx.files_get_metadata(weeks_folder)
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.GetMetadataError):
            raise FileNotFoundError("'_Weeks' subfolder not found")
        raise

    result = _find_weekly_file(dbx, weeks_folder, sunday)
    if result is None:
        logger.warning(f"Weekly note not found: Week-Ending-{sunday.strftime('%Y-%m-%d')}.md -- skipping Manus task append")
        return

    file_path, filename = result
    logger.info(f"Found weekly note: {filename}")

    content = _get_file_content(dbx, file_path)

    # Deduplicate against existing entries
    existing_urls = extract_existing_manus_urls(content)
    new_tasks = [t for t in tasks if t["task_url"] not in existing_urls]

    if not new_tasks:
        logger.info("All Manus tasks already present in the weekly note. Nothing to append.")
        return

    logger.info(f"{len(new_tasks)} new tasks to append ({len(existing_urls)} already present)")

    # Build the section and append
    if existing_urls:
        # Section header already exists — append only the new task lines
        new_lines = []
        sorted_new = sorted(new_tasks, key=lambda t: t.get("created_at", 0))
        for task in sorted_new:
            created_ts = task.get("created_at")
            if created_ts:
                dt = datetime.fromtimestamp(created_ts, tz=system_tz)
                date_str = dt.strftime('%Y-%m-%d')
            else:
                date_str = "unknown date"
            new_lines.append(f"- [{task['task_title']}]({task['task_url']}) - {date_str}")

        # Find the end of the Manus Tasks section and insert before it
        lines = content.split('\n')
        insert_index = len(lines)  # default: end of file
        in_section = False
        for i, line in enumerate(lines):
            if line.strip() == MANUS_TASKS_HEADER:
                in_section = True
                continue
            if in_section and line.startswith('## '):
                insert_index = i
                break

        for j, new_line in enumerate(new_lines):
            lines.insert(insert_index + j, new_line)

        updated_content = '\n'.join(lines)
    else:
        # No existing section — append the full section to the bottom
        section = format_manus_section(new_tasks, system_tz)
        updated_content = content.rstrip('\n') + '\n\n' + section + '\n'

    dbx.files_upload(
        updated_content.encode('utf-8'),
        file_path,
        mode=dropbox.files.WriteMode.overwrite
    )

    logger.info(f"Successfully appended {len(new_tasks)} Manus tasks to {filename}")


if __name__ == "__main__":
    main()
