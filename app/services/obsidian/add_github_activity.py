"""Dropbox helper for writing GitHub activity to Daily Action notes."""

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

GITHUB_ACTIVITY_HEADER = "### GitHub Activity:"
TODOIST_COMPLETED_HEADER = "### Completed Tasks on Todoist:"
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
    formatted_date = now.strftime('%Y-%m-%d')
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
    """Find the index after Daily Review's ending '---'.

    Returns the character index right after the '---' line, or None if not found.
    """
    lines = content.split('\n')
    in_daily_review = False
    char_count = 0

    for i, line in enumerate(lines):
        if 'Daily Review:' in line:
            in_daily_review = True

        if in_daily_review and line.strip() == '---':
            # Found the ending separator
            # Return position after this line (including newline)
            char_count += len(line) + 1  # +1 for newline
            return char_count

        char_count += len(line) + 1  # +1 for newline

    return None


def _find_todoist_section_end(content: str) -> int | None:
    """Find the index after the Todoist section ends.

    Returns the character index right after the last entry in the Todoist section,
    or None if section not found.
    """
    lines = content.split('\n')
    in_todoist_section = False
    last_entry_end = None
    char_count = 0

    for i, line in enumerate(lines):
        if line.strip() == TODOIST_COMPLETED_HEADER:
            in_todoist_section = True
            char_count += len(line) + 1
            last_entry_end = char_count
            continue

        if in_todoist_section:
            if LOG_ENTRY_PATTERN.match(line):
                # This is a log entry, update end position
                char_count += len(line) + 1
                last_entry_end = char_count
            elif line.strip() == '':
                # Empty line within section
                char_count += len(line) + 1
            else:
                # Any other content = end of section
                break
        else:
            char_count += len(line) + 1

    return last_entry_end


def append_github_activity(
    activity_type: str,
    repo_name: str,
    title: str,
    number: int | None = None,
    sha: str | None = None,
    url: str = "",
) -> None:
    """Add GitHub activity to today's GitHub Activity section in Daily Action.

    Creates the section if it doesn't exist.
    Positions section after Todoist section if present, otherwise after Daily Review.

    Args:
        activity_type: "pr" or "commit"
        repo_name: Repository name
        title: PR title or commit message
        number: PR number (for PRs)
        sha: Short commit SHA (for commits)
        url: GitHub URL
    """
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

    dbx = _get_dropbox_client()
    daily_folder = _find_daily_folder(dbx, vault_path)
    daily_action_folder = _find_daily_action_folder(dbx, daily_folder)
    file_path = _get_today_daily_action_path(daily_action_folder)
    content = _get_daily_action_content(dbx, file_path)

    # Format the log entry with timestamp
    system_tz = pytz.timezone(timezone_str)
    now = datetime.now(system_tz)
    timestamp = now.strftime("%H:%M %p")

    # Format entry based on activity type
    if activity_type == "pr" and number is not None:
        entry_text = f"{repo_name}#{number}: {title}"
    elif activity_type == "commit" and sha:
        # Truncate commit message if too long
        short_title = title[:50] + "..." if len(title) > 50 else title
        entry_text = f"{repo_name}: {short_title}"
    else:
        entry_text = f"{repo_name}: {title}"

    # Create markdown link if URL provided
    if url:
        log_entry = f"[{timestamp}] [{entry_text}]({url})"
    else:
        log_entry = f"[{timestamp}] {entry_text}"

    # Parse YAML frontmatter
    yaml_section, main_content = _parse_yaml_frontmatter(content)

    # Check if GitHub section already exists
    if GITHUB_ACTIVITY_HEADER in main_content:
        # Append to existing section
        lines = main_content.split('\n')
        section_found = False
        insert_index = None

        for i, line in enumerate(lines):
            if line.strip() == GITHUB_ACTIVITY_HEADER:
                section_found = True
                insert_index = i + 1
                continue

            if section_found:
                if LOG_ENTRY_PATTERN.match(line):
                    insert_index = i + 1
                elif line.strip() == '':
                    continue
                else:
                    break

        if insert_index is not None:
            lines.insert(insert_index, log_entry)

        updated_main_content = '\n'.join(lines)
    else:
        # Create new section - position after Todoist section
        new_section = f"{GITHUB_ACTIVITY_HEADER}\n{log_entry}\n"

        todoist_section_end = _find_todoist_section_end(main_content)

        if todoist_section_end is not None:
            # Insert after Todoist section
            updated_main_content = (
                main_content[:todoist_section_end] +
                "\n" + new_section +
                main_content[todoist_section_end:].lstrip('\n')
            )
        else:
            # No Todoist section, try after Daily Review
            daily_review_end = _find_daily_review_end(main_content)

            if daily_review_end is not None:
                updated_main_content = (
                    main_content[:daily_review_end] +
                    "\n" + new_section +
                    main_content[daily_review_end:].lstrip('\n')
                )
            else:
                # No Daily Review, insert at top
                updated_main_content = new_section + "\n" + main_content

    # Reassemble and upload
    updated_content = yaml_section + updated_main_content

    dbx.files_upload(
        updated_content.encode('utf-8'),
        file_path,
        mode=dropbox.files.WriteMode.overwrite
    )
