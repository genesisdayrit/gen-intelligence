# Share Link API Endpoint Guide

## Overview

The `/share/link` endpoint receives links (typically from iOS Shortcuts) and saves them as individual markdown files in the Obsidian Knowledge Hub folder in Dropbox.

## Endpoint Details

```
POST /share/link
```

### Authentication
- Header: `X-API-Key`
- Environment variable: `LINK_SHARE_API_KEY`

### Request Body
```json
{
  "url": "https://example.com/article",
  "title": "Optional Article Title"
}
```

- `url` (required): The link to save
- `title` (optional): Title for the file. If omitted, the URL is used as the title.

### Response
- **202 Accepted**: Request received, processing in background
- **401 Unauthorized**: Missing or invalid API key
- **422 Unprocessable Entity**: Missing required `url` field

```json
{"status": "accepted", "message": "Link queued for processing"}
```

## Implementation Files

| File | Purpose |
|------|---------|
| `app/main.py` | Endpoint definition, `LinkShareRequest` model, auth |
| `app/services/obsidian/add_shared_link.py` | Dropbox integration, file creation |

## How It Works

1. **Authentication**: Validates `X-API-Key` header against `LINK_SHARE_API_KEY` env var
2. **Background Processing**: Returns 202 immediately, processes via `BackgroundTasks`
3. **Folder Discovery**: Finds folder ending with `_Knowledge-Hub` in vault
4. **Filename Sanitization**: Replaces `[\/:*?"<>|]` with `_`
5. **Duplicate Check**: Skips if file already exists
6. **File Creation**: Creates markdown file with YAML frontmatter

## File Format

Each link creates a separate `.md` file:

```markdown
---
Journal:
  - "[[Jan 19, 2026]]"
created time: 2026-01-19T15:30:00+00:00
modified time: 2026-01-19T15:30:00+00:00
key words:
People:
URL: https://example.com/article
Notes+Ideas:
Experiences:
Tags:
---

## Article Title

```

This matches the existing Notion sync pattern for consistency in the Knowledge Hub.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LINK_SHARE_API_KEY` | Yes | API key for authentication |
| `DROPBOX_OBSIDIAN_VAULT_PATH` | Yes | Root path to Obsidian vault in Dropbox |
| `DROPBOX_ACCESS_KEY` | Yes | Dropbox OAuth app key |
| `DROPBOX_ACCESS_SECRET` | Yes | Dropbox OAuth app secret |
| `DROPBOX_REFRESH_TOKEN` | Yes | Dropbox OAuth refresh token |
| `REDIS_HOST` | No | Redis host (default: localhost) |
| `REDIS_PORT` | No | Redis port (default: 6379) |
| `SYSTEM_TIMEZONE` | No | Timezone for dates (default: US/Eastern) |

## Key Functions in `add_shared_link.py`

```python
def add_shared_link(url: str, title: str | None = None) -> dict:
    """Main entry point. Returns {"success": bool, "action": str | None, "error": str | None}"""

def _find_knowledge_hub_path(dbx, vault_path) -> str:
    """Finds folder ending with '_Knowledge-Hub' in vault"""

def _sanitize_filename(title: str) -> str:
    """Replaces invalid filename characters"""

def _file_exists(dbx, path) -> bool:
    """Checks if file already exists in Dropbox"""

def _get_dropbox_client() -> dropbox.Dropbox:
    """Gets authenticated Dropbox client (refreshes token if needed)"""
```

## Testing

```bash
# Test authentication
curl -X POST http://localhost:8000/share/link \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
# Expected: 401

# Test successful request
curl -X POST http://localhost:8000/share/link \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{"url": "https://example.com/article", "title": "Test Article"}'
# Expected: 202
```

## Related Guides

- [iOS Shortcut: Save Link to Obsidian](./ios-shortcut-save-link.md) - Step-by-step iOS Shortcut setup
- [Share YouTube Endpoint](./share-youtube-endpoint.md) - YouTube-specific endpoint with auto title fetching

## Related Code

- **Notion sync pattern**: See the attached Notion-to-Obsidian sync script for the original YAML frontmatter format
- **Dropbox token refresh**: Same pattern used in `add_telegram_log.py` and other obsidian services
- **Folder discovery**: Pattern from `_find_daily_folder()` in other services
