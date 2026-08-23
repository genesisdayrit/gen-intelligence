# Raindrop Webhook Setup

Inbound `POST /raindrop/webhook` writes a Knowledge Hub note and a standalone journal Content Buffet wikilink when a Raindrop bookmark/document is **created**. This is the same path as `/share/link` and the Reader-document flow: YouTube URLs use `add_youtube_link`, everything else uses `add_shared_link(url, title=...)`. Those helpers apply the 3am-aware journal date (`get_effective_date` + `journal_filename`) and append `- [[Note Title]]` (filename stem only — no quote, no Raindrop URL).

Raindrop's public API has **no first-party webhook** ([developer.raindrop.io](https://developer.raindrop.io/)). Point IFTTT, Make, or a manual POST at this URL instead.

```
https://<ngrok>/raindrop/webhook
```

Use your current tunnel or `WEBHOOK_BASE_URL`, e.g. `https://<ngrok>/raindrop/webhook`.

## Secret

Set `RAINDROP_WEBHOOK_SECRET` in the app environment. Send the same value in **either** place (both are accepted):

- JSON body field `secret`
- Header `X-Raindrop-Webhook-Secret` (also accepted: `X-Webhook-Secret`)

Empty or whitespace-only POSTs are a 200 test ping (no write). A bad or missing secret is 401. Created bookmarks return 202 and write in the background.

Do not print or commit the secret.

## What is written

- **Created only.** A payload is treated as created when it is a new raindrop/item/bookmark. Delete/update events are ignored when distinguishable (`event` / `event_type` / `action`, or `removed: true`).
- Title + URL from common shapes: `{title, url|link}`, `{raindrop: {title, link}}`, `{item: {...}}`, `{items: [...]}`, IFTTT-style `Title` / `Url`.
- Same-day existing KH note: skipped (no second buffet line). This is what stops a loop when `/share/link` already mirrored the URL to Raindrop and this webhook fires.
- Missing journal: buffet is skipped; KH save still succeeds; the journal is not created.
- Raindrop highlights are not pulled or written. Outbound `/share/link` → `create_bookmark` is unchanged.

## Send a document-created POST

### curl (body secret)

```bash
curl -X POST https://<ngrok>/raindrop/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "<RAINDROP_WEBHOOK_SECRET>",
    "title": "Example Article",
    "url": "https://example.com/article"
  }'
```

### curl (header secret)

```bash
curl -X POST https://<ngrok>/raindrop/webhook \
  -H "Content-Type: application/json" \
  -H "X-Raindrop-Webhook-Secret: <RAINDROP_WEBHOOK_SECRET>" \
  -d '{
    "title": "Example Article",
    "link": "https://example.com/article"
  }'
```

### Empty ping

```bash
curl -X POST https://<ngrok>/raindrop/webhook
# 200 {"status":"ok"}
```

### IFTTT

Raindrop has no official webhook, so use an IFTTT applet:

1. **If:** Raindrop — *New item* or *New item in a collection*.
2. **Then:** Webhooks — *Make a web request*.
   - URL: `https://<ngrok>/raindrop/webhook`
   - Method: POST
   - Content type: `application/json`
   - Body:

```json
{
  "secret": "<RAINDROP_WEBHOOK_SECRET>",
  "title": "{{Title}}",
  "url": "{{Url}}"
}
```

IFTTT maps `Url` from Raindrop's `link` ingredient.

### Make (Integromat)

1. Trigger: Raindrop — *Watch Raindrops* (or a collection filter).
2. Action: HTTP — *Make a request*.
   - URL: `https://<ngrok>/raindrop/webhook`
   - Method: POST
   - Headers: `X-Raindrop-Webhook-Secret: <RAINDROP_WEBHOOK_SECRET>`
   - Body type: JSON

```json
{
  "title": "{{title}}",
  "link": "{{link}}"
}
```

A raw Raindrop REST item (`{"item": {"title": "...", "link": "..."}}`) is also accepted.

## Environment

```bash
RAINDROP_WEBHOOK_SECRET=your_raindrop_webhook_secret
# Outbound /share/link mirror (unchanged)
# RAINDROP_IO_TEST_TOKEN=your_raindrop_access_token
```
