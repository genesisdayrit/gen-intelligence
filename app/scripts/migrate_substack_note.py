#!/usr/bin/env python3
"""One-off Substack note migration (dry-run by default).

Moves already-ingested Substack notes onto ``Substack Notes from {author}``
pages, rewrites journal wikilinks, archives the old KH note, and PATCHes
the Reader title. Writes are rev-safe; ``--apply`` is required to mutate
Dropbox.

Usage (from ``app/``, or via docker exec into the app container):

    uv run python -m services.obsidian.migrate_substack_note
    uv run python -m services.obsidian.migrate_substack_note --apply

    uv run python scripts/migrate_substack_note.py
    uv run python scripts/migrate_substack_note.py --apply

    docker compose exec app uv run python -m services.obsidian.migrate_substack_note
    docker compose exec app uv run python -m services.obsidian.migrate_substack_note --apply
"""

from services.obsidian.migrate_substack_note import main

if __name__ == "__main__":
    raise SystemExit(main())
