"""One-off Substack note migration (dry-run / apply)."""

import os
import sys
from unittest.mock import MagicMock, patch

import dropbox

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")
os.environ.setdefault("READWISE_TOKEN", "test-readwise-token")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from services.obsidian.add_readwise_buffet import clear_book_cache  # noqa: E402
from services.obsidian.migrate_substack_note import (  # noqa: E402
    KNOWN_NOTE_NAME,
    _replace_journal_wikilink,
    build_plan,
    process_note,
    run_migrate_substack_note,
    trash_folder_path,
)
from services.obsidian.utils.substack_note import (  # noqa: E402
    BOOKMARKED_SUBSTACK_NOTES_HEADER,
    SUBSTACK_NOTE_TITLE_SEPARATOR,
    clear_substack_title_cache,
)

KH = "/obsidian/personal/05_Knowledge-Hub"
JOURNAL = "/obsidian/personal/01_Daily/_Journal/Nov 20, 2026.md"
OLD_NOTE = f"{KH}/{KNOWN_NOTE_NAME}"
AUTHOR_PAGE = f"{KH}/Substack Notes from Celine Nguyen.md"
CELINE_URL = "https://substack.com/@celinenguyen/note/c-390742430"
AI_TITLE = "Taste And Judgment"
DOC_ID = "01substackcelinenote001"
REV = "aaaaaaaaaaaaaaaa"

OLD_NOTE_CONTENT = f"""---
title: "Celine Nguyen's comment by Celine Nguyen"
author: "[[Celine Nguyen]]"
URL: {CELINE_URL}
readwise_id: {DOC_ID}
readwise_url: https://read.readwise.io/read/{DOC_ID}
---

# Celine Nguyen's comment by Celine Nguyen

Most design advice is really taste advice dressed up as process. Judgment is the whole job.
"""

OLD_JOURNAL = """---
date: 2026-11-20
---

### Content Buffet:
- [[Celine Nguyen's comment by Celine Nguyen]]

### Content Planning
- plan something
"""


def _download_with_rev(content: str, *, rev: str = REV, path: str | None = None):
    metadata = MagicMock()
    metadata.rev = rev
    metadata.path_display = path
    response = MagicMock()
    response.content = content.encode("utf-8")
    return metadata, response


def _not_found():
    return dropbox.exceptions.ApiError("req", MagicMock(), "", "")


def _mock_vault_dbx(files_by_path=None):
    store = dict(files_by_path or {})
    uploaded = []
    moved = []
    mock_dbx = MagicMock()

    def download(path):
        if path not in store:
            raise FileNotFoundError(f"not found: {path}")
        return _download_with_rev(store[path], path=path)

    def upload(data, path, mode=None, autorename=None):
        text = data.decode("utf-8")
        store[path] = text
        uploaded.append({"path": path, "content": text, "mode": mode, "autorename": autorename})

    def list_folder(path, recursive=False):
        result = MagicMock()
        result.has_more = False
        prefix = path.rstrip("/") + "/"
        entries = []
        for file_path in store:
            if not file_path.startswith(prefix):
                continue
            name = file_path[len(prefix) :]
            if "/" in name:
                continue
            entry = MagicMock()
            entry.name = name
            entry.path_lower = file_path
            entry.path_display = file_path
            entries.append(entry)
        result.entries = entries
        return result

    def search_v2(query, options=None, **_kwargs):
        result = MagicMock()
        result.has_more = False
        matches = []
        q = str(query or "")
        scope = getattr(options, "path", None) if options is not None else None
        prefix = (str(scope).rstrip("/") + "/") if isinstance(scope, str) and scope else None
        for file_path, content in store.items():
            if prefix and not (
                file_path == str(scope).rstrip("/") or file_path.startswith(prefix)
            ):
                continue
            name = file_path.rsplit("/", 1)[-1]
            if not name.endswith(".md"):
                continue
            if q not in f"{file_path}\n{content}":
                continue
            match = MagicMock()
            meta = MagicMock()
            meta.name = name
            meta.path_display = file_path
            meta.path_lower = file_path
            match.metadata.get_metadata.return_value = meta
            matches.append(match)
        result.matches = matches
        return result

    def get_metadata(path):
        if path not in store:
            raise _not_found()
        meta = MagicMock()
        meta.path_display = path
        meta.path_lower = path
        meta.name = path.rsplit("/", 1)[-1]
        return meta

    def move_v2(from_path, to_path, autorename=None):
        if from_path not in store:
            raise FileNotFoundError(from_path)
        if to_path in store:
            raise _not_found()
        store[to_path] = store.pop(from_path)
        moved.append({"from": from_path, "to": to_path, "autorename": autorename})

    mock_dbx.files_download.side_effect = download
    mock_dbx.files_upload.side_effect = upload
    mock_dbx.files_list_folder.side_effect = list_folder
    mock_dbx.files_search_v2.side_effect = search_v2
    mock_dbx.files_get_metadata.side_effect = get_metadata
    mock_dbx.files_move_v2.side_effect = move_v2
    mock_dbx.files_create_folder_v2.return_value = MagicMock()
    return mock_dbx, uploaded, store, moved


def _folder_by_suffix(_dbx, _parent, suffix):
    return {
        "_Daily": "/obsidian/personal/01_Daily",
        "_Journal": "/obsidian/personal/01_Daily/_Journal",
        "_Knowledge-Hub": KH,
    }[suffix]


def test_replace_journal_wikilink_repoints_standalone_buffet_line():
    new_line = (
        f"- [[Substack Notes from Celine Nguyen]]: {AI_TITLE} "
        f"([Link](https://read.readwise.io/read/{DOC_ID}))"
    )
    updated, action = _replace_journal_wikilink(
        OLD_JOURNAL, "Celine Nguyen's comment by Celine Nguyen", new_line
    )
    assert action == "replaced"
    assert "[[Celine Nguyen's comment by Celine Nguyen]]" not in updated
    assert new_line in updated


def test_dry_run_prints_plan_and_does_not_write(tmp_path):
    clear_book_cache()
    clear_substack_title_cache()
    mock_dbx, uploaded, store, moved = _mock_vault_dbx({
        OLD_NOTE: OLD_NOTE_CONTENT,
        JOURNAL: OLD_JOURNAL,
    })
    with patch("services.obsidian.migrate_substack_note._get_dropbox_client", return_value=mock_dbx), \
        patch("services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx), \
        patch("services.obsidian.add_readwise_buffet._find_folder_by_suffix", side_effect=_folder_by_suffix), \
        patch("services.obsidian.add_readwise_buffet._resolve_knowledge_hub_folder", return_value=KH), \
        patch("services.obsidian.migrate_substack_note._resolve_knowledge_hub_folder", return_value=KH), \
        patch("services.obsidian.migrate_substack_note._resolve_journal_folder", return_value="/obsidian/personal/01_Daily/_Journal"), \
        patch("services.obsidian.utils.substack_note._openai_substack_summary", return_value=AI_TITLE), \
        patch("services.readwise.reader.update_document_title") as mock_patch:
        counts = run_migrate_substack_note(
            apply=False, dbx=mock_dbx, paths=[OLD_NOTE], backup_dir=tmp_path
        )

    assert counts["would_migrate"] == 1
    assert uploaded == []
    assert moved == []
    assert OLD_NOTE in store
    assert AUTHOR_PAGE not in store
    mock_patch.assert_not_called()
    plan = build_plan(
        mock_dbx, OLD_NOTE, journal_folder="/obsidian/personal/01_Daily/_Journal"
    )
    assert plan.full_title == f"{AI_TITLE}{SUBSTACK_NOTE_TITLE_SEPARATOR}Celine Nguyen"
    assert plan.author_page_title == "Substack Notes from Celine Nguyen"
    assert plan.archive_path.startswith(trash_folder_path())
    assert JOURNAL in plan.journal_paths


def test_apply_migrates_page_journal_archive_and_patch(tmp_path):
    clear_book_cache()
    clear_substack_title_cache()
    mock_dbx, uploaded, store, moved = _mock_vault_dbx({
        OLD_NOTE: OLD_NOTE_CONTENT,
        JOURNAL: OLD_JOURNAL,
    })
    with patch("services.obsidian.migrate_substack_note._get_dropbox_client", return_value=mock_dbx), \
        patch("services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx), \
        patch("services.obsidian.add_readwise_buffet._find_folder_by_suffix", side_effect=_folder_by_suffix), \
        patch("services.obsidian.add_readwise_buffet._resolve_knowledge_hub_folder", return_value=KH), \
        patch("services.obsidian.migrate_substack_note._resolve_knowledge_hub_folder", return_value=KH), \
        patch("services.obsidian.migrate_substack_note._resolve_journal_folder", return_value="/obsidian/personal/01_Daily/_Journal"), \
        patch("services.obsidian.utils.substack_note._openai_substack_summary", return_value=AI_TITLE), \
        patch(
            "services.readwise.reader.update_document_title",
            return_value={"success": True, "error": None, "status_code": 200},
        ) as mock_patch:
        counts = run_migrate_substack_note(
            apply=True, dbx=mock_dbx, paths=[OLD_NOTE], backup_dir=tmp_path
        )

    assert counts["migrated"] == 1
    assert OLD_NOTE not in store
    archive = f"{trash_folder_path()}/{KNOWN_NOTE_NAME}"
    assert archive in store
    assert AUTHOR_PAGE in store
    page = store[AUTHOR_PAGE]
    assert BOOKMARKED_SUBSTACK_NOTES_HEADER in page
    assert f"- {AI_TITLE}" in page
    assert CELINE_URL in page
    journal = store[JOURNAL]
    assert "[[Celine Nguyen's comment by Celine Nguyen]]" not in journal
    assert f"[[Substack Notes from Celine Nguyen]]: {AI_TITLE}" in journal
    mock_patch.assert_called_once()
    assert mock_patch.call_args.args[0] == DOC_ID
    assert SUBSTACK_NOTE_TITLE_SEPARATOR in mock_patch.call_args.args[1]
    assert any(item["from"] == OLD_NOTE for item in moved)
    backups = list(tmp_path.iterdir())
    assert backups
    assert any(u["path"] == AUTHOR_PAGE for u in uploaded)
    assert any(u["mode"].is_update() for u in uploaded if u["path"] == JOURNAL)


def test_apply_does_not_overwrite_existing_archive(tmp_path):
    clear_book_cache()
    clear_substack_title_cache()
    archive = f"{trash_folder_path()}/{KNOWN_NOTE_NAME}"
    mock_dbx, _uploaded, store, moved = _mock_vault_dbx({
        OLD_NOTE: OLD_NOTE_CONTENT,
        JOURNAL: OLD_JOURNAL,
        archive: "already archived",
    })
    with patch("services.obsidian.add_readwise_buffet._get_dropbox_client", return_value=mock_dbx), \
        patch("services.obsidian.add_readwise_buffet._find_folder_by_suffix", side_effect=_folder_by_suffix), \
        patch("services.obsidian.add_readwise_buffet._resolve_knowledge_hub_folder", return_value=KH), \
        patch("services.obsidian.utils.substack_note._openai_substack_summary", return_value=AI_TITLE), \
        patch(
            "services.readwise.reader.update_document_title",
            return_value={"success": True, "error": None, "status_code": 200},
        ):
        action, plan = process_note(
            mock_dbx,
            OLD_NOTE,
            apply=True,
            hub_path=KH,
            journal_folder="/obsidian/personal/01_Daily/_Journal",
            backup_dir=tmp_path,
        )

    assert plan.archive_path != archive
    assert "already archived" in store[archive]
    assert action in {"migrated", "skipped_archive_exists"}
    if action == "migrated":
        assert store.get(archive) == "already archived"
        assert any(item["to"] != archive for item in moved)
