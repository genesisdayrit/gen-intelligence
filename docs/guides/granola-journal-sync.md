# Granola → Obsidian Journal Sync

Live notes arrive via `POST {WEBHOOK_BASE_URL}/granola/webhook` (see [Granola Webhook Setup](./granola-webhook-setup.md)). Subscribe Granola to `note.generated`, `note.edited`, and `note.access_granted`. Each event fetches `GET /v1/notes/{id}`. Generated and newly shared notes append a **summary block** under `### Transcript Notes` on the matching daily journal. `note.edited` replaces the existing block for that granola id (or inserts if the original write was missed) so the journal stays in the same note order.

Manual year-2099 jobs remain as safety nets: incremental `sync_granola_notes` (Redis last-run cursor) and full-history `backfill_granola_notes`. The hourly `reconcile_missed_obsidian_writes` job also calls that same incremental sync once per hour (idempotent `<!-- granola:not_… -->` blocks) instead of a second write path.

## Overview

**Webhook (primary)**

1. Granola POSTs `event_id`, `event_type`, `note_id`, `occurred_at` (no note body)
2. Verify Standard Webhooks signature + reject stale `webhook-timestamp`
3. Dedup retries on Redis `granola:webhook:event:{event_id}`
4. `GET /v1/notes/{id}` with `GRANOLA_API_KEY`
5. Date the note with meeting start when present (`calendar_event.scheduled_start_time`, or `meeting_start` / `meetingStartAt`), else `created_at`
6. Convert to `SYSTEM_TIMEZONE` and apply the 3am local rollover (`get_effective_date` / `DAY_ROLLOVER_HOUR=3`)
7. Write under `### Transcript Notes` on `01_Daily/_Journal/{Mon D, YYYY}.md`. Generated / access_granted skip an existing `<!-- granola:not_… -->` block; `note.edited` replaces that block in place (or inserts if missing)

**Manual pull (safety net)**

1. `GET https://public-api.granola.ai/v1/notes?updated_after=<ISO8601>` (cursor pagination; no `folder_id` filter)
2. For each listed note, always `GET /v1/notes/{id}` so `summary_markdown` is present (list payloads typically omit it)
3. Same dating / journal write as the webhook
4. Advance Redis `granola:notes:cursor` only when the pull and writes succeed

The Granola API only returns notes that already have an AI summary and transcript. The journal write uses **summary only** (`summary_markdown`, else `summary_text`) — never the full transcript. Missing journal files are skipped (logged as `skipped_missing_journal`) — the job does not create a journal or dump onto today.

## Prerequisites

- Dropbox configured with Obsidian vault access
- Redis running (cursor storage)
- A Granola API key ([Granola API](https://docs.granola.ai/help-center/sharing/integrations/granola-api))

## Environment Variables

Required / optional variables in `app/.env`:

```bash
# Required
GRANOLA_API_KEY=your_granola_api_key
GRANOLA_WEBHOOK_SECRET=whsec_your_granola_webhook_signing_secret
WEBHOOK_BASE_URL=https://your-ngrok-url.ngrok-free.app

# Dropbox + vault (existing)
DROPBOX_ACCESS_KEY=your_app_key
DROPBOX_ACCESS_SECRET=your_app_secret
DROPBOX_REFRESH_TOKEN=your_refresh_token
DROPBOX_OBSIDIAN_VAULT_PATH=/obsidian/personal

# Timezone (America/Los_Angeles in prod). Midnight–2:59am belongs to the previous journal day.
SYSTEM_TIMEZONE=America/Los_Angeles

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# Optional: override the list-notes updated_after filter (ISO8601 UTC)
# GRANOLA_NOTES_UPDATED_AFTER=2026-09-06T00:00:00Z

# Optional: empty-Redis seed lookback in minutes (default 15)
# GRANOLA_SEED_LOOKBACK_MINUTES=15

# Optional: manual backfill_granola_notes defaults (ignored by the webhook)
# GRANOLA_BACKFILL_UPDATED_AFTER=2026-01-01T00:00:00Z
# GRANOLA_BACKFILL_LOOKBACK_DAYS=30
# GRANOLA_BACKFILL_SINCE=2024-08-13
```

Never log or print `GRANOLA_API_KEY` or `GRANOLA_WEBHOOK_SECRET`.

## Redis cursor

| | |
| --- | --- |
| Key | `granola:notes:cursor` |
| Value | ISO8601 UTC of the last **successful** run start |

**First run / empty Redis (incremental job only):** `sync_granola_notes` seeds `updated_after` to now−15m (or `GRANOLA_SEED_LOOKBACK_MINUTES`). This avoids dumping the whole historical library. Documented here so an empty Redis is intentional, not a full backfill. The manual `backfill_granola_notes` job does **not** use that seed — see [Manual backfill](#manual-backfill-full-history). The webhook path does not use this cursor.

The cursor advances only after a successful pull + write (incremental **or** backfill). API errors or Dropbox write errors leave the cursor unchanged. Overlap is OK: blocks dedup on the Granola note `id` (`not_…` / `granola:not_…`).

## Schedule

Live updates use the webhook. Both pull jobs are year-2099 `CronTrigger`s in `app/scheduler.py` so `POST /scheduler/jobs/…/run` stays registered. Neither is on a cadence.

### Manual trigger (incremental safety net)

```bash
# Incremental from Redis cursor, or now−15m if Redis is empty
curl -X POST http://localhost:8000/scheduler/jobs/sync_granola_notes/run

# Optional explicit filter (ISO8601) — overrides cursor/seed for this run; success still advances the cursor
curl -X POST 'http://localhost:8000/scheduler/jobs/sync_granola_notes/run?updated_after=2026-09-06T00:00:00Z'
```

### Manual backfill (full history)

`backfill_granola_notes` is a year-2099 `CronTrigger` so `POST /scheduler/jobs/backfill_granola_notes/run` stays registered. It is **not** on a cadence.

Default (no query params): omit `updated_after` on `GET /v1/notes`. That filter is optional in the Granola API, so this pulls as much history as the API returns (paginated). It does **not** use the incremental empty-Redis now−15m seed, and it does **not** read `GRANOLA_NOTES_UPDATED_AFTER`.

Safe to re-run: writes reuse the same `format_granola_block` / journal helpers as the webhook and incremental job. Existing `<!-- granola:not_… -->` blocks are skipped; legacy title-only `- Title granola:not_…` lines for the same id are upgraded. Missing journals are skipped (`skipped_missing_journal`).

After a successful backfill, Redis `granola:notes:cursor` advances to that run's start (same as `sync_granola_notes`). A later incremental safety-net run continues from that point instead of reseeding now−15m.

```bash
# Full history (omit updated_after)
curl -X POST http://localhost:8000/scheduler/jobs/backfill_granola_notes/run

# Optional explicit list filter (ISO8601)
curl -X POST 'http://localhost:8000/scheduler/jobs/backfill_granola_notes/run?updated_after=2026-01-01T00:00:00Z'

# Optional lookback (sets updated_after to now minus N days UTC unless updated_after is explicit)
curl -X POST 'http://localhost:8000/scheduler/jobs/backfill_granola_notes/run?lookback_days=30'

# Optional journal-day cutoff (3am PT rollover). Notes dated before this day are not written.
# The list API is still update-cursor based — this is a client-side filter, not created_after.
curl -X POST 'http://localhost:8000/scheduler/jobs/backfill_granola_notes/run?since=2024-08-13'
```

Optional env overrides (same precedence as the query params; incremental `GRANOLA_*` vars are ignored):

```bash
# GRANOLA_BACKFILL_UPDATED_AFTER=2026-01-01T00:00:00Z
# GRANOLA_BACKFILL_LOOKBACK_DAYS=30
# GRANOLA_BACKFILL_SINCE=2024-08-13
```

### Check job status

```bash
curl http://localhost:8000/scheduler/jobs
```

## Journal write

- Path: `01_Daily/_Journal/{Mon D, YYYY}.md` via `journal_filename`, Dropbox, `_resolve_journal_folder`
- Section header exactly: `### Transcript Notes`
- Placement: bottom of the note. Missing heading is created at EOF. An existing mid-note heading is reused in place (not moved)
- Section bounds: once `### Transcript Notes` is found, the insert window runs to EOF (it is last in the daily template) unless a later **journal sibling** `###` header is present (`### Morning Pages`, `### Content Buffet:`, `### Content Planning`, `### Music of the Day`). Headings inside `summary_markdown` (`### Church and Spiritual Practice`, `# Title`, …) are note body, not section boundaries — they must not split the section or orphan a note's body when later notes are appended
- Block shape (summary as-is from the API; no transcript):

```markdown
### Transcript Notes

#### [Title](https://notes.granola.ai/d/…)
<!-- granola:not_… -->

<summary_markdown>

---

#### [Next title](https://notes.granola.ai/d/…)
<!-- granola:not_… -->

<summary_markdown>
```

- Prefer `summary_markdown`; fall back to `summary_text` if markdown is empty. Private notes are not written. Never write the full transcript.
- Separate notes with a horizontal rule (`---` with a blank line on each side). The first note has no leading `---`.
- Dedup key: `granola:not_…` inside the HTML comment. A second generated/access_granted/sync write with the same id is skipped when that comment is already present. `note.edited` replaces the existing block instead of skipping.
- Upgrade: a legacy title-only line (`- [Title](url) granola:not_…`) for the same id is replaced with the summary block on the next sync.

## Log summary

Each run logs: `selected`, `inserted`, `replaced`, `skipped`, `skipped_missing_journal`, `errors`, `cursor`, and the `updated_after` actually used. `replaced` is used by `note.edited` (in-place block swap); incremental/backfill title-only upgrades still count as `inserted`.

## Troubleshooting

### "GRANOLA_API_KEY not set"

- Set `GRANOLA_API_KEY` in `app/.env` (see `app/.env.example`)
- Restart the app so the scheduler picks up the env

### Job not appearing in scheduler

- Check logs for `Registered job: sync_granola_notes` and `Registered job: backfill_granola_notes`
- Both are year-2099 manual jobs (no `*/15` cadence)
- Verify the app started without import errors

### Notes not appearing in Obsidian

- Check logs for `Granola sync finished selected=N inserted=N …`
- `selected=0` usually means nothing newer than the cursor (or the now−15m seed)
- `skipped_missing_journal` means the journal file for that note's effective date does not exist
- Write or API errors leave the cursor in place so the next run retries

## Code location

- Webhook endpoint: `app/main.py` (`POST /granola/webhook`)
- Webhook verify / dedup / fetch+write: `app/services/granola/webhook.py`
- Client: `app/services/granola/client.py`
- Sync job: `app/services/granola/sync.py`
- Manual backfill: `app/services/granola/backfill.py`
- Scheduler entry: `app/scheduler.py`
