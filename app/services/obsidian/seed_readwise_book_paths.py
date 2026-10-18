"""Seed Redis ``readwise:kh:book:<id>`` from Knowledge Hub YAML.

Walks ``*_Knowledge-Hub`` markdown notes, reads ``readwise_book_id``, and
stores ``book_id → path_display``. Manual-only — the live writers also
update this map after a successful resolve. Safe to re-run.

Usage (from ``app/``):

    uv run python -m services.obsidian.seed_readwise_book_paths
    uv run python scripts/seed_readwise_book_paths.py
"""

from __future__ import annotations

import argparse
import logging
import os

import dropbox
from dotenv import load_dotenv

from services.obsidian.add_readwise_buffet import _get_dropbox_client
from services.obsidian.add_shared_link import _extract_frontmatter, _find_knowledge_hub_path
from services.obsidian.utils.readwise_concurrency import set_hub_path_for_book_id

load_dotenv()

logger = logging.getLogger(__name__)


def _iter_knowledge_hub_md(dbx: dropbox.Dropbox, kh_path: str):
    result = dbx.files_list_folder(kh_path, recursive=True)
    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata):
                continue
            name = getattr(entry, "name", "") or ""
            if not name.lower().endswith(".md"):
                continue
            path = getattr(entry, "path_display", None) or getattr(
                entry, "path_lower", None
            )
            if path:
                yield name, path
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)


def seed_readwise_book_paths(*, dbx: dropbox.Dropbox | None = None) -> dict:
    """Scan KH YAML and write ``book_id → path_display`` into Redis."""
    client = dbx if dbx is not None else _get_dropbox_client()
    vault_path = os.getenv("DROPBOX_OBSIDIAN_VAULT_PATH")
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")
    kh_path = _find_knowledge_hub_path(client, vault_path)
    summary = {"notes_scanned": 0, "seeded": 0, "skipped": 0}
    for _name, path in _iter_knowledge_hub_md(client, kh_path):
        summary["notes_scanned"] += 1
        try:
            _meta, response = client.files_download(path)
            content = response.content.decode("utf-8")
        except Exception:
            logger.warning("Seed skipped; download failed path=%s", path)
            summary["skipped"] += 1
            continue
        frontmatter, _body = _extract_frontmatter(content)
        book_id = frontmatter.get("readwise_book_id")
        if book_id is None or book_id == "":
            summary["skipped"] += 1
            continue
        set_hub_path_for_book_id(str(book_id).strip(), path)
        summary["seeded"] += 1
        logger.info("Seeded readwise_book_id=%s path=%s", book_id, path)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed Redis book_id → Knowledge Hub path_display from YAML."
    )
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    summary = seed_readwise_book_paths()
    print(
        f"notes_scanned={summary['notes_scanned']} "
        f"seeded={summary['seeded']} skipped={summary['skipped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
