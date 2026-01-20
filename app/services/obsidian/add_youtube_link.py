"""Dropbox helper for saving YouTube links to Obsidian Knowledge Hub."""

import logging
import os
import re
from datetime import datetime, timezone

import dropbox
import httpx
import pytz

from .add_shared_link import (
    _get_dropbox_client,
    _find_knowledge_hub_path,
    _sanitize_filename,
    _file_exists,
)

logger = logging.getLogger(__name__)

# YouTube URL patterns - each captures the 11-character video ID
YOUTUBE_PATTERNS = [
    r'^https?://(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
    r'^https?://youtu\.be/([a-zA-Z0-9_-]{11})',
    r'^https?://(?:www\.)?youtube\.com/shorts/([a-zA-Z0-9_-]{11})',
    r'^https?://m\.youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
    r'^https?://(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})',
]

YOUTUBE_OEMBED_URL = "https://www.youtube.com/oembed"


def is_valid_youtube_url(url: str) -> bool:
    """Check if URL is a valid YouTube URL."""
    for pattern in YOUTUBE_PATTERNS:
        if re.match(pattern, url):
            return True
    return False


def fetch_youtube_metadata(url: str) -> dict:
    """Fetch video metadata from YouTube oEmbed API and page.

    Returns:
        dict with keys: title, author_name, description (may be None if fetch fails)
    """
    result = {"title": None, "author_name": None, "description": None}

    try:
        with httpx.Client(timeout=10.0) as client:
            # Fetch from oEmbed API for title and author
            response = client.get(
                YOUTUBE_OEMBED_URL,
                params={"url": url, "format": "json"},
            )

            if response.status_code == 200:
                data = response.json()
                result["title"] = data.get("title")
                result["author_name"] = data.get("author_name")
            else:
                logger.warning(
                    "YouTube oEmbed returned %s for %s",
                    response.status_code,
                    url[:100],
                )

            # Fetch description from the YouTube page
            result["description"] = _fetch_youtube_description(client, url)

    except httpx.RequestError as e:
        logger.warning("Failed to fetch YouTube metadata: %s", e)
    except Exception as e:
        logger.warning("Unexpected error fetching YouTube metadata: %s", e)

    return result


def _fetch_youtube_description(client: httpx.Client, url: str) -> str | None:
    """Fetch video description from the YouTube page.

    Extracts description from the page's embedded JSON data.
    """
    try:
        response = client.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        )

        if response.status_code != 200:
            logger.warning("YouTube page returned %s for %s", response.status_code, url[:100])
            return None

        html = response.text

        # Try to extract description from ytInitialPlayerResponse JSON
        pattern = r'var ytInitialPlayerResponse\s*=\s*(\{.+?\});'
        match = re.search(pattern, html)

        if match:
            import json
            try:
                player_response = json.loads(match.group(1))
                description = (
                    player_response.get("videoDetails", {})
                    .get("shortDescription")
                )
                return description
            except json.JSONDecodeError:
                pass

        # Fallback: try meta description tag
        meta_pattern = r'<meta\s+name="description"\s+content="([^"]*)"'
        meta_match = re.search(meta_pattern, html)
        if meta_match:
            return meta_match.group(1)

        return None

    except Exception as e:
        logger.warning("Failed to fetch YouTube description: %s", e)
        return None


def add_youtube_link(url: str) -> dict:
    """Create a new markdown file for a YouTube video in Knowledge Hub.

    Args:
        url: The YouTube URL to save

    Returns:
        dict with keys:
            - success: bool
            - action: str | None ("created" or "skipped")
            - error: str | None
    """
    result = {"success": False, "action": None, "error": None}

    vault_path = os.getenv('DROPBOX_OBSIDIAN_VAULT_PATH')
    if not vault_path:
        result["error"] = "DROPBOX_OBSIDIAN_VAULT_PATH not set"
        return result

    timezone_str = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")

    try:
        # Fetch metadata from YouTube oEmbed
        metadata = fetch_youtube_metadata(url)
        video_title = metadata["title"] or url  # Fallback to URL if title unavailable
        description = metadata["description"]

        dbx = _get_dropbox_client()
        knowledge_hub_path = _find_knowledge_hub_path(dbx, vault_path)

        # Sanitize filename and limit length
        sanitized_title = _sanitize_filename(video_title)
        if len(sanitized_title) > 100:
            sanitized_title = sanitized_title[:100]
        filename = sanitized_title + '.md'
        file_path = f"{knowledge_hub_path}/{filename}"

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

        # Build description section
        description_section = ""
        if description:
            description_section = f"\n{description}\n"

        # Generate markdown content with YAML frontmatter
        markdown_content = f"""---
Journal:
  - "[[{formatted_local_date}]]"
created time: {now_utc.isoformat()}
modified time: {now_utc.isoformat()}
key words:
URL: {url}
Notes+Ideas:
Experiences:
Tags:
  - youtube
---

## {video_title}
{description_section}
"""

        # Upload to Dropbox
        dbx.files_upload(
            markdown_content.encode('utf-8'),
            file_path,
            mode=dropbox.files.WriteMode.overwrite
        )

        logger.info("Created YouTube link file: %s", file_path)
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
        logger.error("Unexpected error saving YouTube link: %s", e)

    return result
