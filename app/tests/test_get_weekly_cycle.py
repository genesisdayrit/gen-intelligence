import os
import dropbox
from datetime import datetime, timedelta
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


def find_cycles_folder(dbx, vault_path):
    """Find the folder ending with '_Cycles' in the vault, with pagination support."""
    result = dbx.files_list_folder(vault_path)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Cycles"):
                return entry.path_lower

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    raise FileNotFoundError("Could not find a folder ending with '_Cycles' in Dropbox")


def get_current_week_bounds(tz):
    """Calculate the Wednesday-Tuesday bounds for the current week's cycle."""
    now = datetime.now(tz)

    # Wednesday is weekday 2 (Monday=0, Tuesday=1, Wednesday=2, ...)
    # Calculate days since the most recent Wednesday (including today if it's Wednesday)
    days_since_wednesday = (now.weekday() - 2) % 7

    cycle_start = now - timedelta(days=days_since_wednesday)
    cycle_end = cycle_start + timedelta(days=6)  # Tuesday

    return cycle_start, cycle_end


def format_date_range(cycle_start, cycle_end):
    """Format the date range string to match file naming convention.

    Format: (Jan. 07 - Jan. 13, 2026)
    """
    # Format: "Jan. 07" for start, "Jan. 13, 2026" for end
    start_str = f"{cycle_start.strftime('%b')}. {cycle_start.strftime('%d')}"
    end_str = f"{cycle_end.strftime('%b')}. {cycle_end.strftime('%d')}, {cycle_end.strftime('%Y')}"

    return f"({start_str} - {end_str})"


def find_weekly_cycle_file(dbx, weekly_cycles_folder_path, date_range):
    """Find the weekly cycle file matching the given date range."""
    result = dbx.files_list_folder(weekly_cycles_folder_path)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FileMetadata) and date_range in entry.name:
                return entry.path_lower, entry.name

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    raise FileNotFoundError(f"Could not find weekly cycle file for date range: {date_range}")


def get_weekly_cycle_content(dbx, file_path):
    """Download and return the content of the weekly cycle file."""
    try:
        _, response = dbx.files_download(file_path)
        return response.content.decode('utf-8')
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.DownloadError):
            raise FileNotFoundError(f"Weekly cycle file not found: {file_path}")
        raise


def main():
    dropbox_vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not dropbox_vault_path:
        logger.error("DROPBOX_OBSIDIAN_VAULT_PATH environment variable not set")
        return

    try:
        # Get access token from Redis and initialize Dropbox client
        access_token = get_dropbox_access_token()
        dbx = dropbox.Dropbox(access_token)

        # Find the cycles folder
        cycles_folder_path = find_cycles_folder(dbx, dropbox_vault_path)
        weekly_cycles_folder_path = f"{cycles_folder_path}/_Weekly-Cycles"

        logger.info(f"Found weekly cycles folder: {weekly_cycles_folder_path}")

        # Verify the _Weekly-Cycles folder exists
        try:
            dbx.files_get_metadata(weekly_cycles_folder_path)
        except dropbox.exceptions.ApiError as e:
            if isinstance(e.error, dropbox.files.GetMetadataError):
                raise FileNotFoundError("'_Weekly-Cycles' subfolder not found")
            raise

        # Calculate current week's bounds
        system_tz = pytz.timezone(timezone_str)
        cycle_start, cycle_end = get_current_week_bounds(system_tz)
        date_range = format_date_range(cycle_start, cycle_end)

        logger.info(f"Looking for weekly cycle with date range: {date_range}")
        logger.info(f"Cycle period: {cycle_start.strftime('%A, %b %d')} to {cycle_end.strftime('%A, %b %d, %Y')}")

        # Find and download the weekly cycle file
        file_path, file_name = find_weekly_cycle_file(dbx, weekly_cycles_folder_path, date_range)
        logger.info(f"Found weekly cycle file: {file_name}")

        content = get_weekly_cycle_content(dbx, file_path)

        print("\n" + "=" * 50)
        print("THIS WEEK'S CYCLE")
        print("=" * 50 + "\n")
        print(content)

    except FileNotFoundError as e:
        logger.error(f"Error: {e}")
    except EnvironmentError as e:
        logger.error(str(e))
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    main()
