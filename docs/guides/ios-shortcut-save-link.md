# iOS Shortcut: Save Link to Obsidian

This guide walks through creating an iOS Shortcut that sends links from the Share Sheet to the `/share/link` API endpoint, saving them to your Obsidian Knowledge Hub.

## Prerequisites

- Your API server deployed and accessible (e.g., `https://your-domain.com`)
- Your `LINK_SHARE_API_KEY` value
- iOS 15+ (Shortcuts app)

## Understanding Shortcut Input

When you use the iOS Share Sheet, the `Shortcut Input` variable is a container that can hold multiple items. Its contents change depending on how you share:

- **Sharing a Page (No Text Selected)**: The input often contains two items: the page URL and the page's title as text.
- **Sharing Selected Text from a Page**: The input contains the page URL and the specific text you highlighted.

This shortcut handles both cases gracefully, correctly identifying the URL and using the remaining text as an editable title.

## Shortcut Setup

### Step 1: Receive and Isolate the URL

First, grab whatever is shared and reliably extract the URL.

1. **Receive** `Any Input` from `Share Sheet`
   - Set this to `Any` to be robust, as we filter it ourselves

2. **Get URLs from** `Shortcut Input`
   - This is the most reliable way to get the URL, regardless of what else is shared

3. **If** `URLs` **has no value**
   - **Show Alert**: "No URL Found. Please share a webpage or content with a link."
   - **Stop this shortcut**

4. **End If**

5. **Set variable** `SharedURL` to `URLs`
   - We now have the URL stored safely

### Step 2: Isolate and Clean Up the Text

Next, get all the other text that was shared, which could be the page title or selected text.

1. **Get Text from Input** from `Shortcut Input`

2. **Replace Text** in `Text`
   - Find: `SharedURL`
   - Replace with: (leave this empty)
   - This removes the URL itself from the text block, leaving only the title or selected notes

3. **Set variable** `SharedText` to `Modified Text`

### Step 3: The Editable Confirmation Screen

Present the captured information to the user for editing before saving. This is similar to Notion's capture interface.

1. **Ask for Input**
   - Prompt: `Edit Title & Add Notes`
   - Question 1: `Title`
     - Default Answer: `SharedText` (pre-fills with our best guess for the title)
   - Question 2: `Notes`
     - Default Answer: (leave empty for user to optionally add notes)

This creates a clean dialog with two editable fields. The user can confirm or edit the pre-filled title and add any notes.

### Step 4: Build the Request Payload

Create the JSON payload to send to your API.

1. **Get Dictionary from Input**
   - Key 1: `url`, Value: `SharedURL`
   - Key 2: `title`, Value: `Title` (magic variable from Ask for Input)

### Step 5: Send to Your API

Send the confirmed data to the FastAPI endpoint.

1. **Get Contents of URL**
   - URL: `https://your-domain.com/share/link`
   - Method: `POST`
   - Headers:
     - `Content-Type`: `application/json`
     - `X-API-Key`: `your-api-key-here`
   - Request Body: `Dictionary` (the variable from previous step)

### Step 6: Show Confirmation

Provide feedback to the user.

1. **Show Notification**
   - Title: `Saved to Obsidian`
   - Body: `Title` (the variable from Ask for Input)

## Complete Shortcut Flow

```
[Receive Any Input from Share Sheet]
        |
        v
[Get URLs from Shortcut Input]
        |
        v
[If URLs has no value] --> [Show Alert: No URL Found] --> [Stop]
        |
        v (URLs exist)
[Set variable SharedURL = URLs]
        |
        v
[Get Text from Input from Shortcut Input]
        |
        v
[Replace Text: Find SharedURL, Replace with empty]
        |
        v
[Set variable SharedText = Modified Text]
        |
        v
[Ask for Input: Title (default: SharedText), Notes (default: empty)]
        |
        v
[Get Dictionary: {url: SharedURL, title: Title}]
        |
        v
[Get Contents of URL: POST to /share/link]
        |
        v
[Show Notification: Saved to Obsidian]
```

## Testing the Shortcut

1. **Open Safari** and navigate to any article
2. **Tap the Share button**
3. **Select your shortcut** from the share sheet
4. **Verify** the title field is pre-filled with the page title
5. **Edit if needed** and tap Done
6. **Confirm** you see the success notification
7. **Check your Obsidian vault** for the new file in the Knowledge Hub folder

## Troubleshooting

### "No URL Found" Error

- Make sure you're sharing from an app that provides URLs (Safari, Chrome, News apps, etc.)
- Some apps only share text without a URL

### Request Failed

- Verify your API server is running and accessible
- Check that your API key is correct
- Ensure the URL is `https://` (not `http://`)

### Title Not Pre-Filled

- Some apps don't share the page title
- The title will be empty, and you can type it manually

### Notification Not Showing

- Check iOS notification settings for the Shortcuts app
- Ensure "Show Notification" action is at the end of the shortcut

## Tips

- **Pin the shortcut**: In the Shortcuts app, you can add this shortcut to your home screen for quick access
- **Share Sheet position**: Long-press the shortcut in the Share Sheet to move it to a more accessible position
- **Siri integration**: You can run this shortcut with Siri by saying "Save link to Obsidian" (customize the shortcut name)

## Related Guides

- [Share Link API Endpoint](./share-link-endpoint.md) - API documentation and implementation details
- [iOS Shortcut: Save YouTube to Obsidian](./ios-shortcut-save-youtube.md) - YouTube-specific shortcut
