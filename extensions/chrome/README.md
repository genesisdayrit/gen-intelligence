# gen-intelligence - Chrome Extension

A Chrome extension to quickly save web page links to your Obsidian Knowledge Hub.

## Features

- Save any webpage to your Obsidian Knowledge Hub with one click
- Edit the page title before saving
- Keyboard shortcut support (Enter to save/open)
- Open saved links directly in Obsidian
- "Check connection" button to verify the server can accept new saves, with a per-check breakdown to help debug failures

## Installation

### 1. Load the Extension in Chrome

1. Open Chrome and navigate to `chrome://extensions`
2. Enable "Developer mode" (toggle in top right)
3. Click "Load unpacked"
4. Select the `extensions/chrome` directory from this repository

### 2. Configure the Extension

1. Click the extension icon in Chrome toolbar
2. If prompted, click "Open Settings" (or right-click the extension icon and select "Options")
3. Enter your settings:
   - **API Base URL**: Your server URL (e.g., `https://your-server.ngrok-free.dev`)
   - **API Key**: Your `LINK_SHARE_API_KEY` from the server environment
4. Click "Save Settings"

## Usage

1. Navigate to any webpage you want to save
2. Click the extension icon in the Chrome toolbar
3. Edit the title if needed (pre-populated with page title)
4. Press Enter or click "Save page"
5. After saving, press Enter or click "Open in Obsidian" to view the note

## API Requirements

This extension requires the `/share/link` endpoint from the Gen Intelligence API:

```
POST /share/link
Headers:
  Content-Type: application/json
  X-API-Key: <your-api-key>

Body:
{
  "url": "https://example.com/article",
  "title": "Optional custom title"
}

Response (HTTP 202, processed asynchronously):
{
  "status": "accepted",
  "message": "Link queued for processing",
  "file_path": "01_Knowledge-Hub/Article Title.md",
  "vault_name": "personal"
}
```

### Checking the connection

Click **Check connection** in the popup at any time to confirm the server can
accept new bookmarks. The extension calls the readiness endpoint and shows
either "Ready to accept bookmarks" or "Not ready to accept bookmarks" along
with each underlying check (vault path, Redis, Dropbox credentials, Dropbox
connection, and the Knowledge Hub folder) so you can see exactly what to fix.

This uses the `/share/health` endpoint:

```
GET /share/health
Headers:
  X-API-Key: <your-api-key>

Response (200 when ready, 503 when not):
{
  "ready": true,
  "checks": [
    { "name": "Vault path configured", "ok": true, "detail": "/obsidian/personal" },
    { "name": "Redis reachable", "ok": true, "detail": "localhost:6379" },
    { "name": "Dropbox credentials present", "ok": true, "detail": "..." },
    { "name": "Dropbox connection", "ok": true, "detail": "Authenticated as you@example.com" },
    { "name": "Knowledge Hub folder", "ok": true, "detail": "/obsidian/personal/01_Knowledge-Hub" }
  ]
}
```

## Environment Variables

Make sure your server has these environment variables set:

- `LINK_SHARE_API_KEY` - API key for authentication
- `DROPBOX_OBSIDIAN_VAULT_PATH` - Path to your Obsidian vault in Dropbox

## Troubleshooting

### "Please configure the extension settings first"
- Open extension options and enter your API URL and key

### "Invalid API key"
- Check that your API key matches `LINK_SHARE_API_KEY` on your server

### "Connection error"
- Verify your API server is running
- Check that the API Base URL is correct
- Ensure the server is accessible from your network

### "Open in Obsidian" doesn't work
- Make sure Obsidian is installed on your computer
- The obsidian:// URL scheme must be registered (happens automatically when Obsidian is installed)
