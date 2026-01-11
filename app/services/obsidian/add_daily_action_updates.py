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
    return [INITIATIVE_UPDATES_HEADER, PROJECT_UPDATES_HEADER, TODOIST_COMPLETED_HEADER]


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
        file_content = _get_daily_action_content(dbx, file_path)

        # Format the log entry with timestamp
        system_tz = pytz.timezone(timezone_str)
        now = datetime.now(system_tz)
        timestamp = now.strftime("%H:%M")  # 24-hour format
        # Convert bullet points to Obsidian format with proper indentation
        # Second-level bullets (+ → 8 spaces + dash)
        normalized_content = re.sub(r'^(\s*)\+(\s+)', r'\1        -\2', content, flags=re.MULTILINE)
        # First-level bullets (* → 4 spaces + dash)
        normalized_content = re.sub(r'^(\s*)\*(\s+)', r'\1    -\2', normalized_content, flags=re.MULTILINE)
        # Preserve multiline content with bullet points, indent continuation lines
        content_lines = normalized_content.strip().split('\n')
        # First line gets the timestamp and link
        header_line = f"[{timestamp}] - [{parent_name}]({url}):"
        if len(content_lines) == 1 and not content_lines[0].strip().startswith(('*', '-', '+')):
            # Single line, no bullets - keep on same line
            log_entry = f"{header_line} {content_lines[0].strip()}"
        else:
            # Multiline or has bullets - content starts on new line at column 0
            indented_content = '\n'.join(line for line in content_lines if line.strip())
            log_entry = f"{header_line}\n{indented_content}"

        # Parse YAML frontmatter
        yaml_section, main_content = _parse_yaml_frontmatter(file_content)

        lines = main_content.split('\n')

        # Find Daily Review end line index
        daily_review_end_line = _find_daily_review_end(main_content)
        if daily_review_end_line is None:
            daily_review_end_line = 0  # Start from beginning if no Daily Review

        # Check if this URL already exists in the file (for update)
        existing_line_index = None
        for i, line in enumerate(lines):
            if url in line:
                existing_line_index = i
                break

        if existing_line_index is not None:
            # Update existing entry - need to find and replace the entire block
            # Entry ends at: next timestamp [HH:MM] or section header #
            entry_end = existing_line_index + 1
            for i in range(existing_line_index + 1, len(lines)):
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
            # Add blank line after if next line is another entry and no blank exists
            next_line_index = existing_line_index + 1
            if next_line_index < len(lines):
                next_line = lines[next_line_index]
                if LOG_ENTRY_PATTERN.match(next_line):
                    lines.insert(next_line_index, '')
            action = "updated"
        else:
            # Insert new entry - need to find or create the appropriate section
            target_header = _get_section_header(section_type)
            section_order = _get_section_order()

            # Find existing headers in the content (after Daily Review)
            header_positions = {}
            for i, line in enumerate(lines):
                if i < daily_review_end_line:
                    continue
                for header in section_order:
                    if line.strip() == header:
                        header_positions[header] = i

            if target_header in header_positions:
                # Header exists - insert after all existing entries
                # Entry boundaries: only timestamp lines [HH:MM] or section headers #
                # Everything else (content, blank lines, user notes) belongs to the section
                header_index = header_positions[target_header]
                insert_index = header_index + 1
                for i in range(header_index + 1, len(lines)):
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
                    # Add: blank line, header, entry, blank line
                    lines.insert(insert_before_index, '')
                    lines.insert(insert_before_index, log_entry)
                    lines.insert(insert_before_index, target_header)
                    lines.insert(insert_before_index, '')
                else:
                    # No later headers exist - insert after Daily Review section
                    insert_pos = daily_review_end_line
                    # Insert: blank line, header, entry, blank line
                    new_lines = ['', target_header, log_entry, '']
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
