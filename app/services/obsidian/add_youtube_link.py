"""Dropbox helper for saving YouTube links to Obsidian Knowledge Hub."""

import logging
import math
import os
import re
from datetime import datetime, timezone

import dropbox
import httpx
import pytz
from openai import OpenAI

from .add_shared_link import (
    _get_dropbox_client,
    _find_knowledge_hub_path,
    _sanitize_filename,
    _file_exists,
)

logger = logging.getLogger(__name__)

# YouTube URL patterns - each captures the video/playlist/channel ID
YOUTUBE_PATTERNS = [
    # Videos
    r'^https?://(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
    r'^https?://youtu\.be/([a-zA-Z0-9_-]{11})',
    r'^https?://(?:www\.)?youtube\.com/shorts/([a-zA-Z0-9_-]{11})',
    r'^https?://m\.youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
    r'^https?://(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})',
    r'^https?://(?:www\.)?youtube\.com/live/([a-zA-Z0-9_-]{11})',  # Live streams
    # Playlists
    r'^https?://(?:www\.)?youtube\.com/playlist\?list=([a-zA-Z0-9_-]+)',
    # Channels (oEmbed doesn't work - need page scraping)
    r'^https?://(?:www\.)?youtube\.com/@([a-zA-Z0-9_.-]+)',  # @username
    r'^https?://(?:www\.)?youtube\.com/channel/([a-zA-Z0-9_-]+)',  # channel/ID
    r'^https?://(?:www\.)?youtube\.com/c/([a-zA-Z0-9_.-]+)',  # c/customname (legacy)
    r'^https?://(?:www\.)?youtube\.com/user/([a-zA-Z0-9_.-]+)',  # user/username (legacy)
]

# Channel URL patterns (oEmbed doesn't work for these)
YOUTUBE_CHANNEL_PATTERNS = [
    r'^https?://(?:www\.)?youtube\.com/@([a-zA-Z0-9_.-]+)',
    r'^https?://(?:www\.)?youtube\.com/channel/([a-zA-Z0-9_-]+)',
    r'^https?://(?:www\.)?youtube\.com/c/([a-zA-Z0-9_.-]+)',
    r'^https?://(?:www\.)?youtube\.com/user/([a-zA-Z0-9_.-]+)',
]

YOUTUBE_OEMBED_URL = "https://www.youtube.com/oembed"
SUPADATA_API_URL = "https://api.supadata.ai/v1/youtube/video"
SUPADATA_TRANSCRIPT_URL = "https://api.supadata.ai/v1/youtube/transcript"
# Skip summarization if transcript is too short to be meaningful (~100 tokens)
MIN_TRANSCRIPT_CHARS = 400
# Max chars per single summarization call (~250k tokens, leaving room for prompt + response)
CHUNK_CHAR_LIMIT = 1_000_000

TRANSCRIPT_SUMMARY_PROMPT = """You are a research assistant extracting key takeaways from a YouTube video transcript for a personal knowledge base.

Given the transcript of the video titled "{video_title}", produce a summary in markdown format with exactly these two sections:

### Key Takeaways
Extract the most valuable insights, ideas, and actionable advice as a bulleted list. Each bullet should be a single concise sentence that captures one specific takeaway -- include enough context that the bullet is useful on its own without having watched the video. Attribute claims to the speaker when relevant (e.g., "- Intermittent fasting resolved chronic blood sugar issues that decades of diets couldn't fix - Tim"). Aim for 5-10 bullets depending on video length. No bold text, no sub-bullets, no multi-sentence explanations.

### Mentioned in Video
List specific people, companies, books, papers, tools, technologies, or other resources explicitly mentioned. Each item on its own bullet. Keep it to just the name and a brief phrase of context (e.g., "- Cal Newport -- deep work methodology"). If none are clearly mentioned, write "None identified."

Important:
- Be concise -- one sentence per bullet, no paragraphs
- Be factual -- summarize, do not editorialize
- Preserve the speaker's framing and terminology
- Always write the summary in English"""

CHUNK_SUMMARY_PROMPT = """You are summarizing part {chunk_number} of {total_chunks} of a YouTube video transcript titled "{video_title}".

Extract as a bulleted list:
- Key takeaways, insights, and actionable advice (one concise sentence each)
- People, products, books, tools, or resources mentioned (name + brief context)

Be factual and concise. This will be merged with summaries of the other parts."""

MERGE_SUMMARY_PROMPT = """You are a research assistant creating structured notes from a YouTube video for a personal knowledge base.

Below are summaries of {total_chunks} consecutive sections of the transcript for the video titled "{video_title}". Merge them into a single cohesive summary in markdown format with exactly these two sections:

### Key Takeaways
Combine and deduplicate the key takeaways into a single bulleted list. Each bullet should be one concise sentence capturing a specific insight or piece of advice. Attribute claims to the speaker when relevant. Aim for 5-10 bullets total. No bold text, no sub-bullets, no multi-sentence explanations.

### Mentioned in Video
Combine and deduplicate all mentioned people, companies, books, tools, technologies, or resources. Each item on its own bullet with just the name and a brief phrase of context. If none, write "None identified."

Important:
- Deduplicate across sections
- Be concise -- one sentence per bullet, no paragraphs
- Be factual -- summarize, do not editorialize
- Preserve the speaker's framing and terminology
- Always write the summary in English"""


PEOPLE_EXTRACTION_PROMPT = """Given the title and description of a YouTube video, identify the main people or groups featured in or discussed in this video. Return ONLY a JSON array of their names. Include hosts, guests, interviewees, podcast/show names, and people or groups who are a primary subject of discussion. Do NOT include people who are only briefly mentioned in passing.

If there are no clearly identifiable main people or groups, return an empty array: []

Examples:
- A Tim Ferriss podcast with Naval Ravikant: ["Tim Ferriss", "Naval Ravikant"]
- A Lex Fridman Podcast episode with a guest: ["Lex Fridman Podcast", "Lex Fridman", "Yann LeCun"]
- A solo tutorial by no specific person: []
- A documentary about Elon Musk: ["Elon Musk"]

Return ONLY the JSON array, no other text."""


def _sanitize_obsidian_link(name: str) -> str:
    """Remove characters that are illegal in Obsidian [[]] links."""
    # Obsidian link-illegal chars: [ ] | # ^ \
    return re.sub(r'[\[\]|#^\\\\/]', '', name).strip()


def _extract_people(video_title: str, description: str | None) -> list[str]:
    """Extract main people from a video's title and description using gpt-4o.

    Returns a list of names, or empty list if none identified.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return []

    user_input = f"Title: {video_title}"
    if description:
        user_input += f"\n\nDescription:\n{description}"

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": PEOPLE_EXTRACTION_PROMPT},
                {"role": "user", "content": user_input},
            ],
            temperature=0.0,
        )
        import json
        content = response.choices[0].message.content
        if content:
            names = json.loads(content.strip())
            if isinstance(names, list):
                return [n for n in names if isinstance(n, str) and n.strip()]
        return []
    except Exception as e:
        logger.warning("People extraction failed: %s", e)
        return []


def is_valid_youtube_url(url: str) -> bool:
    """Check if URL is a valid YouTube URL."""
    for pattern in YOUTUBE_PATTERNS:
        if re.match(pattern, url):
            return True
    return False


def _is_channel_url(url: str) -> bool:
    """Check if URL is a YouTube channel URL (requires different handling)."""
    for pattern in YOUTUBE_CHANNEL_PATTERNS:
        if re.match(pattern, url):
            return True
    return False


def _is_playlist_url(url: str) -> bool:
    """Check if URL is a YouTube playlist URL."""
    return bool(re.match(
        r'^https?://(?:www\.)?youtube\.com/playlist\?list=', url
    ))


def _extract_video_id(url: str) -> str | None:
    """Extract video ID from a YouTube video URL.

    Returns the 11-character video ID, or None if the URL is not a video URL.
    """
    video_patterns = [
        r'(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'youtu\.be/([a-zA-Z0-9_-]{11})',
        r'(?:www\.)?youtube\.com/shorts/([a-zA-Z0-9_-]{11})',
        r'm\.youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        r'(?:www\.)?youtube\.com/live/([a-zA-Z0-9_-]{11})',
    ]
    for pattern in video_patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _fetch_video_metadata_supadata(client: httpx.Client, video_id: str) -> dict | None:
    """Fetch video metadata from Supadata API.

    Returns dict with title, author_name, description on success, or None on failure.
    """
    api_key = os.getenv("SUPADATA_API_KEY")
    if not api_key:
        logger.warning("SUPADATA_API_KEY not set, cannot fetch video metadata")
        return None

    try:
        response = client.get(
            SUPADATA_API_URL,
            params={"id": video_id},
            headers={"x-api-key": api_key},
        )

        if response.status_code == 200:
            data = response.json()
            return {
                "title": data.get("title"),
                "author_name": data.get("channel", {}).get("name"),
                "description": data.get("description"),
            }
        else:
            logger.warning(
                "Supadata API returned %s for video %s",
                response.status_code,
                video_id,
            )
            return None

    except httpx.RequestError as e:
        logger.warning("Supadata API request failed for video %s: %s", video_id, e)
        return None


def _fetch_transcript(video_id: str) -> str | None:
    """Fetch video transcript from Supadata API.

    Returns the full transcript as a single string, or None if unavailable.
    """
    api_key = os.getenv("SUPADATA_API_KEY")
    if not api_key:
        logger.warning("SUPADATA_API_KEY not set, cannot fetch transcript")
        return None

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(
                SUPADATA_TRANSCRIPT_URL,
                params={"videoId": video_id, "lang": "en"},
                headers={"x-api-key": api_key},
            )

            if response.status_code == 200:
                data = response.json()
                segments = data.get("content", [])
                if not segments:
                    logger.info("Transcript returned empty content for video %s", video_id)
                    return None
                transcript = " ".join(seg.get("text", "") for seg in segments)
                return transcript.strip() or None

            elif response.status_code == 404:
                logger.info("No transcript available for video %s", video_id)
                return None
            else:
                logger.info(
                    "Supadata transcript API returned %s for video %s",
                    response.status_code,
                    video_id,
                )
                return None

    except httpx.RequestError as e:
        logger.warning("Transcript fetch failed for video %s: %s", video_id, e)
        return None


def _summarize_transcript(transcript: str, video_title: str) -> str | None:
    """Summarize a video transcript using OpenAI.

    For transcripts that fit within CHUNK_CHAR_LIMIT, uses a single API call.
    For longer transcripts, splits into chunks, summarizes each, then merges.

    Returns markdown-formatted summary string, or None on failure.
    """
    if len(transcript) < MIN_TRANSCRIPT_CHARS:
        logger.info("Transcript too short for summarization (%d chars), skipping", len(transcript))
        return None

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set, cannot summarize transcript")
        return None

    estimated_tokens = len(transcript) // 4  # rough estimate: ~4 chars per token
    logger.info(
        "Transcript length: %d chars (~%d estimated tokens)",
        len(transcript),
        estimated_tokens,
    )

    try:
        client = OpenAI(api_key=api_key)

        if len(transcript) <= CHUNK_CHAR_LIMIT:
            return _single_pass_summary(client, transcript, video_title)
        else:
            return _chunked_summary(client, transcript, video_title)

    except Exception as e:
        logger.warning("Transcript summarization failed: %s", e)
        return None


def _single_pass_summary(client: OpenAI, transcript: str, video_title: str) -> str | None:
    """Summarize a transcript in a single OpenAI call."""
    prompt = TRANSCRIPT_SUMMARY_PROMPT.format(video_title=video_title)

    response = client.chat.completions.create(
        model="gpt-5.2",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": transcript},
        ],
        temperature=0.3,
    )

    summary = response.choices[0].message.content
    if summary:
        logger.info(
            "Generated transcript summary (%d chars) using %d prompt tokens",
            len(summary),
            response.usage.prompt_tokens,
        )
        return summary.strip()
    return None


def _chunked_summary(client: OpenAI, transcript: str, video_title: str) -> str | None:
    """Split a long transcript into chunks, summarize each, then merge."""
    num_chunks = math.ceil(len(transcript) / CHUNK_CHAR_LIMIT)
    chunk_size = math.ceil(len(transcript) / num_chunks)
    chunks = [transcript[i:i + chunk_size] for i in range(0, len(transcript), chunk_size)]

    logger.info("Splitting transcript into %d chunks of ~%d chars each", num_chunks, chunk_size)

    chunk_summaries = []
    for i, chunk in enumerate(chunks, 1):
        logger.info("Summarizing chunk %d/%d (%d chars)", i, num_chunks, len(chunk))
        prompt = CHUNK_SUMMARY_PROMPT.format(
            chunk_number=i, total_chunks=num_chunks, video_title=video_title
        )
        response = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": chunk},
            ],
            temperature=0.3,
        )
        chunk_summary = response.choices[0].message.content
        if chunk_summary:
            chunk_summaries.append(chunk_summary.strip())

    if not chunk_summaries:
        return None

    logger.info("Merging %d chunk summaries", len(chunk_summaries))
    merged_input = "\n\n---\n\n".join(
        f"## Section {i}\n{s}" for i, s in enumerate(chunk_summaries, 1)
    )
    merge_prompt = MERGE_SUMMARY_PROMPT.format(
        total_chunks=len(chunk_summaries), video_title=video_title
    )
    response = client.chat.completions.create(
        model="gpt-5.2",
        messages=[
            {"role": "system", "content": merge_prompt},
            {"role": "user", "content": merged_input},
        ],
        temperature=0.3,
    )
    summary = response.choices[0].message.content
    if summary:
        logger.info("Generated merged transcript summary (%d chars)", len(summary))
        return summary.strip()
    return None


def fetch_youtube_metadata(url: str) -> dict:
    """Fetch video/channel metadata from Supadata API (videos) or page scraping (channels).

    Falls back to YouTube oEmbed + page scraping if Supadata is unavailable.

    Returns:
        dict with keys: title, author_name, description (may be None if fetch fails)
    """
    result = {"title": None, "author_name": None, "description": None}

    try:
        with httpx.Client(timeout=10.0) as client:
            # Channels: scrape page directly (Supadata doesn't support channels)
            if _is_channel_url(url):
                channel_meta = _fetch_channel_metadata(client, url)
                result["title"] = channel_meta.get("title")
                result["description"] = channel_meta.get("description")
                return result

            # Playlists: use oEmbed only (Supadata doesn't support playlists)
            if _is_playlist_url(url):
                response = client.get(
                    YOUTUBE_OEMBED_URL,
                    params={"url": url, "format": "json"},
                )
                if response.status_code == 200:
                    data = response.json()
                    result["title"] = data.get("title")
                    result["author_name"] = data.get("author_name")
                return result

            # Videos: use Supadata API as primary source
            video_id = _extract_video_id(url)
            if video_id:
                supadata_result = _fetch_video_metadata_supadata(client, video_id)
                if supadata_result is not None:
                    return supadata_result
                logger.info("Supadata failed for %s, falling back to oEmbed", url[:100])

            # Fallback: oEmbed + page scraping (original approach)
            response = client.get(
                YOUTUBE_OEMBED_URL,
                params={"url": url, "format": "json"},
            )
            if response.status_code == 200:
                data = response.json()
                result["title"] = data.get("title")
                result["author_name"] = data.get("author_name")

            result["description"] = _fetch_youtube_description(client, url)

    except httpx.RequestError as e:
        logger.warning("Failed to fetch YouTube metadata: %s", e)
    except Exception as e:
        logger.warning("Unexpected error fetching YouTube metadata: %s", e)

    return result


def _fetch_channel_metadata(client: httpx.Client, url: str) -> dict:
    """Fetch channel metadata by scraping the page (oEmbed doesn't work for channels)."""
    result = {"title": None, "description": None}

    try:
        response = client.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
            follow_redirects=True,
        )

        if response.status_code != 200:
            logger.warning("YouTube channel page returned %s for %s", response.status_code, url[:100])
            return result

        html = response.text

        # Extract title from <title> tag (format: "Channel Name - YouTube")
        title_match = re.search(r'<title>([^<]+)</title>', html)
        if title_match:
            title = title_match.group(1)
            # Remove " - YouTube" suffix
            if title.endswith(" - YouTube"):
                title = title[:-10]
            result["title"] = title

        # Extract description from meta tag
        desc_match = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html)
        if desc_match:
            result["description"] = desc_match.group(1)

    except Exception as e:
        logger.warning("Failed to fetch YouTube channel metadata: %s", e)

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
            follow_redirects=True,
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
        # Fetch metadata from Supadata API (with oEmbed fallback)
        metadata = fetch_youtube_metadata(url)
        video_title = metadata["title"] or url  # Fallback to URL if title unavailable
        description = metadata["description"]

        # Extract main people from title/description
        people = _extract_people(video_title, description)

        # Fetch and summarize transcript (non-blocking to core flow)
        summary_section = ""
        video_id = _extract_video_id(url)
        if video_id and not _is_channel_url(url) and not _is_playlist_url(url):
            transcript = _fetch_transcript(video_id)
            if transcript:
                summary = _summarize_transcript(transcript, video_title)
                if summary:
                    summary_section = f"\n## AI Summary\n\n{summary}\n"

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

        # Build Channel YAML field
        channel_name = metadata.get("author_name")
        channel_yaml = ""
        if channel_name:
            safe_channel = _sanitize_obsidian_link(channel_name)
            channel_yaml = f'\nChannel: "[[{safe_channel}]]"'

        # Build People YAML field
        people_yaml = ""
        if people:
            people_links = [f'  - "[[{_sanitize_obsidian_link(p)}]]"' for p in people]
            people_yaml = "\nPeople:\n" + "\n".join(people_links)

        # Generate markdown content with YAML frontmatter
        markdown_content = f"""---
Journal:
  - "[[{formatted_local_date}]]"
created time: {now_utc.isoformat()}
modified time: {now_utc.isoformat()}
key words:
URL: {url}{channel_yaml}{people_yaml}
Notes+Ideas:
Experiences:
Tags:
  - youtube
---

## {video_title}
{description_section}
{summary_section}"""

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
