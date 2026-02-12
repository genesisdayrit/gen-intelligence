"""Dropbox helper for writing Linear Issues Touched to Weekly Cycle notes."""

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

# Redis configuration
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', None)
redis_client = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)

# Timezone
timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")

# Section headers
INITIATIVE_UPDATES_HEADER = "##### Initiative Updates:"
PROJECT_UPDATES_HEADER = "##### Project Updates:"
COMPLETED_TASKS_HEADER = "##### Completed Tasks:"
ISSUES_TOUCHED_HEADER = "##### Linear Issues Touched:"

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
    """Calculate the Wednesday-Tuesday bounds for the current week's cycle.

    Uses a 3-hour buffer: updates between midnight and 3am count as the previous day.
    """
    now = datetime.now(tz)
    effective_now = get_effective_date(now)

    # Wednesday is weekday 2 (Monday=0, Tuesday=1, Wednesday=2, ...)
    days_since_wednesday = (effective_now.weekday() - 2) % 7

    cycle_start = effective_now - timedelta(days=days_since_wednesday)
    cycle_end = cycle_start + timedelta(days=6)  # Tuesday

    return cycle_start, cycle_end


def _format_date_range(cycle_start: datetime, cycle_end: datetime) -> str:
    """Format the date range string to match file naming convention.

    Format: (Jan. 07 - Jan. 13, 2026)
    """
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


def _get_weekly_cycle_content(dbx: dropbox.Dropbox, file_path: str) -> str:
    """Download and return the content of the weekly cycle file."""
    try:
        _, response = dbx.files_download(file_path)
        return response.content.decode('utf-8')
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.DownloadError):
            raise FileNotFoundError(f"Weekly cycle file not found: {file_path}")
        raise


def _get_current_day_name(tz) -> str:
    """Get the effective day of week name.

    Uses a 3-hour buffer: midnight-3am counts as the previous day.
    """
    now = datetime.now(tz)
    effective_now = get_effective_date(now)
    return effective_now.strftime('%A')  # Returns "Wednesday", "Thursday", etc.


def _to_native_app_url(url: str) -> str:
    """Convert Linear browser URL to native app URL.

    Transforms https://linear.app/... to linear://...
    """
    return url.replace("https://linear.app/", "linear://")


def _format_issue_entry(
    issue_identifier: str,
    project_name: str,
    issue_title: str,
    status_name: str,
    issue_url: str,
) -> str:
    """Format a single issue entry line.

    Format: GD-328 Project Name - Issue Title (In Progress) ([link](linear://...))
    """
    native_url = _to_native_app_url(issue_url)
    if project_name:
        return f"{issue_identifier} {project_name} - {issue_title} ({status_name}) ([link]({native_url}))"
    else:
        return f"{issue_identifier} {issue_title} ({status_name}) ([link]({native_url}))"


def upsert_weekly_cycle_issue_touched(
    issue_identifier: str,
    project_name: str,
    issue_title: str,
    status_name: str,
    issue_url: str,
    status_changed: bool,
) -> dict:
    """Upsert a Linear issue entry to the Linear Issues Touched section in the Weekly Cycle.

    Args:
        issue_identifier: Human-readable issue ID (e.g., "GD-328")
        project_name: Parent project name (may be empty)
        issue_title: Issue title
        status_name: Current workflow state name (e.g., "In Progress")
        issue_url: Linear URL for the issue
        status_changed: Whether the status was updated in this webhook event

    Returns:
        dict with keys: success, action ("inserted", "updated", or "skipped"), error (if any)
    """
    try:
        vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
        if not vault_path:
            raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

        dbx = _get_dropbox_client()

        # Find cycles folder and weekly cycles subfolder
        cycles_folder = _find_cycles_folder(dbx, vault_path)
        weekly_cycles_folder = f"{cycles_folder}/_Weekly-Cycles"

        # Verify the _Weekly-Cycles folder exists
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
        file_content = _get_weekly_cycle_content(dbx, file_path)

        # Format the entry
        entry_line = _format_issue_entry(issue_identifier, project_name, issue_title, status_name, issue_url)

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

        # If we didn't find the end, it's the last section
        if day_section_end is None:
            day_section_end = len(lines)

        # Check if this issue identifier already exists in the day section
        identifier_pattern = re.compile(rf'^{re.escape(issue_identifier)}\s')
        existing_line_index = None
        in_issues_section = False

        for i in range(day_section_start, day_section_end):
            if lines[i].strip() == ISSUES_TOUCHED_HEADER:
                in_issues_section = True
                continue
            if in_issues_section:
                if lines[i].strip().startswith('#') or lines[i].strip() == '---':
                    break
                if identifier_pattern.match(lines[i]):
                    existing_line_index = i
                    break

        if existing_line_index is not None:
            if not status_changed:
                # No status change - skip (no-op)
                return {"success": True, "action": "skipped"}

            # Status changed - update the existing line
            lines[existing_line_index] = entry_line
            action = "updated"
        else:
            # Issue not found - insert new entry
            issues_header_index = None
            for i in range(day_section_start, day_section_end):
                if lines[i].strip() == ISSUES_TOUCHED_HEADER:
                    issues_header_index = i
                    break

            if issues_header_index is not None:
                # Section exists - append after existing entries
                insert_index = issues_header_index + 1
                for i in range(issues_header_index + 1, day_section_end):
                    line = lines[i]
                    if line.strip() == '':
                        continue
                    elif line.strip().startswith('#') or line.strip() == '---':
                        break
                    else:
                        insert_index = i + 1

                lines.insert(insert_index, entry_line)
            else:
                # Section doesn't exist - create it before the --- separator
                # Find insert position: after Completed Tasks section, or before ---
                section_order = [INITIATIVE_UPDATES_HEADER, PROJECT_UPDATES_HEADER, COMPLETED_TASKS_HEADER, ISSUES_TOUCHED_HEADER]

                # Find existing section headers in the day section
                header_positions = {}
                for i in range(day_section_start, day_section_end):
                    for header in section_order:
                        if lines[i].strip() == header:
                            header_positions[header] = i

                # Find the last content line before the end separator
                insert_pos = day_section_end
                for i in range(day_section_end - 1, day_section_start, -1):
                    if lines[i].strip() == '---':
                        insert_pos = i
                        break
                    elif lines[i].strip() != '':
                        insert_pos = i + 1
                        break

                # Insert: blank line, header, entry, blank line
                new_lines = ['', ISSUES_TOUCHED_HEADER, entry_line, '']
                for j, new_line in enumerate(new_lines):
                    lines.insert(insert_pos + j, new_line)

            action = "inserted"

        updated_content = '\n'.join(lines)

        # Upload updated content
        dbx.files_upload(
            updated_content.encode('utf-8'),
            file_path,
            mode=dropbox.files.WriteMode.overwrite
        )

        return {"success": True, "action": action}

    except Exception as e:
        return {"success": False, "action": None, "error": str(e)}
