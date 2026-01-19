# iOS Shortcut: Save YouTube to Obsidian

This guide walks through creating an iOS Shortcut that sends YouTube video URLs from the Share Sheet to the `/share/youtube` API endpoint, saving them to your Obsidian Knowledge Hub with automatic title fetching.

## Prerequisites

- Your API server deployed and accessible (e.g., `https://your-domain.com`)
- Your `LINK_SHARE_API_KEY` value
- iOS 15+ (Shortcuts app)

## Why a Separate YouTube Shortcut?

The iOS Share Sheet for the YouTube app only provides the video URL - it does not include the title. The `/share/youtube` endpoint solves this by fetching the video title server-side using YouTube's oEmbed API.

This makes the shortcut much simpler than the general link shortcut since there's no title to capture or edit.

## Shortcut Setup

### Step 1: Receive URLs from Share Sheet

1. **Receive** `URLs` from `Share Sheet`
   - This receives the YouTube video URL directly

### Step 2: Extract the URL

1. **Get URLs from Input**
   - Input: `Shortcut Input`
   - This ensures we have a clean URL to work with

### Step 3: Optional Confirmation

Add an optional confirmation step to prevent accidental saves.

1. **Show Alert**
   - Title: `Save YouTube video to Obsidian?`
   - Message: `URLs` (the variable)
   - Turn `Show Cancel Button` **ON**

If the user taps Cancel, the shortcut stops. If they tap OK, it continues.

### Step 4: Build the Request Payload

1. **Get Dictionary from Input**
   - Key: `url`, Value: `URLs`

### Step 5: Send to Your API

1. **Get Contents of URL**
   - URL: `https://your-domain.com/share/youtube`
   - Method: `POST`
   - Headers:
     - `Content-Type`: `application/json`
     - `X-API-Key`: `your-api-key-here`
   - Request Body: `Dictionary` (the variable from previous step)

### Step 6: Handle Response

Add error handling for a better user experience.

1. **If** `Status Code` **is** `202`
   - **Show Notification**: Title: `YouTube link saved!`

2. **Otherwise**
   - **Show Notification**: Title: `Save Failed`, Body: Check API key and server status

3. **End If**

## Complete Shortcut Flow

```
[Receive URLs from Share Sheet]
        |
        v
[Get URLs from Input]
        |
        v
[Show Alert: Save YouTube video to Obsidian?] --> [Cancel: Stop]
        |
        v (OK)
[Get Dictionary: {url: URLs}]
        |
        v
[Get Contents of URL: POST to /share/youtube]
        |
        v
[If Status Code is 202]
    |           |
    v           v
[Success]   [Failed]
[Notify]    [Notify]
```

## Alternative: Minimal Shortcut

If you don't need confirmation, here's an even simpler version:

1. **Receive** `URLs` from `Share Sheet`
2. **Get Dictionary from Input**: `{url: Shortcut Input}`
3. **Get Contents of URL**: POST to `/share/youtube` with headers
4. **Show Notification**: `YouTube link saved!`

This four-action shortcut is the fastest way to save YouTube videos.

## Testing the Shortcut

1. **Open the YouTube app** and navigate to any video
2. **Tap the Share button** below the video
3. **Select your shortcut** from the share sheet
4. **Confirm** (if you added the confirmation step)
5. **Verify** you see the success notification
6. **Check your Obsidian vault** for the new file in the Knowledge Hub folder

The file will be named with the video title (fetched automatically by the server) and include the YouTube channel name in the metadata.

## Supported YouTube URL Formats

The API accepts all common YouTube URL formats:

- `https://www.youtube.com/watch?v=VIDEO_ID`
- `https://youtu.be/VIDEO_ID` (shortened)
- `https://www.youtube.com/shorts/VIDEO_ID`
- `https://m.youtube.com/watch?v=VIDEO_ID` (mobile)
- `https://youtube.com/watch?v=VIDEO_ID` (no www)

## Troubleshooting

### Request Failed (422 Error)

- The URL might not be a valid YouTube URL
- Try sharing directly from the YouTube app rather than a browser

### Request Failed (401 Error)

- Your API key is invalid or missing
- Double-check the `X-API-Key` header value

### Notification Not Showing

- Check iOS notification settings for the Shortcuts app
- The request may have failed silently - check the status code handling

### Video Title is Wrong or Missing

- The server uses YouTube's oEmbed API which is very reliable
- If a title is wrong, it's how YouTube returns it
- Deleted or private videos may not have titles available

## Tips

- **Quick access**: Add this shortcut to your home screen for one-tap access
- **Siri integration**: Say "Save YouTube to Obsidian" to run the shortcut
- **Share Sheet order**: Reorder your Share Sheet to put this shortcut near the top

## What Gets Saved

Each YouTube video creates a markdown file with:

- **Filename**: Video title (sanitized for filesystem)
- **Journal date**: Current date linked to your daily note
- **People field**: YouTube channel name
- **URL**: Full YouTube video URL
- **Tags**: `youtube` tag automatically added

Example file content:

```markdown
---
Journal:
  - "[[Jan 19, 2026]]"
created time: 2026-01-19T15:30:00+00:00
modified time: 2026-01-19T15:30:00+00:00
key words:
People: "Channel Name"
URL: https://www.youtube.com/watch?v=VIDEO_ID
Notes+Ideas:
Experiences:
Tags:
  - youtube
---

## Video Title Here

```

## Related Guides

- [Share YouTube API Endpoint](./share-youtube-endpoint.md) - API documentation and implementation details
- [iOS Shortcut: Save Link to Obsidian](./ios-shortcut-save-link.md) - General link sharing shortcut
