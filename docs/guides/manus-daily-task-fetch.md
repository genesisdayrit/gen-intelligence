# Manus Daily Task Fetch

Automatically fetch Manus AI tasks created today and write them to your Obsidian Daily Action and Weekly Cycle notes.

## Overview

A scheduled job that runs every 30 minutes, fetching tasks from the Manus API for the current effective calendar day and upserting them into both Daily Action and Weekly Cycle pages in Obsidian via Dropbox.

1. Fetches all Manus tasks via `GET /v1/tasks`
2. Filters to tasks created during the current effective day (3am rollover)
3. Upserts each task into the Daily Action note
4. Upserts each task into the Weekly Cycle note
5. Deduplicates by task URL so re-running is safe

This supplements the Manus webhook handler (which only fires for API-created tasks) by also capturing tasks created from the browser at manus.im.

## Prerequisites

- Dropbox configured with Obsidian vault access
- Redis running for access token caching
- A Manus API key (get from [manus.im](https://manus.im))
- Daily Action and Weekly Cycle notes following the expected folder structure

## Environment Variables

Required variables in your `.env`:

```bash
# Manus AI
MANUS_API_KEY=your_manus_api_key

# Dropbox OAuth credentials
DROPBOX_ACCESS_KEY=your_app_key
DROPBOX_ACCESS_SECRET=your_app_secret
DROPBOX_REFRESH_TOKEN=your_refresh_token

# Path to Obsidian vault in Dropbox
DROPBOX_OBSIDIAN_VAULT_PATH=/Your_Vault

# Timezone (default: US/Eastern)
SYSTEM_TIMEZONE=US/Pacific

# Redis configuration
REDIS_HOST=localhost
REDIS_PORT=6379
```

## Schedule

The job is registered in `app/scheduler.py` with a `CronTrigger(minute="*/30")`, which fires at `:00` and `:30` of every hour (48 times per day).

### Manual Trigger

You can trigger the job immediately via the API:

```bash
curl -X POST http://localhost:8000/scheduler/jobs/fetch_manus_tasks/run
```

### Check Job Status

```bash
curl http://localhost:8000/scheduler/jobs
```

## Output Format

The job writes entries to the `### Manus Tasks:` section in Daily Action notes and `##### Manus Tasks:` in Weekly Cycle notes.

Each entry follows this format:

```markdown
- Comprehensive Research on Topics ([RyagLvAZPLQwCdBT5m3nZj](https://manus.im/app/RyagLvAZPLQwCdBT5m3nZj))
```

## How It Works

### Effective Date

The system uses a 3-hour day rollover: tasks created between midnight and 3am are attributed to the previous day. This is handled by `get_effective_date()` in `services/obsidian/utils/date_helpers.py`.

### Manus API

The job calls `GET https://api.manus.ai/v1/tasks` with:
- `API_KEY` header for authentication
- `limit`: 20 per page
- Pagination via `last_id` cursor

Tasks are returned newest-first. The job stops paginating once it hits tasks older than today's effective date.

### Deduplication

The downstream `upsert_manus_task_touched()` function checks for existing task URLs in the note. If a task was already written (by a previous fetch cycle or by the webhook handler), it is skipped.

### Interaction with Webhook Handler

Both the cron job and the Manus webhook handler (`POST /manus/webhook`) call the same `upsert_manus_task_touched()` function. The webhook provides real-time updates for API-created tasks, while the cron job catches browser-created tasks. URL-based deduplication ensures no duplicates.

## Troubleshooting

### "MANUS_API_KEY not set"

- Ensure `MANUS_API_KEY` is set in your `.env` file
- Verify the key is valid at [manus.im](https://manus.im)

### Job not appearing in scheduler

- Check the server logs for `Registered job: fetch_manus_tasks`
- Verify the app started without import errors

### Tasks not appearing in Obsidian

- Check logs for `Manus task fetch complete: found=N, upserted=N, errors=N`
- If `found=0`, verify you have Manus tasks created today
- If errors are present, check Dropbox credentials and vault path

### Token Refresh Issues

The system automatically refreshes Dropbox tokens using Redis caching. If you see token errors:

```bash
# Check Redis is running
docker compose logs redis

# Clear cached token to force refresh
redis-cli DEL DROPBOX_ACCESS_TOKEN
```

## Code Location

- Fetch service: `app/services/manus/fetch_manus_tasks.py`
- Obsidian writer: `app/services/obsidian/add_manus_task_touched.py`
- Scheduler entry: `app/scheduler.py`
