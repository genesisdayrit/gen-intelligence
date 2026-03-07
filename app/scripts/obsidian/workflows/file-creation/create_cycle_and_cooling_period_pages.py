#!/usr/bin/env python3
"""Create 6-week cycle and 2-week cooling period files in Obsidian vault via Dropbox.

Manages the lifecycle of 6-week cycles and 2-week cooling periods by:
1. Resolving cycle dates in Redis (handling overlaps and expired periods)
2. Ensuring the _6-Week-Cycles folder exists in Dropbox
3. Creating numbered cycle and cooling period files when future coverage is missing

Usage:
    python -m scripts.obsidian.workflows.file-creation.create_cycle_and_cooling_period_pages
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


def _list_all_entries(dbx: dropbox.Dropbox, folder_path: str) -> list:
    """List all entries in a Dropbox folder, handling pagination."""
    entries = []
    try:
        response = dbx.files_list_folder(folder_path)
        entries.extend(response.entries)
        while response.has_more:
            response = dbx.files_list_folder_continue(response.cursor)
            entries.extend(response.entries)
    except dropbox.exceptions.ApiError as e:
        logger.error(f"Error fetching folder list from {folder_path}: {e}")
    return entries


# ===== Redis Cycle Date Management =====

def _display_cycle_variables():
    """Display all cycle-related variables from Redis."""
    logger.info("Current cycle variables in Redis:")
    cooling_vars = [
        'two_week_cooling_period_start_date',
        'two_week_cooling_period_end_date',
        'next_two_week_cooling_period_start_date',
        'next_two_week_cooling_period_end_date'
    ]
    cycle_vars = [
        '6_week_cycle_start_date',
        '6_week_cycle_end_date',
        'next_6_week_cycle_start_date',
        'next_6_week_cycle_end_date'
    ]
    for var in cooling_vars + cycle_vars:
        value = redis_client.get(var)
        logger.info(f"  {var}: {value}")


def _calculate_two_week_cooling_periods(start_date: str) -> dict:
    """Calculate all dates related to the two-week cooling period."""
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = start + timedelta(days=13)
    next_start = start + timedelta(weeks=8)
    next_end = next_start + timedelta(days=13)
    return {
        'two_week_cooling_period_start_date': start_date,
        'two_week_cooling_period_end_date': end.strftime('%Y-%m-%d'),
        'next_two_week_cooling_period_start_date': next_start.strftime('%Y-%m-%d'),
        'next_two_week_cooling_period_end_date': next_end.strftime('%Y-%m-%d')
    }


def _calculate_six_week_cycles(start_date: str) -> dict:
    """Calculate all dates related to the six-week cycle."""
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = start + timedelta(days=(7 * 6 - 1))
    next_start = end + timedelta(days=15)
    next_end = next_start + timedelta(days=(7 * 6 - 1))
    return {
        '6_week_cycle_start_date': start_date,
        '6_week_cycle_end_date': end.strftime('%Y-%m-%d'),
        'next_6_week_cycle_start_date': next_start.strftime('%Y-%m-%d'),
        'next_6_week_cycle_end_date': next_end.strftime('%Y-%m-%d')
    }


def _is_date_between(check_date, start_date, end_date) -> bool:
    """Check if a date falls between start and end dates (inclusive)."""
    return start_date <= check_date <= end_date


def _update_redis_dates(date_dict: dict):
    """Update multiple Redis keys with their corresponding values."""
    for key, value in date_dict.items():
        redis_client.set(key, value)
        logger.info(f"Updated {key}: {value}")


def _resolve_cycle_dates() -> bool:
    """Resolve any issues with cycle dates in Redis.

    Returns:
        True if changes were made, False otherwise.
    """
    logger.info("Resolving cycle dates...")

    today = datetime.now().strftime('%Y-%m-%d')
    today_date = datetime.strptime(today, '%Y-%m-%d')

    cooling_start = redis_client.get('two_week_cooling_period_start_date')
    cooling_end = redis_client.get('two_week_cooling_period_end_date')
    cycle_start = redis_client.get('6_week_cycle_start_date')
    cycle_end = redis_client.get('6_week_cycle_end_date')

    if not all([cooling_start, cooling_end, cycle_start, cycle_end]):
        logger.error("One or more required dates not found in Redis.")
        return False

    cooling_start_date = datetime.strptime(cooling_start, '%Y-%m-%d')
    cooling_end_date = datetime.strptime(cooling_end, '%Y-%m-%d')
    cycle_start_date = datetime.strptime(cycle_start, '%Y-%m-%d')
    cycle_end_date = datetime.strptime(cycle_end, '%Y-%m-%d')

    in_cooling = _is_date_between(today_date, cooling_start_date, cooling_end_date)
    in_cycle = _is_date_between(today_date, cycle_start_date, cycle_end_date)

    if in_cooling and cycle_end_date < today_date:
        new_start = (cooling_end_date + timedelta(days=1)).strftime('%Y-%m-%d')
        _update_redis_dates(_calculate_six_week_cycles(new_start))
        return True
    elif in_cycle and cooling_end_date < today_date:
        new_start = (cycle_end_date + timedelta(days=1)).strftime('%Y-%m-%d')
        _update_redis_dates(_calculate_two_week_cooling_periods(new_start))
        return True
    elif in_cooling:
        cooling_overlaps = (cooling_start_date <= cycle_start_date <= cooling_end_date) or \
                           (cooling_start_date <= cycle_end_date <= cooling_end_date)
        if cooling_overlaps:
            new_start = (cooling_end_date + timedelta(days=1)).strftime('%Y-%m-%d')
            _update_redis_dates(_calculate_six_week_cycles(new_start))
            return True
    elif in_cycle:
        cycle_overlaps = (cycle_start_date <= cooling_start_date <= cycle_end_date) or \
                         (cycle_start_date <= cooling_end_date <= cycle_end_date)
        if cycle_overlaps:
            new_start = (cycle_end_date + timedelta(days=1)).strftime('%Y-%m-%d')
            _update_redis_dates(_calculate_two_week_cooling_periods(new_start))
            return True
    elif cooling_end_date < today_date and cycle_end_date < today_date:
        if cooling_end_date > cycle_end_date:
            new_start = (cooling_end_date + timedelta(days=1)).strftime('%Y-%m-%d')
            if datetime.strptime(new_start, '%Y-%m-%d') < today_date:
                new_start = (today_date + timedelta(days=1)).strftime('%Y-%m-%d')
            _update_redis_dates(_calculate_six_week_cycles(new_start))
            return True
        else:
            new_start = (cycle_end_date + timedelta(days=1)).strftime('%Y-%m-%d')
            if datetime.strptime(new_start, '%Y-%m-%d') < today_date:
                new_start = (today_date + timedelta(days=1)).strftime('%Y-%m-%d')
            _update_redis_dates(_calculate_two_week_cooling_periods(new_start))
            return True

    logger.info("No updates were needed for cycle dates.")
    return False


# ===== Dropbox File Management =====

def _find_cycles_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find folder containing '_Cycles' in the vault."""
    result = dbx.files_list_folder(vault_path)
    for entry in result.entries:
        if isinstance(entry, dropbox.files.FolderMetadata) and '_Cycles' in entry.name:
            return entry.path_lower
    raise FileNotFoundError("Could not find a folder containing '_Cycles' in Dropbox")


def _ensure_six_week_cycles_folder(dbx: dropbox.Dropbox, cycles_path: str) -> str:
    """Ensure the _6-Week-Cycles folder exists, creating it if necessary."""
    six_week_path = f"{cycles_path}/_6-Week-Cycles"
    try:
        dbx.files_get_metadata(six_week_path)
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.GetMetadataError):
            logger.info("'_6-Week-Cycles' folder not found. Creating it now.")
            dbx.files_create_folder_v2(six_week_path)
        else:
            raise
    return six_week_path


def _format_date_for_filename(date_str: str) -> str:
    """Convert YYYY-MM-DD to YYYY.MM.DD format."""
    return datetime.strptime(date_str, '%Y-%m-%d').strftime('%Y.%m.%d')


def _has_future_coverage(dbx: dropbox.Dropbox, folder_path: str, cycle_type: str, today_date: datetime) -> tuple:
    """Check if there's at least one file of this type with an end date in the future."""
    entries = _list_all_entries(dbx, folder_path)
    pattern = rf'^{re.escape(cycle_type)} \d+ \((\d{{4}}\.\d{{2}}\.\d{{2}}) - (\d{{4}}\.\d{{2}}\.\d{{2}})\)\.md$'

    future_files = []
    for entry in entries:
        if isinstance(entry, dropbox.files.FileMetadata):
            match = re.match(pattern, entry.name)
            if match:
                end_date = datetime.strptime(match.group(2), '%Y.%m.%d')
                if end_date > today_date:
                    future_files.append({'filename': entry.name, 'end_date': end_date})

    if future_files:
        latest = max(future_files, key=lambda x: x['end_date'])
        return True, latest['end_date'], latest['filename']
    return False, None, None


def _get_next_cycle_number(dbx: dropbox.Dropbox, folder_path: str, cycle_type: str) -> int:
    """Get next sequential number for a cycle type based on latest end date."""
    entries = _list_all_entries(dbx, folder_path)
    pattern = rf'^{re.escape(cycle_type)} (\d+) \((\d{{4}}\.\d{{2}}\.\d{{2}}) - (\d{{4}}\.\d{{2}}\.\d{{2}})\)\.md$'

    files_data = []
    for entry in entries:
        if isinstance(entry, dropbox.files.FileMetadata):
            match = re.match(pattern, entry.name)
            if match:
                files_data.append({
                    'number': int(match.group(1)),
                    'end_date': datetime.strptime(match.group(3), '%Y.%m.%d')
                })

    if not files_data:
        return 1

    latest = max(files_data, key=lambda x: x['end_date'])
    return latest['number'] + 1


def _create_cycle_files(dbx: dropbox.Dropbox, six_week_cycles_path: str) -> bool:
    """Create cycle and cooling period files in Dropbox."""
    today_date = datetime.now()

    cooling_has_future, _, _ = _has_future_coverage(
        dbx, six_week_cycles_path, "2-Week Cooling Period", today_date
    )
    cycle_has_future, _, _ = _has_future_coverage(
        dbx, six_week_cycles_path, "6-Week Cycle", today_date
    )

    if cooling_has_future and cycle_has_future:
        logger.info("Both cycle types have future coverage. No new files needed.")
        return True

    # Get cycle dates from Redis
    current_cooling_start = redis_client.get('two_week_cooling_period_start_date')
    current_cooling_end = redis_client.get('two_week_cooling_period_end_date')
    next_cooling_start = redis_client.get('next_two_week_cooling_period_start_date')
    next_cooling_end = redis_client.get('next_two_week_cooling_period_end_date')
    current_cycle_start = redis_client.get('6_week_cycle_start_date')
    current_cycle_end = redis_client.get('6_week_cycle_end_date')
    next_cycle_start = redis_client.get('next_6_week_cycle_start_date')
    next_cycle_end = redis_client.get('next_6_week_cycle_end_date')

    if not all([current_cooling_start, current_cooling_end, next_cooling_start, next_cooling_end,
                current_cycle_start, current_cycle_end, next_cycle_start, next_cycle_end]):
        logger.error("Missing cycle date values in Redis.")
        return False

    # Get next sequential numbers
    next_cooling_number = _get_next_cycle_number(dbx, six_week_cycles_path, "2-Week Cooling Period")
    next_cycle_number = _get_next_cycle_number(dbx, six_week_cycles_path, "6-Week Cycle")

    # Build filenames
    files_to_create = []
    if not cooling_has_future:
        files_to_create.extend([
            f"2-Week Cooling Period {next_cooling_number} ({_format_date_for_filename(current_cooling_start)} - {_format_date_for_filename(current_cooling_end)}).md",
            f"2-Week Cooling Period {next_cooling_number + 1} ({_format_date_for_filename(next_cooling_start)} - {_format_date_for_filename(next_cooling_end)}).md",
        ])
    if not cycle_has_future:
        files_to_create.extend([
            f"6-Week Cycle {next_cycle_number} ({_format_date_for_filename(current_cycle_start)} - {_format_date_for_filename(current_cycle_end)}).md",
            f"6-Week Cycle {next_cycle_number + 1} ({_format_date_for_filename(next_cycle_start)} - {_format_date_for_filename(next_cycle_end)}).md",
        ])

    for filename in files_to_create:
        file_path = f"{six_week_cycles_path}/{filename}"
        try:
            dbx.files_get_metadata(file_path)
            logger.info(f"File already exists: {filename}")
        except dropbox.exceptions.ApiError as e:
            if isinstance(e.error, dropbox.files.GetMetadataError):
                dbx.files_upload(b"", file_path)
                logger.info(f"Created file: {filename}")
            else:
                logger.error(f"Dropbox API error for {filename}: {e}")

    return True


def create_cycle_and_cooling_period_pages() -> bool:
    """Create 6-week cycle and 2-week cooling period files.

    Resolves Redis cycle dates, ensures folder structure exists,
    and creates any missing cycle/cooling period files.

    Returns:
        True on success, False on error.
    """
    dropbox_vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not dropbox_vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return False

    try:
        # Step 1: Resolve cycle dates in Redis
        _display_cycle_variables()
        _resolve_cycle_dates()

        # Step 2: Set up Dropbox folders
        dbx = _get_dropbox_client()
        cycles_folder_path = _find_cycles_folder(dbx, dropbox_vault_path)
        six_week_cycles_path = _ensure_six_week_cycles_folder(dbx, cycles_folder_path)

        # Step 3: Create files
        return _create_cycle_files(dbx, six_week_cycles_path)

    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
        return False
    except dropbox.exceptions.AuthError as e:
        logger.error(f"Dropbox authentication error: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Create 6-week cycle and 2-week cooling period files in Obsidian vault"
    )
    parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    success = create_cycle_and_cooling_period_pages()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
