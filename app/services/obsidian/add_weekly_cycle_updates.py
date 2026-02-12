"""Dropbox helper for writing Linear Initiative/Project Updates to Weekly Cycle notes."""

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
MANUS_TASKS_TOUCHED_HEADER = "##### Manus Tasks:"

# Patterns
DAY_SECTION_PATTERN = re.compile(r'^### (Wednesday|Thursday|Friday|Saturday|Sunday|Monday|Tuesday) -', re.MULTILINE)
LOG_ENTRY_PATTERN = re.compile(r'^\[\d{2}:\d{2}\]')


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


def _get_section_header(section_type: str) -> str:
    """Get the header string for a section type."""
    if section_type == "initiative":
        return INITIATIVE_UPDATES_HEADER
    elif section_type == "project":
        return PROJECT_UPDATES_HEADER
    else:
        raise ValueError(f"Unknown section type: {section_type}")


def _get_section_order() -> list[str]:
    """Return the ordered list of section headers (top to bottom)."""
    return [INITIATIVE_UPDATES_HEADER, PROJECT_UPDATES_HEADER, COMPLETED_TASKS_HEADER, ISSUES_TOUCHED_HEADER, MANUS_TASKS_TOUCHED_HEADER]


def upsert_weekly_cycle_update(section_type: str, url: str, parent_name: str, content: str) -> dict:
    """Upsert an initiative or project update to today's section in the Weekly Cycle note.

    Args:
        section_type: Either "initiative" or "project"
        url: The Linear URL for the update (used as unique identifier)
        parent_name: The name of the initiative or project
        content: The update body text

    Returns:
        dict with keys: success, action ("inserted" or "updated"), error (if any)
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

        # Format the log entry with timestamp
        now = datetime.now(system_tz)
        timestamp = now.strftime("%H:%M")  # 24-hour format
        # Convert bullet points to Obsidian format with proper indentation
        # Second-level bullets (+ → 8 spaces + dash)
        normalized_content = re.sub(r'^(\s*)\+(\s+)', r'\1        -\2', content, flags=re.MULTILINE)
        # First-level bullets (* → 4 spaces + dash)
        normalized_content = re.sub(r'^(\s*)\*(\s+)', r'\1    -\2', normalized_content, flags=re.MULTILINE)
        # Preserve multiline content with bullet points, indent continuation lines
        content_lines = normalized_content.strip().split('\n')
        # First line gets the timestamp and Obsidian wiki-link with Linear hyperlink
        header_line = f"[{timestamp}] - [[{parent_name}]] ([link]({url})):"
        if len(content_lines) == 1 and not content_lines[0].strip().startswith(('*', '-', '+')):
            # Single line, no bullets - keep on same line
            log_entry = f"{header_line} {content_lines[0].strip()}"
        else:
            # Multiline or has bullets - content starts on new line at column 0
            indented_content = '\n'.join(line for line in content_lines if line.strip())
            log_entry = f"{header_line}\n{indented_content}"

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

        # Check if this URL already exists in the day section (for update)
        existing_line_index = None
        for i in range(day_section_start, day_section_end):
            if url in lines[i]:
                existing_line_index = i
                break

        if existing_line_index is not None:
            # Update existing entry - need to find and replace the entire block
            # Entry ends at: next timestamp [HH:MM] or section header #
            entry_end = existing_line_index + 1
            for i in range(existing_line_index + 1, day_section_end):
                line = lines[i]
                if LOG_ENTRY_PATTERN.match(line):
                    # Next entry starts here
                    break
                elif line.strip().startswith('#'):
                    # Section header
                    break
                else:
                    # Content line or blank line - part of this entry
                    entry_end = i + 1

            # Remove all lines of the old entry
            del lines[existing_line_index:entry_end]
            # Insert new entry at the same position
            lines.insert(existing_line_index, log_entry)
            # Add blank line after if next line is another entry or section header
            next_line_index = existing_line_index + 1
            if next_line_index < len(lines):
                next_line = lines[next_line_index]
                if LOG_ENTRY_PATTERN.match(next_line) or next_line.strip().startswith('#'):
                    lines.insert(next_line_index, '')
            action = "updated"
        else:
            # Insert new entry - need to find or create the appropriate section
            target_header = _get_section_header(section_type)
            section_order = _get_section_order()

            # Find existing headers in the day section
            header_positions = {}
            for i in range(day_section_start, day_section_end):
                for header in section_order:
                    if lines[i].strip() == header:
                        header_positions[header] = i

            if target_header in header_positions:
                # Header exists - insert after all existing entries
                # Entry boundaries: only timestamp lines [HH:MM] or section headers #
                # Everything else (content, blank lines, user notes) belongs to the section
                header_index = header_positions[target_header]
                insert_index = header_index + 1
                for i in range(header_index + 1, day_section_end):
                    line = lines[i]
                    if line.strip().startswith('#'):
                        # Section header - stop here, insert before it
                        break
                    else:
                        # Any other line (content, blank, notes) - keep going
                        insert_index = i + 1
                # Add blank line before new entry if there isn't one already
                if insert_index > 0 and lines[insert_index - 1].strip() != '':
                    lines.insert(insert_index, '')
                    insert_index += 1
                lines.insert(insert_index, log_entry)
                # Add blank line after if next line is a section header
                next_line_index = insert_index + 1
                if next_line_index < len(lines) and lines[next_line_index].strip().startswith('#'):
                    lines.insert(next_line_index, '')
            else:
                # Header doesn't exist - need to create it in the right position
                # Find where to insert based on section order
                target_order_index = section_order.index(target_header)

                # Find the first existing header that comes after our target
                insert_before_index = None
                for later_header in section_order[target_order_index + 1:]:
                    if later_header in header_positions:
                        insert_before_index = header_positions[later_header]
                        break

                if insert_before_index is not None:
                    # Insert before the next section
                    # Add: header, entry, blank line
                    lines.insert(insert_before_index, '')
                    lines.insert(insert_before_index, log_entry)
                    lines.insert(insert_before_index, target_header)
                    lines.insert(insert_before_index, '')
                else:
                    # No later headers exist - insert before the --- separator or at end of section
                    # Find the last content line before section end
                    insert_pos = day_section_end
                    for i in range(day_section_end - 1, day_section_start, -1):
                        if lines[i].strip() == '---':
                            insert_pos = i
                            break
                        elif lines[i].strip() != '':
                            insert_pos = i + 1
                            break

                    # Insert: blank line, header, entry, blank line
                    new_lines = ['', target_header, log_entry, '']
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
