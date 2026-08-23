# Readwise Webhook Setup

Point Readwise at `POST {WEBHOOK_BASE_URL}/readwise/webhook` and set the shared secret as `READWISE_WEBHOOK_SECRET` (Readwise sends it in the JSON body as `secret`). Set `READWISE_TOKEN` from https://readwise.io/access_token so highlight book titles can be resolved.

Subscribe to both:

- `readwise.highlight.created` — highlight quotes under `### Content Buffet:`
- `reader.any_document.created` — parent Reader documents as a Knowledge Hub note plus `- [[Note Title]]`

When a parent Reader document is created, the webhook creates or updates a Knowledge Hub note the same way as a share-link (YouTube URLs use the YouTube helper). The journal date on the note and buffet is the document's 3am-aware date (`created_at` / `saved_at` / `updated_at`), not "now". The buffet line is **only** the standalone wikilink `- [[Note Title]]` (the KH filename stem) — no quote and no nested source / readwise / author / published / saved bullets. Later `highlight.created` events **append** a separate line and do not replace or remove the standalone wikilink. Child annotation documents (`category=highlight|note` or `parent_id` set) are still skipped. If KH create fails, the webhook logs it and does not crash; it does not fall back to a markdown Reader URL unless KH was skipped for a junk/empty title.

Reader-created Knowledge Hub notes also write `URL`, `author`, `readwise_id`, `readwise_url`, `published`, and `saved_at` when those fields exist on the document payload. Regular iOS share-link / YouTube saves omit the Readwise keys.

Locked Content Buffet shape:

```
- [[Note Title]]
- [[Note Title]]: ["quote"](https://readwise.io/open/{id})
```

Standalone lines dedup on the exact wikilink bullet (`- [[Note Title]]`). Highlight lines dedup on `readwise.io/open/{id}`. The two never collapse into each other. Same-day skip does not duplicate the wikilink.

The wikilink target is the same Knowledge Hub stem as the document save (`_sanitize_filename` then wikilink sanitize). Do not use `[[Title by Author]]` or a `readwise.io/bookreview` / `read.readwise.io` title link when a stem exists. If there is no title/stem, keep the quote-only or author-only fallback. Tweet sources (`category=tweets`, `source=twitter`, a `Tweets From …` title, `@handle on Twitter` author, or a twitter.com / x.com `source_url`) still use `[[Tweets from @handle]]`, with the handle taken from the author or the last path segment of `source_url`. Do not put `by` on tweets. The highlight permalink stays on the quote. Dedup remains `readwise.io/open/{id}`.

`reader.any_document.created` includes RSS and newsletter feed items as well as documents you save yourself. Those days will be logged too. If you only want manually saved documents, subscribe to `reader.non_feed_document.created` instead (this endpoint also accepts `reader.feed_document.created` and treats them the same). Reader also models highlights and notes as documents (`category=highlight|note`, or `parent_id` set); those are skipped so they do not duplicate `readwise.highlight.created` lines.

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
