#!/usr/bin/env python3
"""One-off Tweets-from-@handle ``X Profile`` YAML backfill (dry-run by default).

Adds ``X Profile: "https://x.com/{handle}"`` to existing Knowledge Hub notes
named ``Tweets from @*.md``. Writes are rev-safe; ``--apply`` is required
to upload. Does not append to the daily journal.

Usage (from ``app/``, or via docker exec into the app container):

    uv run python -m services.obsidian.backfill_tweet_x_profile
    uv run python -m services.obsidian.backfill_tweet_x_profile --apply

    uv run python scripts/backfill_tweet_x_profile.py
    uv run python scripts/backfill_tweet_x_profile.py --apply

    docker compose exec app uv run python -m services.obsidian.backfill_tweet_x_profile
    docker compose exec app uv run python -m services.obsidian.backfill_tweet_x_profile --apply
"""

from services.obsidian.backfill_tweet_x_profile import main

if __name__ == "__main__":
    raise SystemExit(main())
