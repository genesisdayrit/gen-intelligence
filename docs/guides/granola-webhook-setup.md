# Granola Webhook Setup

Point Granola at `POST {WEBHOOK_BASE_URL}/granola/webhook` and set the signing secret as `GRANOLA_WEBHOOK_SECRET`. Set `GRANOLA_API_KEY` so the handler can `GET /v1/notes/{id}` — webhook payloads carry no note body.

Docs: [Granola webhooks](https://docs.granola.ai/webhooks). Business and Enterprise plans.

## Prerequisites

- A Granola workspace on a plan that includes webhooks
- A Granola API key ([Granola API](https://docs.granola.ai/help-center/sharing/integrations/granola-api))
- A publicly reachable HTTPS URL (`WEBHOOK_BASE_URL`, same as the other webhooks)
- The Gen Intelligence API running (see [EC2 Docker Setup](./ec2-docker-setup.md))

## 1. Create a webhook in Granola

1. Go to **Settings → Connectors → Webhooks**
2. Select **Set up a webhook**
3. Subscribe to all three:
   - `note.generated` — first AI summary (writes Transcript Notes)
   - `note.edited` — summary edited or regenerated (replaces the existing Transcript Notes block for that note, or inserts if it was never written)
   - `note.access_granted` — a note is newly shared with you (writes Transcript Notes)
4. Endpoint URL: `{WEBHOOK_BASE_URL}/granola/webhook`
5. Create the webhook and copy the signing secret (`whsec_…`). It is shown only once.

You can also create a folder-scoped webhook from a folder's **Integrations → Webhooks**, or register via `POST https://public-api.granola.ai/v1/webhook-endpoints` (see the Granola docs). Subscribe to all three events. Folder-based routing for shared/business notes may be added later.

## 2. Environment variables

Add these to `app/.env` (never log or print them):

```bash
WEBHOOK_BASE_URL=https://your-ngrok-url.ngrok-free.app
GRANOLA_API_KEY=your_granola_api_key
GRANOLA_WEBHOOK_SECRET=whsec_your_granola_webhook_signing_secret
```

Restart the app so it picks up the secret.

## 3. Verify setup

```bash
curl "$WEBHOOK_BASE_URL/health"
# {"status":"healthy"}
```

From Granola, send a test event. Check the API logs:

```bash
docker compose logs -f app
```

Look for `Granola webhook | event=… | note_id=…` and a write summary. The note should appear under `### Transcript Notes` on the matching daily journal (`01_Daily/_Journal/{Mon D, YYYY}.md`).

## How it works

1. Granola POSTs JSON: `event_id`, `event_type`, `note_id`, `occurred_at` (and, for `note.edited`, `data.changed_fields` — unused here).
2. The endpoint verifies the Standard Webhooks signature on the **raw body**, then rejects timestamps older than five minutes.
3. Retries reuse `event_id`. The first delivery is claimed in Redis (`granola:webhook:event:{event_id}`, 7-day TTL). Duplicates return `200` `{"status":"duplicate"}` and do not write again.
4. The handler acknowledges with `202` within Granola's 15s window, then `GET /v1/notes/{id}` with `GRANOLA_API_KEY` and reuses the same journal writers as the manual sync (`format_granola_block`, `### Transcript Notes`, 3am PT rollover). `note.generated` and `note.access_granted` skip an existing `<!-- granola:not_… -->` block. `note.edited` replaces that block in place (same position; neighboring notes stay put) or inserts if the original write was missed.
5. Missing journal files are skipped (not created, not dumped onto today). Private notes / transcripts are never written.

### Signature

| Header | Role |
| --- | --- |
| `webhook-id` | Event id (matches `event_id`) |
| `webhook-timestamp` | Unix seconds of this delivery attempt |
| `webhook-signature` | One or more `v1,<base64 HMAC-SHA256>` values |

Signed content is `{webhook-id}.{webhook-timestamp}.{raw_body}`. The HMAC key is the base64-decoded secret after the `whsec_` prefix.

## Historical notes

Webhooks do not replay the library. Use the year-2099 manual jobs:

- `POST /scheduler/jobs/backfill_granola_notes/run` — full (or filtered) history
- `POST /scheduler/jobs/sync_granola_notes/run` — incremental from Redis `granola:notes:cursor` (safety net; not on a cadence)

See [Granola Journal Sync](./granola-journal-sync.md).

## Troubleshooting

### 401 Invalid signature / Missing signature headers

- `GRANOLA_WEBHOOK_SECRET` in `.env` must match the `whsec_…` secret Granola showed at creation
- Restart the app after changing `.env`
- Compute the HMAC over the raw body, not a re-serialized JSON object

### 401 Timestamp expired

- Delivery `webhook-timestamp` is more than five minutes off the server clock
- Check host time sync; Granola will not retry other `4xx` responses

### Notes not appearing in Obsidian

- Check logs for `Granola webhook get note failed` (`GRANOLA_API_KEY` missing or HTTP error)
- `skipped_missing_journal` means that day's journal file does not exist
- An existing `<!-- granola:not_… -->` block is skipped for `note.generated` / `note.access_granted` (and for the manual sync/backfill jobs). `note.edited` replaces that block with the freshly fetched summary.

### Duplicate deliveries

- Expected: Granola retries reuse `event_id`. The Redis claim + journal HTML comment both skip a second write.

## Code location

- Endpoint: `app/main.py` (`POST /granola/webhook`)
- Verify / dedup / fetch+write: `app/services/granola/webhook.py`
- Journal writers: `app/services/granola/sync.py`
- Client: `app/services/granola/client.py`
