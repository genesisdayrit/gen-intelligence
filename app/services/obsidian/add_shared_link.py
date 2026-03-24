"""Dropbox helper for saving shared links to Obsidian Knowledge Hub."""

import json
import logging
import os
import re
from datetime import datetime, timezone

import dropbox
import pytz
import redis
import requests
from dotenv import load_dotenv
from openai import OpenAI

from .web_content_extractor import fetch_web_content

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

ARTICLE_PEOPLE_EXTRACTION_PROMPT = """Given the title, author, and opening text of a web article, identify the author and any primary people or entities mentioned. Return ONLY a JSON array of names.

Include:
- The article author (if identifiable)
- People who are a primary subject of or prominently featured in the article
- Organizations or entities that are a primary focus

Do NOT include:
- People or entities only mentioned in passing
- Generic references (e.g., "researchers", "the company")

IMPORTANT: Always use full names (first and last name) for people. If you cannot determine someone's full name, omit them. The only exception is well-known single-word identifiers, brands, or aliases (e.g., "Banksy", "NASA", "OpenAI").

If there are no clearly identifiable people or entities, return an empty array: []

Examples:
- An article by Paul Graham about startups mentioning Sam Altman: ["Paul Graham", "Sam Altman"]
- A NYT profile of Jensen Huang: ["Jensen Huang"]
- A blog post by an unknown author with no notable people: []

Return ONLY the JSON array, no other text."""

# Max chars of body text to send for people extraction (~10k tokens)
_PEOPLE_EXTRACTION_BODY_LIMIT = 4000


def _extract_people_from_article(
    title: str | None, author: str | None, body_text: str | None
) -> list[str]:
    """Extract author and key people/entities from an article using gpt-4o.

    Returns a list of names, or empty list if none identified or API unavailable.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return []

    parts = []
    if title:
        parts.append(f"Title: {title}")
    if author:
        parts.append(f"Author: {author}")
    if body_text:
        truncated = body_text[:_PEOPLE_EXTRACTION_BODY_LIMIT]
        parts.append(f"Article text:\n{truncated}")

    if not parts:
        return []

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": ARTICLE_PEOPLE_EXTRACTION_PROMPT},
                {"role": "user", "content": "\n\n".join(parts)},
            ],
            temperature=0.0,
        )
        content = response.choices[0].message.content
        if content:
            names = json.loads(content.strip())
            if isinstance(names, list):
                return [n for n in names if isinstance(n, str) and n.strip()]
        return []
    except Exception as e:
        logger.warning("Article people extraction failed: %s", e)
        return []


def _sanitize_obsidian_link(name: str) -> str:
    """Remove characters that are illegal in Obsidian [[]] links."""
    return re.sub(r'[\[\]|#^\\\\/]', '', name).strip()


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


def get_predicted_link_path(url: str, title: str | None = None) -> dict:
    """Get the predicted file path for a shared link without creating the file.

    Args:
        url: The URL to save
        title: Optional title for the link. Uses URL if not provided.

    Returns:
        dict with keys:
            - vault_name: str | None
            - file_path: str | None (relative path within vault)
    """
    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        return {"vault_name": None, "file_path": None}

    vault_name = vault_path.rstrip('/').split('/')[-1]
    knowledge_hub_folder = os.getenv('OBSIDIAN_KNOWLEDGE_HUB_FOLDER', '_Knowledge-Hub')
    link_title = title if title else _generate_title_from_url(url)
    filename = _sanitize_filename(link_title) + '.md'
    file_path = f"{knowledge_hub_folder}/{filename}"

    return {"vault_name": vault_name, "file_path": file_path}


def add_shared_link(url: str, title: str | None = None) -> dict:
    """Create a new markdown file for a shared link in Knowledge Hub.

    Args:
        url: The URL to save
        title: Optional title for the link. Uses extracted or URL-derived title if not provided.

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
        # Fetch web content (title, author, body text)
        web_content = fetch_web_content(url)
        extracted_title = web_content.get("title")
        author = web_content.get("author")
        body_text = web_content.get("body_text")

        dbx = _get_dropbox_client()
        knowledge_hub_path = _find_knowledge_hub_path(dbx, vault_path)

        # Title fallback chain: user-provided -> extracted -> URL-derived
        if title:
            link_title = title
        elif extracted_title:
            link_title = extracted_title
        else:
            link_title = _generate_title_from_url(url)

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

        # Build body section
        body_section = ""
        if body_text:
            body_section = f"\n{body_text}\n"

        # Build author field (empty string if not available)
        author_value = author if author else ""

        # Extract people/entities using AI
        people = _extract_people_from_article(link_title, author, body_text)
        if people:
            people_lines = "\n".join(
                f'  - "[[{_sanitize_obsidian_link(name)}]]"' for name in people
            )
            people_yaml = f"\n{people_lines}"
        else:
            people_yaml = ""

        # Generate markdown content with YAML frontmatter
        markdown_content = f"""---
Journal:
  - "[[{formatted_local_date}]]"
created time: {now_utc.isoformat()}
modified time: {now_utc.isoformat()}
key words:
People:{people_yaml}
URL: {url}
author: {author_value}
Notes+Ideas:
Experiences:
Tags:
---

## {link_title}
{body_section}
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
