#!/usr/bin/env python3
"""One-off Knowledge Hub article-highlights relocation (dry-run by default).

Moves an existing ``### Article highlights`` section from under a
*verified* H1 / H2 title (YAML ``title``, filename stem, or hub
``Title by Author`` stem; exact or casefold, plus a ≥12-character
word-boundary prefix when ``readwise_id`` / ``readwise_url`` is present)
to above that title. Scraped in-body headings that do not match are not
treated as the title. Book / tweet / transcript sections are not changed.
Writes are rev-safe Dropbox updates; ``--apply`` is required to upload.

Usage (from ``app/``, or via docker exec into the app container):

    uv run python -m services.obsidian.relocate_article_highlights
    uv run python -m services.obsidian.relocate_article_highlights --apply

    uv run python scripts/relocate_article_highlights.py
    uv run python scripts/relocate_article_highlights.py --apply

    docker compose exec app uv run python -m services.obsidian.relocate_article_highlights
    docker compose exec app uv run python -m services.obsidian.relocate_article_highlights --apply
"""

from services.obsidian.relocate_article_highlights import main

if __name__ == "__main__":
    raise SystemExit(main())
