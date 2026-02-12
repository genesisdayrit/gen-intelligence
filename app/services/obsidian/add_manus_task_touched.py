"""Service for writing Manus task events to Daily Action and Weekly Cycle notes."""

import logging
import os
import re
from datetime import datetime, timedelta

import dropbox
import pytz
import redis
import requests
from dotenv import load_dotenv

from services.obsidian.utils.date_helpers import get_effective_date

load_dotenv()

logger = logging.getLogger(__name__)

# Redis configuration
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', None)
redis_client = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)

# Timezone
timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")

# Section headers
DAILY_ACTION_HEADER = "### Manus Tasks:"
WEEKLY_CYCLE_HEADER = "##### Manus Tasks:"

# Daily Action section ordering (must match add_daily_action_updates.py)
DAILY_INITIATIVE_HEADER = "### Initiative Updates:"
DAILY_PROJECT_HEADER = "### Project Updates:"
DAILY_TODOIST_HEADER = "### Completed Tasks on Todoist:"
DAILY_ISSUES_TOUCHED_HEADER = "### Linear Issues Touched:"
DAILY_TEMPLATE_BOUNDARY = "Vision Objective 1:"

# Weekly Cycle section ordering (must match add_weekly_cycle_updates.py)
WEEKLY_INITIATIVE_HEADER = "##### Initiative Updates:"
WEEKLY_PROJECT_HEADER = "##### Project Updates:"
WEEKLY_COMPLETED_HEADER = "##### Completed Tasks:"
WEEKLY_ISSUES_TOUCHED_HEADER = "##### Linear Issues Touched:"

# Patterns
DAY_SECTION_PATTERN = re.compile(r'^### (Wednesday|Thursday|Friday|Saturday|Sunday|Monday|Tuesday) -', re.MULTILINE)


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


# --- Daily Action helpers ---

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
    effective_date = get_effective_date(now)
    formatted_date = effective_date.strftime('%Y-%m-%d')
    return f"{daily_action_folder_path}/DA {formatted_date}.md"


def _get_file_content(dbx: dropbox.Dropbox, file_path: str) -> str:
    """Fetch file content from Dropbox."""
    try:
        _, response = dbx.files_download(file_path)
        return response.content.decode('utf-8')
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.DownloadError):
            raise FileNotFoundError(f"File not found: {file_path}")
        raise


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
    main_content = '\n'.join(main_content_lines)
    main_content = main_content.lstrip('\n')

    return yaml_section, main_content


def _find_daily_review_end(content: str) -> int | None:
    """Find the line index after Daily Review's ending '---'."""
    lines = content.split('\n')
    in_daily_review = False

    for i, line in enumerate(lines):
        if 'Daily Review:' in line:
            in_daily_review = True

        if in_daily_review and line.strip() == '---':
            return i + 1

    return None


def _get_daily_section_order() -> list[str]:
    """Return the ordered list of section headers for Daily Action."""
    return [DAILY_INITIATIVE_HEADER, DAILY_PROJECT_HEADER, DAILY_TODOIST_HEADER, DAILY_ISSUES_TOUCHED_HEADER, DAILY_ACTION_HEADER]


def _upsert_daily_action_manus_touched(task_id: str, task_title: str, task_url: str) -> dict:
    """Add a Manus task entry to today's Daily Action note.

    Args:
        task_id: The Manus task ID (used in display and deduplication)
        task_title: The task title
        task_url: The task URL (used for deduplication)

    Returns:
        dict with keys: success, action ("inserted" or "skipped"), error (if any)
    """
    try:
        vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
        if not vault_path:
            raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

        dbx = _get_dropbox_client()

        # Find Daily Action file
        daily_folder = _find_daily_folder(dbx, vault_path)
        daily_action_folder = _find_daily_action_folder(dbx, daily_folder)
        file_path = _get_today_daily_action_path(daily_action_folder)
        file_content = _get_file_content(dbx, file_path)

        # Format the entry
        log_entry = f"- {task_title} ([{task_id}]({task_url}))"

        # Parse YAML frontmatter
        yaml_section, main_content = _parse_yaml_frontmatter(file_content)

        lines = main_content.split('\n')

        # Find Daily Review end line index
        daily_review_end_line = _find_daily_review_end(main_content)
        if daily_review_end_line is None:
            daily_review_end_line = 0

        # Check if this URL already exists in the file (deduplication)
        for line in lines:
            if task_url in line:
                return {"success": True, "action": "skipped"}

        # Insert new entry - find or create the Manus Tasks Touched section
        target_header = DAILY_ACTION_HEADER
        section_order = _get_daily_section_order()

        # Find existing headers in the content (after Daily Review)
        header_positions = {}
        for i, line in enumerate(lines):
            if i < daily_review_end_line:
                continue
            for header in section_order:
                if line.strip() == header:
                    header_positions[header] = i

        if target_header in header_positions:
            # Header exists - insert after existing task entries (skip trailing blank lines)
            header_index = header_positions[target_header]
            insert_index = header_index + 1
            for i in range(header_index + 1, len(lines)):
                line = lines[i]
                if line.strip().startswith('#'):
                    break
                elif line.strip() == '---':
                    break
                elif line.strip() == DAILY_TEMPLATE_BOUNDARY:
                    break
                elif line.strip() == '':
                    # Don't advance past blank lines — insert before them
                    break
                else:
                    insert_index = i + 1
            lines.insert(insert_index, log_entry)
            # Ensure a blank line between entries and next section
            next_idx = insert_index + 1
            if next_idx < len(lines) and lines[next_idx].strip() != '':
                lines.insert(next_idx, '')
        else:
            # Header doesn't exist - create it in the right position
            target_order_index = section_order.index(target_header)

            # Find the first existing header that comes after our target
            insert_before_index = None
            for later_header in section_order[target_order_index + 1:]:
                if later_header in header_positions:
                    insert_before_index = header_positions[later_header]
                    break

            if insert_before_index is not None:
                # Insert before the next section
                lines.insert(insert_before_index, '')
                lines.insert(insert_before_index, log_entry)
                lines.insert(insert_before_index, target_header)
                lines.insert(insert_before_index, '')
            else:
                # No later headers exist - find the best insertion point
                # Look for template boundary or insert after Daily Review
                insert_pos = None
                for i in range(len(lines) - 1, daily_review_end_line - 1, -1):
                    if lines[i].strip() == DAILY_TEMPLATE_BOUNDARY:
                        insert_pos = i
                        break

                if insert_pos is None:
                    insert_pos = daily_review_end_line

                new_lines = ['', target_header, log_entry, '']
                for j, new_line in enumerate(new_lines):
                    lines.insert(insert_pos + j, new_line)

        updated_main_content = '\n'.join(lines)
        updated_content = yaml_section + updated_main_content

        # Upload updated content
        dbx.files_upload(
            updated_content.encode('utf-8'),
            file_path,
            mode=dropbox.files.WriteMode.overwrite
        )

        return {"success": True, "action": "inserted"}

    except Exception as e:
        return {"success": False, "action": None, "error": str(e)}


# --- Weekly Cycle helpers ---

def _find_cycles_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find folder ending with '_Cycles' in the vault."""
    result = dbx.files_list_folder(vault_path)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Cycles"):
                return entry.path_lower

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    raise FileNotFoundError("Could not find '_Cycles' folder in Dropbox")


def _get_current_week_bounds(tz) -> tuple[datetime, datetime]:
    """Calculate the Wednesday-Tuesday bounds for the current week's cycle."""
    now = datetime.now(tz)
    effective_now = get_effective_date(now)

    days_since_wednesday = (effective_now.weekday() - 2) % 7
    cycle_start = effective_now - timedelta(days=days_since_wednesday)
    cycle_end = cycle_start + timedelta(days=6)

    return cycle_start, cycle_end


def _format_date_range(cycle_start: datetime, cycle_end: datetime) -> str:
    """Format the date range string to match file naming convention."""
    start_str = f"{cycle_start.strftime('%b')}. {cycle_start.strftime('%d')}"
    end_str = f"{cycle_end.strftime('%b')}. {cycle_end.strftime('%d')}, {cycle_end.strftime('%Y')}"
    return f"({start_str} - {end_str})"


def _find_weekly_cycle_file(dbx: dropbox.Dropbox, weekly_cycles_folder_path: str, date_range: str) -> tuple[str, str]:
    """Find the weekly cycle file matching the given date range."""
    result = dbx.files_list_folder(weekly_cycles_folder_path)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FileMetadata) and date_range in entry.name:
                return entry.path_display, entry.name

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    raise FileNotFoundError(f"Could not find weekly cycle file for date range: {date_range}")


def _get_current_day_name(tz) -> str:
    """Get the effective day of week name."""
    now = datetime.now(tz)
    effective_now = get_effective_date(now)
    return effective_now.strftime('%A')


def _get_weekly_section_order() -> list[str]:
    """Return the ordered list of section headers for Weekly Cycle."""
    return [WEEKLY_INITIATIVE_HEADER, WEEKLY_PROJECT_HEADER, WEEKLY_COMPLETED_HEADER, WEEKLY_ISSUES_TOUCHED_HEADER, WEEKLY_CYCLE_HEADER]


def _upsert_weekly_cycle_manus_touched(task_id: str, task_title: str, task_url: str) -> dict:
    """Add a Manus task entry to today's section in the Weekly Cycle note.

    Args:
        task_id: The Manus task ID (used in display and deduplication)
        task_title: The task title
        task_url: The task URL (used for deduplication)

    Returns:
        dict with keys: success, action ("inserted" or "skipped"), error (if any)
    """
    try:
        vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
        if not vault_path:
            raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

        dbx = _get_dropbox_client()

        # Find cycles folder and weekly cycles subfolder
        cycles_folder = _find_cycles_folder(dbx, vault_path)
        weekly_cycles_folder = f"{cycles_folder}/_Weekly-Cycles"

        try:
            dbx.files_get_metadata(weekly_cycles_folder)
        except dropbox.exceptions.ApiError as e:
            if isinstance(e.error, dropbox.files.GetMetadataError):
                raise FileNotFoundError("'_Weekly-Cycles' subfolder not found")
            raise

        # Calculate current week's bounds and find file
        system_tz = pytz.timezone(timezone_str)
        cycle_start, cycle_end = _get_current_week_bounds(system_tz)
        date_range = _format_date_range(cycle_start, cycle_end)

        file_path, _ = _find_weekly_cycle_file(dbx, weekly_cycles_folder, date_range)
        file_content = _get_file_content(dbx, file_path)

        # Format the entry
        log_entry = f"- {task_title} ([{task_id}]({task_url}))"

        # Get current day name and find the section
        day_name = _get_current_day_name(system_tz)
        day_section_header = f"### {day_name} -"

        lines = file_content.split('\n')
        day_section_start = None
        day_section_end = None

        # Find the day section boundaries
        for i, line in enumerate(lines):
            if line.strip() == day_section_header:
                day_section_start = i
                continue

            if day_section_start is not None and day_section_end is None:
                if line.strip() == '---':
                    day_section_end = i
                    break

        if day_section_start is None:
            raise ValueError(f"Could not find day section '{day_section_header}' in weekly cycle file")

        if day_section_end is None:
            day_section_end = len(lines)

        # Check if this URL already exists in the day section (deduplication)
        for i in range(day_section_start, day_section_end):
            if task_url in lines[i]:
                return {"success": True, "action": "skipped"}

        # Insert new entry - find or create the Manus Tasks Touched section
        target_header = WEEKLY_CYCLE_HEADER
        section_order = _get_weekly_section_order()

        # Find existing headers in the day section
        header_positions = {}
        for i in range(day_section_start, day_section_end):
            for header in section_order:
                if lines[i].strip() == header:
                    header_positions[header] = i

        if target_header in header_positions:
            # Header exists - insert after existing task entries (skip trailing blank lines)
            header_index = header_positions[target_header]
            insert_index = header_index + 1
            for i in range(header_index + 1, day_section_end):
                line = lines[i]
                if line.strip().startswith('#'):
                    break
                elif line.strip() == '':
                    break
                else:
                    insert_index = i + 1
            lines.insert(insert_index, log_entry)
            # Ensure a blank line between entries and next section
            next_idx = insert_index + 1
            if next_idx < len(lines) and lines[next_idx].strip() != '':
                lines.insert(next_idx, '')
        else:
            # Header doesn't exist - create it in the right position
            target_order_index = section_order.index(target_header)

            # Find the first existing header that comes after our target
            insert_before_index = None
            for later_header in section_order[target_order_index + 1:]:
                if later_header in header_positions:
                    insert_before_index = header_positions[later_header]
                    break

            if insert_before_index is not None:
                # Insert before the next section
                lines.insert(insert_before_index, '')
                lines.insert(insert_before_index, log_entry)
                lines.insert(insert_before_index, target_header)
                lines.insert(insert_before_index, '')
            else:
                # No later headers exist - insert before the --- separator or at end of section
                insert_pos = day_section_end
                for i in range(day_section_end - 1, day_section_start, -1):
                    if lines[i].strip() == '---':
                        insert_pos = i
                        break
                    elif lines[i].strip() != '':
                        insert_pos = i + 1
                        break

                new_lines = ['', target_header, log_entry, '']
                for j, new_line in enumerate(new_lines):
                    lines.insert(insert_pos + j, new_line)

        updated_content = '\n'.join(lines)

        # Upload updated content
        dbx.files_upload(
            updated_content.encode('utf-8'),
            file_path,
            mode=dropbox.files.WriteMode.overwrite
        )

        return {"success": True, "action": "inserted"}

    except Exception as e:
        return {"success": False, "action": None, "error": str(e)}


# --- Main entry point ---

def upsert_manus_task_touched(task_id: str, task_title: str, task_url: str) -> dict:
    """Add a Manus task to both Daily Action and Weekly Cycle notes.

    Writes to Daily Action first, then Weekly Cycle. Both operations are
    independent - if one fails, the other's success/failure is unaffected.

    Args:
        task_id: The Manus task ID
        task_title: The task title
        task_url: The task URL (used for deduplication and linking)

    Returns:
        dict with keys:
            - daily_action_success: bool
            - daily_action_action: str | None ("inserted" or "skipped")
            - daily_action_error: str | None
            - weekly_cycle_success: bool
            - weekly_cycle_action: str | None ("inserted" or "skipped")
            - weekly_cycle_error: str | None
    """
    result = {
        "daily_action_success": False,
        "daily_action_action": None,
        "daily_action_error": None,
        "weekly_cycle_success": False,
        "weekly_cycle_action": None,
        "weekly_cycle_error": None,
    }

    # Write to Daily Action
    try:
        da_result = _upsert_daily_action_manus_touched(task_id, task_title, task_url)
        result["daily_action_success"] = da_result["success"]
        result["daily_action_action"] = da_result.get("action")
        if not da_result["success"]:
            result["daily_action_error"] = da_result.get("error")
        else:
            logger.info("Manus task written to Daily Action: action=%s", da_result["action"])
    except Exception as e:
        result["daily_action_error"] = str(e)
        logger.error("Failed to write Manus task to Daily Action: %s", e)

    # Write to Weekly Cycle
    try:
        wc_result = _upsert_weekly_cycle_manus_touched(task_id, task_title, task_url)
        result["weekly_cycle_success"] = wc_result["success"]
        result["weekly_cycle_action"] = wc_result.get("action")
        if not wc_result["success"]:
            result["weekly_cycle_error"] = wc_result.get("error")
        else:
            logger.info("Manus task written to Weekly Cycle: action=%s", wc_result["action"])
    except Exception as e:
        result["weekly_cycle_error"] = str(e)
        logger.error("Failed to write Manus task to Weekly Cycle: %s", e)

    return result
