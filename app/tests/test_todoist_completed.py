import os
import re
import dropbox
from datetime import datetime
import pytz
import redis
import requests
from dotenv import load_dotenv
import logging

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

# --- Timezone Configuration ---
timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")
logger.info(f"Using timezone: {timezone_str}")

# Get Redis configuration from environment variables
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', None)

# Connect to Redis using the environment variables
r = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)

TODOIST_COMPLETED_HEADER = "### Completed Tasks on Todoist:"
LOG_ENTRY_PATTERN = re.compile(r'^\[\d{2}:\d{2}')


def refresh_access_token():
    """Refresh the Dropbox access token using the refresh token."""
    client_id = os.getenv('DROPBOX_ACCESS_KEY')
    client_secret = os.getenv('DROPBOX_ACCESS_SECRET')
    refresh_token = os.getenv('DROPBOX_REFRESH_TOKEN')

    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError("Missing Dropbox credentials in .env file")

    url = 'https://api.dropbox.com/oauth2/token'
    data = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': client_id,
        'client_secret': client_secret
    }

    response = requests.post(url, data=data)

    if response.status_code == 200:
        response_data = response.json()
        access_token = response_data.get('access_token')
        expires_in = response_data.get('expires_in')

        logger.info(f"Refreshed access token (expires in {expires_in} seconds)")

        # Store the access token in Redis with an expiration time
        r.set('DROPBOX_ACCESS_TOKEN', access_token, ex=expires_in)
        return access_token
    else:
        raise EnvironmentError(f"Failed to refresh token: {response.status_code} - {response.content}")


def get_dropbox_access_token():
    """Get the Dropbox access token from Redis, refreshing if needed."""
    access_token = r.get('DROPBOX_ACCESS_TOKEN')
    if not access_token:
        logger.info("No access token in Redis, refreshing...")
        access_token = refresh_access_token()
    return access_token


def find_daily_folder(dbx, vault_path):
    """Find the folder ending with '_Daily' in the vault, with pagination support."""
    result = dbx.files_list_folder(vault_path)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Daily"):
                return entry.path_lower

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    raise FileNotFoundError("Could not find a folder ending with '_Daily' in Dropbox")


def find_daily_action_folder(dbx, daily_folder_path):
    """Find the folder ending with '_Daily-Action' in the daily folder, with pagination support."""
    result = dbx.files_list_folder(daily_folder_path)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Daily-Action"):
                return entry.path_lower

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    raise FileNotFoundError("Could not find a folder ending with '_Daily-Action' in Dropbox")


def get_today_daily_action_path(daily_action_folder_path):
    """Get the file path for today's Daily Action."""
    system_tz = pytz.timezone(timezone_str)
    now = datetime.now(system_tz)
    formatted_date = now.strftime('%Y-%m-%d')
    file_name = f"DA {formatted_date}.md"
    return f"{daily_action_folder_path}/{file_name}"


def get_daily_action_content(dbx, file_path):
    """Fetch Daily Action file content from Dropbox."""
    logger.info(f"Looking for Daily Action file: {file_path}")

    try:
        _, response = dbx.files_download(file_path)
        return response.content.decode('utf-8')
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.DownloadError):
            raise FileNotFoundError(f"Daily Action file not found: {file_path}")
        raise


def parse_yaml_frontmatter(content):
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


def find_daily_review_end(content):
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
            char_count += len(line) + 1
            return char_count

        char_count += len(line) + 1

    return None


def find_todoist_section(content):
    """Find and extract the Todoist Completed Tasks section with its entries."""
    lines = content.split('\n')
    section_lines = []
    in_section = False

    for line in lines:
        if line.strip() == TODOIST_COMPLETED_HEADER:
            in_section = True
            section_lines.append(line)
            continue

        if in_section:
            if line.startswith('#') or line.strip() == '---':
                break
            section_lines.append(line)

    if section_lines:
        return '\n'.join(section_lines).rstrip()
    return None


def add_todoist_entry(dbx, file_path, content, log_text):
    """Add a log entry to the Todoist Completed Tasks section."""
    yaml_section, main_content = parse_yaml_frontmatter(content)

    if TODOIST_COMPLETED_HEADER in main_content:
        # Append to existing section
        lines = main_content.split('\n')
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
            lines.insert(insert_index, log_text)

        updated_main_content = '\n'.join(lines)
    else:
        # Create new section
        new_section = f"{TODOIST_COMPLETED_HEADER}\n{log_text}\n\n"

        daily_review_end = find_daily_review_end(main_content)

        if daily_review_end is not None:
            updated_main_content = (
                main_content[:daily_review_end] +
                "\n" + new_section +
                main_content[daily_review_end:].lstrip('\n')
            )
        else:
            updated_main_content = new_section + main_content

    updated_content = yaml_section + updated_main_content

    dbx.files_upload(
        updated_content.encode('utf-8'),
        file_path,
        mode=dropbox.files.WriteMode.overwrite
    )

    logger.info(f"Added log entry '{log_text[:50]}...' to Todoist Completed Tasks section")
    return updated_content


def main():
    dropbox_vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not dropbox_vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return

    try:
        # Get access token from Redis and initialize Dropbox client
        access_token = get_dropbox_access_token()
        dbx = dropbox.Dropbox(access_token)

        # Find the daily folder
        daily_folder_path = find_daily_folder(dbx, dropbox_vault_path)

        # Find the daily action folder
        daily_action_folder_path = find_daily_action_folder(dbx, daily_folder_path)

        logger.info(f"Found daily action folder: {daily_action_folder_path}")

        # Get today's Daily Action path and content
        file_path = get_today_daily_action_path(daily_action_folder_path)
        content = get_daily_action_content(dbx, file_path)

        # Check for Todoist section
        todoist_section = find_todoist_section(content)

        if todoist_section:
            print("\n" + "=" * 50)
            print("TODOIST COMPLETED TASKS SECTION FOUND")
            print("=" * 50 + "\n")
            print(todoist_section)
        else:
            print("\n" + "=" * 50)
            print("TODOIST COMPLETED TASKS SECTION NOT FOUND")
            print("=" * 50 + "\n")

        # Add test log entry
        system_tz = pytz.timezone(timezone_str)
        now = datetime.now(system_tz)
        timestamp = now.strftime("%H:%M %p")
        test_entry = f"[{timestamp}] Test completed task"

        print(f"\nAdding test log entry: {test_entry}")
        updated_content = add_todoist_entry(dbx, file_path, content, test_entry)

        # Show updated section
        updated_section = find_todoist_section(updated_content)
        print("\n" + "=" * 50)
        print("UPDATED TODOIST COMPLETED TASKS SECTION")
        print("=" * 50 + "\n")
        print(updated_section)

    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
    except EnvironmentError as e:
        logger.error(str(e))
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    main()
