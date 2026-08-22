# Readwise Webhook Setup

Point Readwise at `POST {WEBHOOK_BASE_URL}/readwise/webhook` and set the shared secret as `READWISE_WEBHOOK_SECRET` (Readwise sends it in the JSON body as `secret`). Set `READWISE_TOKEN` from https://readwise.io/access_token so highlight book titles can be resolved. Only `readwise.highlight.created` events are written to the Obsidian daily journal for `highlighted_at` (3am local rollover) under `### Content Buffet:`. Missing journal files are skipped, not created.
