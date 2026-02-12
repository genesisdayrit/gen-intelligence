"""Dropbox helper for writing Linear Issues Touched to Daily Action notes."""

import os
import re
from datetime import datetime

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

# Section headers (level 3 for Daily Action)
INITIATIVE_UPDATES_HEADER = "### Initiative Updates:"
PROJECT_UPDATES_HEADER = "### Project Updates:"
TODOIST_COMPLETED_HEADER = "### Completed Tasks on Todoist:"
ISSUES_TOUCHED_HEADER = "### Linear Issues Touched:"

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


def _get_today_daily_action_path(daily_action_folder_path: str) -> str:
    """Get file path for today's Daily Action.

    Uses a 3-hour buffer: updates between midnight and 3am
    are logged to the previous day's file.
    """
    system_tz = pytz.timezone(timezone_str)
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


def _find_daily_review_end(content: str) -> int | None:
    """Find the line index after Daily Review's ending '---'.

    Returns the line index right after the '---' line, or None if not found.
    """
    lines = content.split('\n')
    in_daily_review = False

    for i, line in enumerate(lines):
        if 'Daily Review:' in line:
            in_daily_review = True

        if in_daily_review and line.strip() == '---':
            return i + 1

    return None


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


def _find_issues_touched_insert_position(lines: list[str], daily_review_end_line: int) -> int:
    """Find the correct line index to insert the Linear Issues Touched section.

    The section should be inserted:
    1. After Todoist Completed section (if exists)
    2. After Project Updates section (if exists and no Todoist)
    3. After Initiative Updates section (if exists and no others)
    4. Before Template Boundary (Vision Objective 1:)
    5. After Daily Review if no other sections exist
    """
    todoist_end = None
    project_end = None
    initiative_end = None
    template_boundary_line = None

    in_todoist = False
    in_project = False
    in_initiative = False

    for i, line in enumerate(lines):
        if i < daily_review_end_line:
            continue

        stripped = line.strip()

        if stripped == TODOIST_COMPLETED_HEADER:
            in_todoist = True
            in_project = False
            in_initiative = False
            continue
        elif stripped == PROJECT_UPDATES_HEADER:
            in_project = True
            in_todoist = False
            in_initiative = False
            if initiative_end is None and any(INITIATIVE_UPDATES_HEADER in lines[j] for j in range(daily_review_end_line, i)):
                initiative_end = i
            continue
        elif stripped == INITIATIVE_UPDATES_HEADER:
            in_initiative = True
            in_todoist = False
            in_project = False
            continue
        elif stripped.startswith('#') or stripped == '---':
            if in_todoist:
                todoist_end = i
                in_todoist = False
            if in_project:
                project_end = i
                in_project = False
            if in_initiative:
                initiative_end = i
                in_initiative = False

        if stripped == TEMPLATE_BOUNDARY:
            template_boundary_line = i
            if in_todoist:
                todoist_end = i
            if in_project:
                project_end = i
            if in_initiative:
                initiative_end = i
            break

    # If still in a section at end of file
    if in_todoist and todoist_end is None:
        todoist_end = len(lines)
    if in_project and project_end is None:
        project_end = len(lines)
    if in_initiative and initiative_end is None:
        initiative_end = len(lines)

    # Priority: after Todoist > after Project > after Initiative > before template > after daily review
    if todoist_end is not None:
        return todoist_end
    elif project_end is not None:
        return project_end
    elif initiative_end is not None:
        return initiative_end
    elif template_boundary_line is not None:
        return template_boundary_line
    else:
        return daily_review_end_line


def upsert_daily_action_issue_touched(
    issue_identifier: str,
    project_name: str,
    issue_title: str,
    status_name: str,
    issue_url: str,
    status_changed: bool,
) -> dict:
    """Upsert a Linear issue entry to the Linear Issues Touched section in today's Daily Action.

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

        # Find Daily Action file
        daily_folder = _find_daily_folder(dbx, vault_path)
        daily_action_folder = _find_daily_action_folder(dbx, daily_folder)
        file_path = _get_today_daily_action_path(daily_action_folder)
        file_content = _get_daily_action_content(dbx, file_path)

        # Format the entry
        entry_line = _format_issue_entry(issue_identifier, project_name, issue_title, status_name, issue_url)

        # Parse YAML frontmatter
        yaml_section, main_content = _parse_yaml_frontmatter(file_content)
        lines = main_content.split('\n')

        # Find Daily Review end line index
        daily_review_end_line = _find_daily_review_end(main_content)
        if daily_review_end_line is None:
            daily_review_end_line = 0

        # Check if this issue identifier already exists in the file
        # Pattern: line starts with the identifier followed by a space
        identifier_pattern = re.compile(rf'^{re.escape(issue_identifier)}\s')
        existing_line_index = None
        in_issues_section = False

        for i, line in enumerate(lines):
            if line.strip() == ISSUES_TOUCHED_HEADER:
                in_issues_section = True
                continue
            if in_issues_section:
                if line.strip().startswith('#') or line.strip() == '---' or line.strip() == TEMPLATE_BOUNDARY:
                    break
                if identifier_pattern.match(line):
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
            if ISSUES_TOUCHED_HEADER in main_content:
                # Section exists - append after existing entries
                section_found = False
                insert_index = None

                for i, line in enumerate(lines):
                    if line.strip() == ISSUES_TOUCHED_HEADER:
                        section_found = True
                        insert_index = i + 1
                        continue

                    if section_found:
                        if line.strip() == '':
                            continue
                        elif line.strip().startswith('#') or line.strip() == '---' or line.strip() == TEMPLATE_BOUNDARY:
                            break
                        else:
                            # Content line - update insert position
                            insert_index = i + 1

                if insert_index is not None:
                    lines.insert(insert_index, entry_line)
            else:
                # Section doesn't exist - create it
                insert_pos = _find_issues_touched_insert_position(lines, daily_review_end_line)

                new_lines = []
                if insert_pos > 0 and lines[insert_pos - 1].strip() != '':
                    new_lines.append('')
                new_lines.append(ISSUES_TOUCHED_HEADER)
                new_lines.append(entry_line)
                if insert_pos < len(lines) and lines[insert_pos].strip() != '':
                    new_lines.append('')

                for j, new_line in enumerate(new_lines):
                    lines.insert(insert_pos + j, new_line)

            action = "inserted"

        updated_main_content = '\n'.join(lines)
        updated_content = yaml_section + updated_main_content

        # Upload updated content
        dbx.files_upload(
            updated_content.encode('utf-8'),
            file_path,
            mode=dropbox.files.WriteMode.overwrite
        )

        return {"success": True, "action": action}

    except Exception as e:
        return {"success": False, "action": None, "error": str(e)}
