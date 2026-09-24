#!/usr/bin/env python3
"""One-off Knowledge Hub article-highlights relocation (dry-run by default).

Moves an existing ``### Article highlights`` section from under the first
H1 / H2 title to above that title. Book / tweet / transcript sections are
not changed. Writes are rev-safe Dropbox updates; ``--apply`` is required
to upload.

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
