"""Dropbox helper for writing completed Todoist tasks to Daily Action notes."""

import os
import re
from datetime import datetime, timedelta

import dropbox
import pytz
import redis
import requests
from dotenv import load_dotenv

from services.obsidian.utils.date_helpers import get_effective_date
from services.obsidian.utils.dedup_helpers import extract_task_contents_from_section, is_task_duplicate

load_dotenv()

# Redis configuration
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', None)
redis_client = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)

# Timezone
timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")

TODOIST_COMPLETED_HEADER = "### Completed Tasks on Todoist:"
LOG_ENTRY_PATTERN = re.compile(r'^\[\d{2}:\d{2}')

# Related section headers that should come before Todoist
INITIATIVE_UPDATES_HEADER = "### Initiative Updates:"
PROJECT_UPDATES_HEADER = "### Project Updates:"

# Template section boundary (marks end of tracked sections)
TEMPLATE_BOUNDARY = "Vision Objective 1:"


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


def _get_today_daily_action_path(daily_action_folder_path: str, target_dt: datetime | None = None) -> str:
    """Get file path for the target date's Daily Action.

    Uses a 3-hour buffer: tasks completed between midnight and 3am
    are logged to the previous day's file.

    Args:
        daily_action_folder_path: Dropbox path to the Daily Action folder
        target_dt: Optional timezone-aware datetime to use instead of now
    """
    system_tz = pytz.timezone(timezone_str)
    if target_dt is not None:
        now = target_dt.astimezone(system_tz)
    else:
        now = datetime.now(system_tz)
    effective_date = get_effective_date(now)
    formatted_date = effective_date.strftime('%Y-%m-%d')
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


def _find_daily_review_end_line(content: str) -> int | None:
    """Find the line index after Daily Review's ending '---'.

    Returns the line index right after the '---' line, or None if not found.
    """
    lines = content.split('\n')
    in_daily_review = False

    for i, line in enumerate(lines):
        if 'Daily Review:' in line:
            in_daily_review = True

        if in_daily_review and line.strip() == '---':
            # Found the ending separator, return the next line index
            return i + 1

    return None


def _find_todoist_insert_position(lines: list[str], daily_review_end_line: int) -> int:
    """Find the correct line index to insert the Todoist section.

    The Todoist section should be inserted:
    1. After Initiative Updates section (if exists)
    2. After Project Updates section (if exists)
    3. Before Template Boundary (Vision Objective 1:)
    4. After Daily Review if no other sections exist

    Returns the line index where Todoist section should be inserted.
    """
    # Find positions of relevant sections
    initiative_end = None
    project_end = None
    template_boundary_line = None

    in_initiative = False
    in_project = False

    for i, line in enumerate(lines):
        if i < daily_review_end_line:
            continue

        stripped = line.strip()

        # Check for section headers
        if stripped == INITIATIVE_UPDATES_HEADER:
            in_initiative = True
            in_project = False
            continue
        elif stripped == PROJECT_UPDATES_HEADER:
            in_project = True
            in_initiative = False
            # Mark end of initiative section
            if initiative_end is None and any(INITIATIVE_UPDATES_HEADER in lines[j] for j in range(daily_review_end_line, i)):
                initiative_end = i
            continue
        elif stripped.startswith('#') or stripped == '---':
            # Another section header or separator - end current section
            if in_initiative:
                initiative_end = i
                in_initiative = False
            if in_project:
                project_end = i
                in_project = False

        # Check for template boundary
        if stripped == TEMPLATE_BOUNDARY:
            template_boundary_line = i
            if in_initiative:
                initiative_end = i
            if in_project:
                project_end = i
            break

    # If we're still in a section at end of file
    if in_initiative and initiative_end is None:
        initiative_end = len(lines)
    if in_project and project_end is None:
        project_end = len(lines)

    # Determine insert position based on what exists
    # Priority: after Project Updates > after Initiative Updates > before template > after daily review
    if project_end is not None:
        return project_end
    elif initiative_end is not None:
        return initiative_end
    elif template_boundary_line is not None:
        return template_boundary_line
    else:
        return daily_review_end_line


def append_todoist_completed(task_content: str, target_dt: datetime | None = None) -> None:
    """Add a completed task to the Todoist section in Daily Action.

    Creates the section if it doesn't exist.
    Positions section after Initiative/Project Updates if present,
    before Vision Objectives, otherwise after Daily Review.

    Args:
        task_content: The task text to add
        target_dt: Optional timezone-aware datetime for file routing and timestamp.
                   When None, uses datetime.now() (real-time webhook behavior).
    """
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

    dbx = _get_dropbox_client()
    daily_folder = _find_daily_folder(dbx, vault_path)
    daily_action_folder = _find_daily_action_folder(dbx, daily_folder)
    file_path = _get_today_daily_action_path(daily_action_folder, target_dt)
    content = _get_daily_action_content(dbx, file_path)

    # Dedup check
    existing_tasks = extract_task_contents_from_section(content, TODOIST_COMPLETED_HEADER)
    if is_task_duplicate(task_content, existing_tasks):
        return

    # Format the log entry with timestamp
    system_tz = pytz.timezone(timezone_str)
    if target_dt is not None:
        now = target_dt.astimezone(system_tz)
    else:
        now = datetime.now(system_tz)
    timestamp = now.strftime("%H:%M %p")
    log_entry = f"[{timestamp}] {task_content}"

    # Parse YAML frontmatter
    yaml_section, main_content = _parse_yaml_frontmatter(content)
    lines = main_content.split('\n')

    # Find Daily Review end line for positioning reference
    daily_review_end_line = _find_daily_review_end_line(main_content)
    if daily_review_end_line is None:
        daily_review_end_line = 0

    # Check if Todoist section already exists
    if TODOIST_COMPLETED_HEADER in main_content:
        # Append to existing section
        section_found = False
        insert_index = None

        # First pass: find the insert position
        for i, line in enumerate(lines):
            if line.strip() == TODOIST_COMPLETED_HEADER:
                section_found = True
                insert_index = i + 1
                continue

            if section_found:
                if LOG_ENTRY_PATTERN.match(line):
                    # This is a log entry, update insert position
                    insert_index = i + 1
                elif line.strip() == '':
                    # Empty line, keep looking
                    continue
                else:
                    # Any other content (heading, text, ---) = end of section
                    break

        # Insert at the found position
        if insert_index is not None:
            lines.insert(insert_index, log_entry)
    else:
        # Create new section - find correct position
        insert_pos = _find_todoist_insert_position(lines, daily_review_end_line)

        # Insert: blank line (if needed), header, entry, blank line
        new_lines = []
        if insert_pos > 0 and lines[insert_pos - 1].strip() != '':
            new_lines.append('')
        new_lines.append(TODOIST_COMPLETED_HEADER)
        new_lines.append(log_entry)
        # Add trailing blank line if next content isn't a blank line
        if insert_pos < len(lines) and lines[insert_pos].strip() != '':
            new_lines.append('')

        for j, new_line in enumerate(new_lines):
            lines.insert(insert_pos + j, new_line)

    updated_main_content = '\n'.join(lines)

    # Reassemble and upload
    updated_content = yaml_section + updated_main_content

    dbx.files_upload(
        updated_content.encode('utf-8'),
        file_path,
        mode=dropbox.files.WriteMode.overwrite
    )
