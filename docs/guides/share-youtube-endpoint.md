# Share YouTube API Endpoint Guide

## Overview

The `/share/youtube` endpoint receives YouTube video URLs (typically from iOS Shortcuts) and saves them as individual markdown files in the Obsidian Knowledge Hub folder in Dropbox. It automatically fetches video metadata (title, description) from the Supadata API, with a fallback to YouTube's oEmbed API and page scraping if Supadata is unavailable.

## Endpoint Details

```
POST /share/youtube
```

### Authentication
- Header: `X-API-Key`
- Environment variable: `LINK_SHARE_API_KEY` (same as /share/link)

### Request Body
```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID"
}
```

- `url` (required): A valid YouTube URL

### Supported URL Formats
- `https://www.youtube.com/watch?v=VIDEO_ID`
- `https://youtu.be/VIDEO_ID` (shortened)
- `https://www.youtube.com/shorts/VIDEO_ID`
- `https://m.youtube.com/watch?v=VIDEO_ID` (mobile)
- `https://youtube.com/watch?v=VIDEO_ID` (no www)
- `https://www.youtube.com/embed/VIDEO_ID`

### Response
- **202 Accepted**: Request received, processing in background
- **401 Unauthorized**: Missing or invalid API key
- **422 Unprocessable Entity**: Missing `url` field or invalid YouTube URL

```json
{"status": "accepted", "message": "YouTube link queued for processing"}
```

## Implementation Files

| File | Purpose |
|------|---------|
| `app/main.py` | Endpoint definition, `YouTubeShareRequest` model, auth, URL validation |
| `app/services/obsidian/add_youtube_link.py` | YouTube metadata fetching, Dropbox file creation |

## How It Works

1. **Authentication**: Validates `X-API-Key` header against `LINK_SHARE_API_KEY` env var
2. **URL Validation**: Checks URL matches known YouTube patterns (returns 422 if invalid)
3. **Background Processing**: Returns 202 immediately, processes via `BackgroundTasks`
4. **Metadata Fetching**: Calls Supadata API for video metadata (title, description, channel name). Falls back to YouTube oEmbed + page scraping if Supadata is unavailable
5. **Folder Discovery**: Finds folder ending with `_Knowledge-Hub` in vault
6. **Filename Sanitization**: Replaces `[\/:*?"<>|]` with `_`
7. **Duplicate Check**: Skips if file already exists
8. **File Creation**: Creates markdown file with YAML frontmatter

## File Format

Each YouTube link creates a separate `.md` file:

```markdown
---
Journal:
  - "[[Jan 19, 2026]]"
created time: 2026-01-19T15:30:00+00:00
modified time: 2026-01-19T15:30:00+00:00
key words:
URL: https://www.youtube.com/watch?v=VIDEO_ID
Notes+Ideas:
Experiences:
Tags:
  - youtube
---

## Video Title

Video description text goes here...

```

Note: A `youtube` tag is automatically added, and the video description is included in the body of the markdown file.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LINK_SHARE_API_KEY` | Yes | API key for authentication (same as /share/link) |
| `DROPBOX_OBSIDIAN_VAULT_PATH` | Yes | Root path to Obsidian vault in Dropbox |
| `DROPBOX_ACCESS_KEY` | Yes | Dropbox OAuth app key |
| `DROPBOX_ACCESS_SECRET` | Yes | Dropbox OAuth app secret |
| `DROPBOX_REFRESH_TOKEN` | Yes | Dropbox OAuth refresh token |
| `REDIS_HOST` | No | Redis host (default: localhost) |
| `REDIS_PORT` | No | Redis port (default: 6379) |
| `SUPADATA_API_KEY` | Yes | API key for Supadata YouTube metadata API |
| `SYSTEM_TIMEZONE` | No | Timezone for dates (default: US/Eastern) |

## Key Functions in `add_youtube_link.py`

```python
def is_valid_youtube_url(url: str) -> bool:
    """Check if URL is a valid YouTube URL."""

def fetch_youtube_metadata(url: str) -> dict:
    """Fetch video metadata from Supadata API (with oEmbed fallback).
    Returns: dict with keys: title, author_name, description"""

def add_youtube_link(url: str) -> dict:
    """Main entry point. Returns {"success": bool, "action": str | None, "error": str | None}"""
```

## Testing

```bash
# Test authentication
curl -X POST http://localhost:8000/share/youtube \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
# Expected: 401

# Test invalid YouTube URL
curl -X POST http://localhost:8000/share/youtube \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{"url": "https://example.com/video"}'
# Expected: 422

# Test successful request
curl -X POST http://localhost:8000/share/youtube \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
# Expected: 202
```

## Related Guides

- [iOS Shortcut: Save YouTube to Obsidian](./ios-shortcut-save-youtube.md) - Step-by-step iOS Shortcut setup
- [Share Link Endpoint](./share-link-endpoint.md) - General link sharing endpoint

## Related Code

- **Link sharing endpoint**: `/share/link` - Same authentication pattern, similar file format
- **Dropbox utilities**: Reuses `_get_dropbox_client`, `_find_knowledge_hub_path`, etc. from `add_shared_link.py`
- **Supadata API**: Primary video metadata source at `https://api.supadata.ai/v1/youtube/video`
- **oEmbed API**: YouTube's public metadata API at `https://www.youtube.com/oembed` (fallback for videos, primary for playlists)
