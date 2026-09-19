"""Dropbox helper for writing Linear Initiative/Project Updates to Daily Action notes."""

import os
import re
from datetime import datetime

import dropbox
import pytz
import redis
import requests
from dotenv import load_dotenv

from services.obsidian.utils.date_helpers import get_effective_date
from services.obsidian.utils.dropbox_rev_safe import update_with_retry
from services.obsidian.utils.template_boundary import is_template_boundary

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
MANUS_TASKS_HEADER = "### Manus Tasks:"

# Template section boundary detection lives in
# `services.obsidian.utils.template_boundary` so any change to the user's
# template label only needs to be updated in one place.

# Patterns
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
            # Found the ending separator, return the next line index
            return i + 1

    return None


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
    return [INITIATIVE_UPDATES_HEADER, PROJECT_UPDATES_HEADER, TODOIST_COMPLETED_HEADER, ISSUES_TOUCHED_HEADER, MANUS_TASKS_HEADER]


def _is_section_header(line: str) -> bool:
    """Check if a line is a known section header."""
    return line.strip() in _get_section_order()


def upsert_daily_action_update(section_type: str, url: str, parent_name: str, content: str) -> dict:
    """Upsert an initiative or project update to today's Daily Action note.

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

        # Find Daily Action file
        daily_folder = _find_daily_folder(dbx, vault_path)
        daily_action_folder = _find_daily_action_folder(dbx, daily_folder)
        file_path = _get_today_daily_action_path(daily_action_folder)

        # Format the log entry with timestamp
        system_tz = pytz.timezone(timezone_str)
        now = datetime.now(system_tz)
        timestamp = now.strftime("%H:%M")  # 24-hour format
        # Convert bullet points to Obsidian format (preserve existing indentation)
        # Second-level bullets (+ → preserve indent + dash)
        normalized_content = re.sub(r'^(\s*)\+(\s+)', r'\1-\2', content, flags=re.MULTILINE)
        # First-level bullets (* → preserve indent + dash)
        normalized_content = re.sub(r'^(\s*)\*(\s+)', r'\1-\2', normalized_content, flags=re.MULTILINE)
        # Sanitize --- separators from content to prevent boundary detection issues
        normalized_content = re.sub(r'^---$', '***', normalized_content, flags=re.MULTILINE)
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

        def apply_update(file_content: str):
            yaml_section, main_content = _parse_yaml_frontmatter(file_content)
            lines = main_content.split('\n')

            daily_review_end_line = _find_daily_review_end(main_content)
            if daily_review_end_line is None:
                daily_review_end_line = 0

            existing_line_index = None
            for i, line in enumerate(lines):
                if url in line:
                    existing_line_index = i
                    break

            if existing_line_index is not None:
                entry_end = existing_line_index + 1
                for i in range(existing_line_index + 1, len(lines)):
                    line = lines[i]
                    if LOG_ENTRY_PATTERN.match(line):
                        break
                    elif _is_section_header(line):
                        break
                    elif is_template_boundary(line):
                        break
                    else:
                        entry_end = i + 1

                del lines[existing_line_index:entry_end]
                lines.insert(existing_line_index, log_entry)
                next_line_index = existing_line_index + 1
                if next_line_index < len(lines):
                    next_line = lines[next_line_index]
                    if LOG_ENTRY_PATTERN.match(next_line) or _is_section_header(next_line) or is_template_boundary(next_line):
                        lines.insert(next_line_index, '')
                action = "updated"
            else:
                target_header = _get_section_header(section_type)
                section_order = _get_section_order()

                header_positions = {}
                for i, line in enumerate(lines):
                    if i < daily_review_end_line:
                        continue
                    for header in section_order:
                        if line.strip() == header:
                            header_positions[header] = i

                if target_header in header_positions:
                    header_index = header_positions[target_header]
                    insert_index = header_index + 1
                    for i in range(header_index + 1, len(lines)):
                        line = lines[i]
                        if _is_section_header(line):
                            break
                        elif is_template_boundary(line):
                            break
                        else:
                            insert_index = i + 1
                    if insert_index > 0 and lines[insert_index - 1].strip() != '':
                        lines.insert(insert_index, '')
                        insert_index += 1
                    lines.insert(insert_index, log_entry)
                    next_line_index = insert_index + 1
                    if next_line_index < len(lines):
                        if _is_section_header(lines[next_line_index]) or is_template_boundary(lines[next_line_index]):
                            lines.insert(next_line_index, '')
                else:
                    target_order_index = section_order.index(target_header)

                    insert_before_index = None
                    for later_header in section_order[target_order_index + 1:]:
                        if later_header in header_positions:
                            insert_before_index = header_positions[later_header]
                            break

                    if insert_before_index is not None:
                        lines.insert(insert_before_index, '')
                        lines.insert(insert_before_index, log_entry)
                        lines.insert(insert_before_index, target_header)
                        lines.insert(insert_before_index, '')
                    else:
                        insert_pos = daily_review_end_line
                        new_lines = ['', target_header, log_entry, '']
                        for j, new_line in enumerate(new_lines):
                            lines.insert(insert_pos + j, new_line)

                action = "inserted"

            return yaml_section + '\n'.join(lines), action

        status, action, _updated = update_with_retry(
            dbx,
            file_path,
            apply_update,
            defer={
                "source": "daily_action",
                "kind": "update",
                "payload_ref": url,
                "target": file_path,
                "payload": {
                    "section_type": section_type,
                    "url": url,
                    "parent_name": parent_name,
                    "content": content,
                },
            },
        )
        if status == "missing":
            return {"success": False, "action": None, "error": f"File not found: {file_path}"}
        if status == "error":
            return {"success": False, "action": None, "error": f"No Dropbox rev on download for {file_path}"}
        if status == "deferred":
            return {"success": True, "action": "deferred"}
        return {"success": True, "action": action}

    except Exception as e:
        return {"success": False, "action": None, "error": str(e)}
