#!/usr/bin/env python3
"""Seed Redis book_id → Knowledge Hub path_display from KH YAML.

Usage (from ``app/``, or via docker exec into the app container):

    uv run python -m services.obsidian.seed_readwise_book_paths
    uv run python scripts/seed_readwise_book_paths.py
"""

from services.obsidian.seed_readwise_book_paths import main

if __name__ == "__main__":
    raise SystemExit(main())
