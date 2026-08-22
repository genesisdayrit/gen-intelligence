# Readwise Webhook Setup

Point Readwise at `POST {WEBHOOK_BASE_URL}/readwise/webhook` and set the shared secret as `READWISE_WEBHOOK_SECRET` (Readwise sends it in the JSON body as `secret`). Set `READWISE_TOKEN` from https://readwise.io/access_token so highlight book titles can be resolved. Only `readwise.highlight.created` events are written to the Obsidian daily journal for `highlighted_at` (3am local rollover) under `### Content Buffet:`. Missing journal files are skipped, not created.

## Backfill historical highlights

Readwise does not refire webhooks for historical imports. Use the repeatable `backfill_readwise_highlights` job to pull from [GET /api/v2/export/](https://readwise.io/api_deets) and append the same Content Buffet bullets. Safe to rerun: existing highlight id / `readwise.io/open/{id}` bullets are skipped. Missing journal files are skipped (never dumped onto today). Writes are batched per journal day.

```bash
# Default since=2024-08-13 (first day of the unbroken daily journal streak)
curl -X POST http://localhost:8000/scheduler/jobs/backfill_readwise_highlights/run

# Optional cutoff (ISO date) and incremental export filter (ISO8601)
curl -X POST 'http://localhost:8000/scheduler/jobs/backfill_readwise_highlights/run?since=2024-08-13&updated_after=2026-01-01T00:00:00Z'
```

Defaults can also be set as `READWISE_BACKFILL_SINCE` and `READWISE_BACKFILL_UPDATED_AFTER`. The job is manual (not on a daily cadence). Check counts in the app log: `selected`, `inserted`, `replaced`, `skipped` (dedup), `skipped_missing_journal`, `files_written`, `errors`.
