"""Dropbox helper for saving shared links to Obsidian Knowledge Hub."""

import logging
import os
import re
from datetime import datetime, timezone

import dropbox
import pytz
import redis
import requests
from dotenv import load_dotenv

load_dotenv()

# Logging
logger = logging.getLogger(__name__)

# Redis configuration
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', None)
redis_client = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)

# Timezone
timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")


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


def _find_knowledge_hub_path(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find folder ending with '_Knowledge-Hub' in the vault."""
    result = dbx.files_list_folder(vault_path)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Knowledge-Hub"):
                return entry.path_lower

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    raise FileNotFoundError("Could not find '_Knowledge-Hub' folder in Dropbox")


def _sanitize_filename(title: str) -> str:
    """Replace invalid filename characters with underscores."""
    return re.sub(r'[\/:*?"<>|]', '_', title)


def _file_exists(dbx: dropbox.Dropbox, path: str) -> bool:
    """Check if a file already exists in Dropbox."""
    try:
        dbx.files_get_metadata(path)
        return True
    except dropbox.exceptions.ApiError as e:
        if e.error.is_path() and e.error.get_path().is_not_found():
            return False
        raise


def _generate_title_from_url(url: str) -> str:
    """Generate a title from URL if none provided."""
    # Remove protocol
    title = re.sub(r'^https?://', '', url)
    # Remove trailing slashes
    title = title.rstrip('/')
    # Limit length
    if len(title) > 100:
        title = title[:100]
    return title


def add_shared_link(url: str, title: str | None = None) -> dict:
    """Create a new markdown file for a shared link in Knowledge Hub.

    Args:
        url: The URL to save
        title: Optional title for the link. Uses URL if not provided.

    Returns:
        dict with keys:
            - success: bool
            - action: str | None ("created" or "skipped")
            - error: str | None
            - file_path: str | None (relative path within vault)
            - vault_name: str | None (name of the Obsidian vault)
    """
    result = {"success": False, "action": None, "error": None, "file_path": None, "vault_name": None}

    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        result["error"] = "DROPBOX_OBSIDIAN_VAULT_PATH not set"
        return result

    # Extract vault name from path (e.g., "/obsidian/personal" -> "personal")
    vault_name = vault_path.rstrip('/').split('/')[-1]
    result["vault_name"] = vault_name

    try:
        dbx = _get_dropbox_client()
        knowledge_hub_path = _find_knowledge_hub_path(dbx, vault_path)

        # Generate title if not provided
        link_title = title if title else _generate_title_from_url(url)

        # Sanitize filename
        filename = _sanitize_filename(link_title) + '.md'
        file_path = f"{knowledge_hub_path}/{filename}"

        # Calculate relative path within vault for Obsidian URL
        relative_file_path = file_path.replace(vault_path.lower(), '').lstrip('/')
        result["file_path"] = relative_file_path

        # Check if file already exists
        if _file_exists(dbx, file_path):
            logger.info("File already exists, skipping: %s", file_path)
            result["success"] = True
            result["action"] = "skipped"
            return result

        # Get timestamps
        system_tz = pytz.timezone(timezone_str)
        now_local = datetime.now(timezone.utc).astimezone(system_tz)
        now_utc = datetime.now(timezone.utc)

        # Format date for Journal link (e.g., "Jan 19, 2026")
        formatted_local_date = now_local.strftime('%b %-d, %Y')

        # Generate markdown content with YAML frontmatter
        markdown_content = f"""---
Journal:
  - "[[{formatted_local_date}]]"
created time: {now_utc.isoformat()}
modified time: {now_utc.isoformat()}
key words:
People:
URL: {url}
Notes+Ideas:
Experiences:
Tags:
---

## {link_title}

"""

        # Upload to Dropbox
        dbx.files_upload(
            markdown_content.encode('utf-8'),
            file_path,
            mode=dropbox.files.WriteMode.overwrite
        )

        logger.info("Created shared link file: %s", file_path)
        result["success"] = True
        result["action"] = "created"

    except FileNotFoundError as e:
        result["error"] = str(e)
        logger.error("Knowledge Hub folder not found: %s", e)
    except EnvironmentError as e:
        result["error"] = str(e)
        logger.error("Environment error: %s", e)
    except Exception as e:
        result["error"] = str(e)
        logger.error("Unexpected error saving shared link: %s", e)

    return result
