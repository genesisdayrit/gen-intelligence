"""Tests for Daily Action creation from the vault template.

The live template is body-only at
`*_Templates/daily-templates/daily_action_properties.md`
(e.g. `/obsidian/personal/15_Templates/daily-templates/daily_action_properties.md`).
"""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import dropbox
import pytz
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SYSTEM_TIMEZONE", "America/Los_Angeles")
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")

from scripts.obsidian.workflows.file_creation import create_daily_action as mod

MODULE = "scripts.obsidian.workflows.file_creation.create_daily_action"
LA = pytz.timezone("America/Los_Angeles")
FIXED_NOW = LA.localize(datetime(2026, 9, 16, 18, 5))

VAULT = "/obsidian/personal"
TEMPLATES_FOLDER = "/obsidian/personal/15_templates"
DAILY_TEMPLATES_FOLDER = "/obsidian/personal/15_templates/daily-templates"
TEMPLATE_PATH = (
    "/obsidian/personal/15_templates/daily-templates/daily_action_properties.md"
)
DAILY_FOLDER = "/obsidian/personal/_daily"
DA_FOLDER = "/obsidian/personal/_daily/_daily-action"
DA_FILE = "/obsidian/personal/_daily/_daily-action/DA 2026-09-17.md"

BODY_ONLY_TEMPLATE = (
    "What is the highest leverage thing that you can do today to move the ball forward on what you need to?\n"
    "If you only had 2 hours to work today, what would you need to get done to move forward towards your goals or master vision?\n"
    "\n"
    "---\n"
)

TEMPLATE_WITH_FRONTMATTER = """---
_Journal:
Date:
_Weekly-Cycle:
Previous Day:
Shuffler:
---

""" + BODY_ONLY_TEMPLATE

GENERATED = {
    "_Journal": "[[Sep 17, 2026]]",
    "_Weekly-Cycle": ["[[Weekly Cycle Sep. 16 - Sep. 22, 2026]]"],
    "_Long-Cycle": ["[[6-Week Cycle]]"],
    "_Weekly-Map": ["[[Weekly Map 2026-09-20]]"],
}


def _fake_target_day(use_today: bool = False) -> datetime:
    return FIXED_NOW if use_today else FIXED_NOW + timedelta(days=1)


def _folder(name: str, path: str) -> MagicMock:
    entry = MagicMock(spec=dropbox.files.FolderMetadata)
    entry.name = name
    entry.path_lower = path
    return entry


def _file(name: str, path: str) -> MagicMock:
    entry = MagicMock(spec=dropbox.files.FileMetadata)
    entry.name = name
    entry.path_lower = path
    return entry


def _list_result(entries: list) -> MagicMock:
    result = MagicMock()
    result.entries = entries
    result.has_more = False
    return result


def _metadata_missing() -> dropbox.exceptions.ApiError:
    error = dropbox.files.GetMetadataError.path(dropbox.files.LookupError.not_found)
    return dropbox.exceptions.ApiError("req", error, "", "")


def _download_missing() -> dropbox.exceptions.ApiError:
    error = dropbox.files.DownloadError.path(dropbox.files.LookupError.not_found)
    return dropbox.exceptions.ApiError("req", error, "", "")


def _download(content: str):
    response = MagicMock()
    response.content = content.encode("utf-8")
    return MagicMock(), response


def _parse_note(content: str) -> tuple[dict, str]:
    assert content.startswith("---\n")
    lines = content.split("\n")
    end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def _list_folder(path: str, *, include_templates: bool = True, include_template_file: bool = True):
    if path == VAULT:
        entries = [_folder("_Daily", DAILY_FOLDER)]
        if include_templates:
            entries.append(_folder("15_Templates", TEMPLATES_FOLDER))
        return _list_result(entries)
    if path == DAILY_FOLDER:
        return _list_result([_folder("_Daily-Action", DA_FOLDER)])
    if path == TEMPLATES_FOLDER:
        return _list_result([_folder("daily-templates", DAILY_TEMPLATES_FOLDER)])
    if path == DAILY_TEMPLATES_FOLDER:
        files = []
        if include_template_file:
            files.append(_file("daily_action_properties.md", TEMPLATE_PATH))
        files.append(
            _file("daily_note_properties.md", f"{DAILY_TEMPLATES_FOLDER}/daily_note_properties.md")
        )
        return _list_result(files)
    return _list_result([])


def _run_create(
    *,
    template_content=BODY_ONLY_TEMPLATE,
    include_templates=True,
    include_template_file=True,
    existing=False,
    download_error=None,
):
    mock_dbx = MagicMock()
    mock_dbx.files_list_folder.side_effect = lambda path: _list_folder(
        path,
        include_templates=include_templates,
        include_template_file=include_template_file,
    )
    if existing:
        mock_dbx.files_get_metadata.return_value = MagicMock()
    else:
        mock_dbx.files_get_metadata.side_effect = _metadata_missing()

    if download_error is not None:
        mock_dbx.files_download.side_effect = download_error
    elif template_content is not None:
        mock_dbx.files_download.return_value = _download(template_content)
    else:
        mock_dbx.files_download.side_effect = _download_missing()

    with (
        patch(f"{MODULE}._get_dropbox_client", return_value=mock_dbx),
        patch(f"{MODULE}._get_target_day", side_effect=_fake_target_day),
        patch(
            f"{MODULE}._generate_yaml_properties",
            return_value={
                "journal": GENERATED["_Journal"],
                "weekly_cycle": GENERATED["_Weekly-Cycle"],
                "long_cycle": GENERATED["_Long-Cycle"],
                "weekly_map": GENERATED["_Weekly-Map"],
            },
        ),
    ):
        result = mod.create_daily_action(use_today=False)
    return result, mock_dbx


def test_strip_body_only_template_is_verbatim():
    assert mod._strip_template_frontmatter(BODY_ONLY_TEMPLATE) == BODY_ONLY_TEMPLATE


def test_strip_template_frontmatter_drops_yaml_and_keeps_body(caplog):
    body = mod._strip_template_frontmatter(TEMPLATE_WITH_FRONTMATTER)
    assert body == BODY_ONLY_TEMPLATE
    assert "Date" not in body
    assert "Shuffler" not in body
    assert any("Ignoring YAML frontmatter" in rec.message for rec in caplog.records)


def test_resolve_body_fallback_when_template_missing():
    assert mod._resolve_daily_action_body(None) == mod.FALLBACK_MAIN_CONTENT


def test_create_daily_action_uses_body_only_template_under_numbered_templates():
    result, mock_dbx = _run_create()
    assert result is True
    mock_dbx.files_download.assert_called_once_with(TEMPLATE_PATH)
    uploaded_bytes, dest = mock_dbx.files_upload.call_args.args[:2]
    assert dest == DA_FILE
    uploaded = uploaded_bytes.decode("utf-8")
    meta, body = _parse_note(uploaded)
    assert list(meta.keys()) == list(GENERATED.keys())
    assert meta["_Journal"] == GENERATED["_Journal"]
    assert meta["_Weekly-Cycle"] == GENERATED["_Weekly-Cycle"]
    assert "Date" not in meta
    assert "Previous Day" not in meta
    assert body == BODY_ONLY_TEMPLATE


def test_create_daily_action_strips_template_frontmatter_if_present(caplog):
    result, mock_dbx = _run_create(template_content=TEMPLATE_WITH_FRONTMATTER)
    assert result is True
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    meta, body = _parse_note(uploaded)
    assert list(meta.keys()) == list(GENERATED.keys())
    assert "Date" not in meta
    assert "Shuffler" not in meta
    assert body == BODY_ONLY_TEMPLATE
    assert any("Ignoring YAML frontmatter" in rec.message for rec in caplog.records)


def test_create_daily_action_missing_template_uses_fallback(caplog):
    result, mock_dbx = _run_create(include_templates=False)
    assert result is True
    mock_dbx.files_download.assert_not_called()
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    meta, body = _parse_note(uploaded)
    assert meta["_Journal"] == GENERATED["_Journal"]
    assert body == mod.FALLBACK_MAIN_CONTENT
    assert "Vision Objective 1:" in body
    assert any("fallback" in rec.message.lower() for rec in caplog.records)


def test_create_daily_action_missing_template_file_uses_fallback(caplog):
    result, mock_dbx = _run_create(include_template_file=False)
    assert result is True
    mock_dbx.files_download.assert_not_called()
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    assert mod.FALLBACK_MAIN_CONTENT in uploaded
    assert any("fallback" in rec.message.lower() for rec in caplog.records)


def test_create_daily_action_download_error_uses_fallback(caplog):
    result, mock_dbx = _run_create(download_error=_download_missing())
    assert result is True
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    assert mod.FALLBACK_MAIN_CONTENT in uploaded
    assert any("fallback" in rec.message.lower() for rec in caplog.records)


def test_create_daily_action_does_not_overwrite_existing():
    result, mock_dbx = _run_create(existing=True)
    assert result is True
    mock_dbx.files_upload.assert_not_called()
    mock_dbx.files_download.assert_not_called()
