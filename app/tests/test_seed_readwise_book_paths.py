"""seed_readwise_book_paths --dry-run must not write Redis."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")

from services.obsidian.seed_readwise_book_paths import (  # noqa: E402
    main,
    seed_readwise_book_paths,
)

KH_PATH = "/obsidian/personal/01_knowledge-hub"
NOTE_PATH = f"{KH_PATH}/Essay.md"
NOTE = """---
readwise_book_id: 8237
---

# Essay
"""


def _mock_kh_dbx():
    mock_dbx = MagicMock()
    result = MagicMock()
    result.has_more = False
    entry = MagicMock()
    entry.name = "Essay.md"
    entry.path_display = NOTE_PATH
    entry.path_lower = NOTE_PATH
    result.entries = [entry]
    mock_dbx.files_list_folder.return_value = result
    response = MagicMock()
    response.content = NOTE.encode("utf-8")
    mock_dbx.files_download.return_value = (None, response)
    return mock_dbx


def test_seed_dry_run_does_not_write_redis():
    mock_dbx = _mock_kh_dbx()
    with patch(
        "services.obsidian.seed_readwise_book_paths._find_knowledge_hub_path",
        return_value=KH_PATH,
    ), patch(
        "services.obsidian.seed_readwise_book_paths.set_hub_path_for_book_id"
    ) as mock_set:
        summary = seed_readwise_book_paths(dbx=mock_dbx, dry_run=True)

    mock_set.assert_not_called()
    assert summary["notes_scanned"] == 1
    assert summary["seeded"] == 1
    assert summary["dry_run"] is True


def test_seed_writes_redis_when_not_dry_run():
    mock_dbx = _mock_kh_dbx()
    with patch(
        "services.obsidian.seed_readwise_book_paths._find_knowledge_hub_path",
        return_value=KH_PATH,
    ), patch(
        "services.obsidian.seed_readwise_book_paths.set_hub_path_for_book_id"
    ) as mock_set:
        summary = seed_readwise_book_paths(dbx=mock_dbx, dry_run=False)

    mock_set.assert_called_once_with("8237", NOTE_PATH)
    assert summary["dry_run"] is False


def test_main_dry_run_flag(capsys):
    with patch(
        "services.obsidian.seed_readwise_book_paths.seed_readwise_book_paths",
        return_value={
            "notes_scanned": 1,
            "seeded": 1,
            "skipped": 0,
            "dry_run": True,
        },
    ) as mock_seed:
        assert main(["--dry-run"]) == 0
    mock_seed.assert_called_once_with(dry_run=True)
    assert "dry-run" in capsys.readouterr().out
