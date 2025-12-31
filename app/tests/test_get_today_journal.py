import os
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


def get_today_journal(dbx, journal_folder_path):
    """Fetch today's journal file content from Dropbox."""
    system_tz = pytz.timezone(timezone_str)
    now = datetime.now(system_tz)

    # Format: "Dec 30, 2024.md"
    formatted_date = f"{now.strftime('%b')} {now.day}, {now.strftime('%Y')}"
    file_name = f"{formatted_date}.md"
    file_path = f"{journal_folder_path}/{file_name}"

    logger.info(f"Looking for journal file: {file_path}")

    try:
        _, response = dbx.files_download(file_path)
        return response.content.decode('utf-8')
    except dropbox.exceptions.ApiError as e:
        if isinstance(e.error, dropbox.files.DownloadError):
            raise FileNotFoundError(f"Journal file not found: {file_name}")
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

        # Find the daily folder
        daily_folder_path = find_daily_folder(dbx, dropbox_vault_path)
        journal_folder_path = f"{daily_folder_path}/_Journal"

        logger.info(f"Found journal folder: {journal_folder_path}")

        # Get today's journal content
        content = get_today_journal(dbx, journal_folder_path)

        print("\n" + "=" * 50)
        print("TODAY'S JOURNAL")
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
