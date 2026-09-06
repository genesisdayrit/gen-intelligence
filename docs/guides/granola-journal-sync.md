# Granola → Obsidian Journal Sync

Every 15 minutes, pull Granola notes updated since a Redis last-run cursor and append each **summary block** under `### Transcript Notes` on the matching daily journal.

## Overview

1. `GET https://public-api.granola.ai/v1/notes?updated_after=<ISO8601>` (cursor pagination; no `folder_id` filter)
2. For each listed note, always `GET /v1/notes/{id}` so `summary_markdown` is present (list payloads typically omit it)
3. Date each note with meeting start when present (`calendar_event.scheduled_start_time`, or `meeting_start` / `meetingStartAt`), else `created_at`
4. Convert to `SYSTEM_TIMEZONE` and apply the 3am local rollover (`get_effective_date` / `DAY_ROLLOVER_HOUR=3`)
5. Append an idempotent summary block under `### Transcript Notes` on `01_Daily/_Journal/{Mon D, YYYY}.md`
6. Advance Redis `granola:notes:cursor` only when the pull and writes succeed

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
```

Never log or print `GRANOLA_API_KEY`.

## Redis cursor

| | |
| --- | --- |
| Key | `granola:notes:cursor` |
| Value | ISO8601 UTC of the last **successful** run start |

**First run / empty Redis:** seed `updated_after` to now−15m (or `GRANOLA_SEED_LOOKBACK_MINUTES`). This avoids dumping the whole historical library. Documented here so an empty Redis is intentional, not a full backfill.

The cursor advances only after a successful pull + write. API errors or Dropbox write errors leave the cursor unchanged. Overlap is OK: blocks dedup on the Granola note `id` (`not_…` / `granola:not_…`).

## Schedule

Registered in `app/scheduler.py` as `sync_granola_notes` with `CronTrigger(minute="*/15", timezone=SYSTEM_TZ)` — a real 15-minute cadence, unlike the year-2099 manual Readwise backfill.

### Manual trigger

```bash
# Incremental from Redis cursor, or now−15m if Redis is empty
curl -X POST http://localhost:8000/scheduler/jobs/sync_granola_notes/run

# Optional explicit filter (ISO8601) — overrides cursor/seed for this run; success still advances the cursor
curl -X POST 'http://localhost:8000/scheduler/jobs/sync_granola_notes/run?updated_after=2026-09-06T00:00:00Z'
```

### Check job status

```bash
curl http://localhost:8000/scheduler/jobs
```

## Journal write

- Path: `01_Daily/_Journal/{Mon D, YYYY}.md` via `journal_filename`, Dropbox, `_resolve_journal_folder`
- Section header exactly: `### Transcript Notes`
- Placement: bottom of the note. Missing heading is created at EOF. An existing mid-note heading is reused in place (not moved)
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
- Dedup key: `granola:not_…` inside the HTML comment. A second run with the same id is skipped when that comment is already present.
- Upgrade: a legacy title-only line (`- [Title](url) granola:not_…`) for the same id is replaced with the summary block on the next sync.

## Log summary

Each run logs: `selected`, `inserted`, `skipped`, `skipped_missing_journal`, `errors`, `cursor`, and the `updated_after` actually used.

## Troubleshooting

### "GRANOLA_API_KEY not set"

- Set `GRANOLA_API_KEY` in `app/.env` (see `app/.env.example`)
- Restart the app so the scheduler picks up the env

### Job not appearing in scheduler

- Check logs for `Registered job: sync_granola_notes`
- Verify the app started without import errors

### Notes not appearing in Obsidian

- Check logs for `Granola sync finished selected=N inserted=N …`
- `selected=0` usually means nothing newer than the cursor (or the now−15m seed)
- `skipped_missing_journal` means the journal file for that note's effective date does not exist
- Write or API errors leave the cursor in place so the next run retries

## Code location

- Client: `app/services/granola/client.py`
- Sync job: `app/services/granola/sync.py`
- Scheduler entry: `app/scheduler.py`
