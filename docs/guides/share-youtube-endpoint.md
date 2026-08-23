# Share YouTube API Endpoint Guide

## Overview

The `/share/youtube` endpoint receives YouTube video URLs (typically from iOS Shortcuts) and saves them as individual markdown files in the Obsidian Knowledge Hub folder in Dropbox. It automatically fetches video metadata (title, description) from the Supadata API, with a fallback to YouTube's oEmbed API and page scraping if Supadata is unavailable.

It also POSTs the YouTube URL to Readwise Reader (`POST https://readwise.io/api/v3/save/`, `category: video`) when a usable transcript exists, so Genesis can highlight the video in Reader. Channels, playlists, empty/404/error transcript fetches, and whitespace-only transcripts skip Reader — Knowledge Hub, journal Content Buffet, and Raindrop still run. The Knowledge Hub filename and buffet wikilink use the same `reader_knowledge_hub_note_stem` helper as other Reader docs: `Title by Author` when author/creator exists, otherwise title-only. After a Reader save, the note prefers Reader's list title + author; if Reader is skipped or has no title yet, it falls back to the YouTube title (and channel) through that same helper. `/share/link` routes YouTube URLs through this path; regular article shares are unchanged.

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
| `app/main.py` | Endpoint definition, `YouTubeShareRequest` model, auth, URL validation, transcript gate, optional Reader save, then KH write |
| `app/services/obsidian/add_youtube_link.py` | YouTube metadata fetching, Dropbox file creation, YAML extras merge |
| `app/services/readwise/reader.py` | `save_document` (`POST /api/v3/save/`) and `get_document` (`GET /api/v3/list/?id=`) |

## How It Works

1. **Authentication**: Validates `X-API-Key` header against `LINK_SHARE_API_KEY` env var
2. **URL Validation**: Checks URL matches known YouTube patterns (returns 422 if invalid)
3. **Background Processing**: Returns 202 immediately, processes via `BackgroundTasks`
4. **Metadata Fetching**: Calls Supadata API for video metadata (title, description, channel name). Falls back to YouTube oEmbed + page scraping if Supadata is unavailable
5. **Transcript Fetching**: Calls Supadata transcript API once (skipped for channels/playlists). Empty/404/error/whitespace = no transcript. This fetch is reused for the AI summary — no second Supadata transcript call.
6. **Reader save (transcript required)**: Only when stripped transcript text exists (auto-captions count; `MIN_TRANSCRIPT_CHARS` is only for AI summary). POSTs the YouTube URL to Reader Document CREATE with `category: video` and `saved_using: gen-intelligence` (title is left for Reader to scrape). `201` (new) and `200` (already exists) are both success. Failures are logged; the KH write still happens. No transcript → skip Reader and omit `readwise_id` / `readwise_url`.
7. **Reader list**: After a successful save, GETs `/api/v3/list/?id=` for title + author/creator. Save only returns `{id, url}`.
8. **AI Summarization**: Sends the same transcript to OpenAI gpt-5.2 for structured summarization (skipped if transcript is missing, shorter than `MIN_TRANSCRIPT_CHARS`, or OPENAI_API_KEY is not set)
9. **Folder Discovery**: Finds folder ending with `_Knowledge-Hub` in vault
10. **Filename / wikilink**: `reader_knowledge_hub_note_stem(Reader title, author)` — `Title by Author` when author exists. If Reader is skipped or has no title yet, the same helper runs on the YouTube title + channel.
11. **Duplicate / identity lookup**: Prefer that stem. If the file is missing, find an existing note by YAML `URL` (YouTube id-aware) or `readwise_id` and update it — never create a second file when the title string drifted.
12. **File Creation / extras**: Writes the markdown note with YAML `URL` (YouTube). When Reader was saved, fill-if-empty `readwise_id` and `readwise_url`. `readwise_url` is `https://read.readwise.io/read/{id}` (the save API returns `/new/read/{id}`; both open the video).
13. **Journal buffet**: Standalone `- [[stem]]` using that same normalized stem.
14. **Raindrop**: On a new KH note, mirrors the URL to Raindrop Unsorted (failures are logged and do not undo the note). Reader skip does not skip Raindrop.

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
readwise_id: 01kb5cap1wy21zp37bc2rjj
readwise_url: https://read.readwise.io/read/01kb5cap1wy21zp37bc2rjj
Notes+Ideas:
Experiences:
Tags:
  - youtube
---

## Video Title

Video description text goes here...

```

Note: A `youtube` tag is automatically added, and the video description is included in the body of the markdown file. Filename and Content Buffet use the shared Reader stem (`Video Title by Channel.md` / `- [[Video Title by Channel]]` when author exists). `readwise_id` and `readwise_url` are filled if empty so Genesis can open the video from metadata, and later `readwise.highlight.created` events can find the same note by stem, YouTube `URL`, or `readwise_id`.

When `reader.any_document.created` fires after this save, the webhook calls `add_youtube_link` with Reader's title/author. If a note already exists for this YouTube URL or `readwise_id`, it only fill-if-empty extras — it does not create a differently named file.

Later YouTube / Reader `category=video` highlights append `- ["quote"](https://readwise.io/open/{id})` under `### Transcript Highlights` at the **top** of that note's body (heading is created if missing; other sections stay). Dedup is the open/id URL. Tweets and books are unchanged.

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
| `OPENAI_API_KEY` | No | API key for OpenAI transcript summarization (summary skipped if not set) |
| `READWISE_TOKEN` | Yes (for Reader save) | Readwise access token for `POST /api/v3/save/` (same token as highlight export). Missing token is logged; KH write still succeeds. |
| `SYSTEM_TIMEZONE` | No | Timezone for dates (default: US/Eastern) |

## Key Functions in `add_youtube_link.py`

```python
def is_valid_youtube_url(url: str) -> bool:
    """Check if URL is a valid YouTube URL."""

def fetch_youtube_metadata(url: str) -> dict:
    """Fetch video metadata from Supadata API (with oEmbed fallback).
    Returns: dict with keys: title, author_name, description"""

def add_youtube_link(url: str) -> dict:
    """Main entry point. Returns success, action, stem, file_path, ..."""
```

`fetch_youtube_transcript` in `add_youtube_link.py` is the share-path gate (URL in, stripped text or None). `save_document` in `app/services/readwise/reader.py` POSTs to Reader only when that text exists. `get_document` lists the saved video for title/author. `reader_permalink` normalizes the save API's `/new/read/{id}` to `/read/{id}` for the `readwise_url` YAML key. Naming is `youtube_knowledge_hub_note_stem` → `reader_knowledge_hub_note_stem`.

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
