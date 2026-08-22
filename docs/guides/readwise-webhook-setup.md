# Readwise Webhook Setup

Point Readwise at `POST {WEBHOOK_BASE_URL}/readwise/webhook` and set the shared secret as `READWISE_WEBHOOK_SECRET` (Readwise sends it in the JSON body as `secret`). Set `READWISE_TOKEN` from https://readwise.io/access_token so highlight book titles can be resolved.

Subscribe to both:

- `readwise.highlight.created` — highlight quotes under `### Content Buffet:`
- `reader.any_document.created` — Reader documents as `- [Title](https://read.readwise.io/read/{id})`

Highlights are written as:

```
- [[Title - Author]]: ["quote"](https://readwise.io/open/{id})
```

The wikilink target is `Title - Author` using the Readwise book title and `author` string as-is (no `bookreview` URL, no `(Book)` suffix, no name reordering). Multiple authors stay in Readwise order, e.g. `[[Zero to One - Peter Thiel, Blake Masters]]`. Author is omitted when missing (`[[Title]]`). The highlight permalink stays on the quote.

Reader documents stay markdown links (not wikilinks): `- [Title](https://read.readwise.io/read/{id})`, with an optional short ` — Author` in plain text.

`reader.any_document.created` includes RSS and newsletter feed items as well as documents you save yourself. Those days will be logged too. If you only want manually saved documents, subscribe to `reader.non_feed_document.created` instead (this endpoint also accepts `reader.feed_document.created` and treats them the same).

Highlights are dated by `highlighted_at` (3am local rollover). Documents are dated by `created_at`, then `saved_at`, then `updated_at`. Missing journal files are skipped, not created.

## Backfill historical highlights

Readwise does not refire webhooks for historical imports. Use the repeatable `backfill_readwise_highlights` job to pull from [GET /api/v2/export/](https://readwise.io/api_deets) and append the same Content Buffet bullets. Safe to rerun: existing highlight id / `readwise.io/open/{id}` bullets are skipped. Missing journal files are skipped (never dumped onto today). Writes are batched per journal day.

```bash
# Default since=2024-08-13 (first day of the unbroken daily journal streak)
curl -X POST http://localhost:8000/scheduler/jobs/backfill_readwise_highlights/run

# Optional cutoff (ISO date) and incremental export filter (ISO8601)
curl -X POST 'http://localhost:8000/scheduler/jobs/backfill_readwise_highlights/run?since=2024-08-13&updated_after=2026-01-01T00:00:00Z'
```

Defaults can also be set as `READWISE_BACKFILL_SINCE` and `READWISE_BACKFILL_UPDATED_AFTER`. The job is manual (not on a daily cadence). Check counts in the app log: `selected`, `inserted`, `replaced`, `skipped` (dedup), `skipped_missing_journal`, `files_written`, `errors`.
