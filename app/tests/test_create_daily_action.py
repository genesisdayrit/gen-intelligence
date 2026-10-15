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
import requests
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SYSTEM_TIMEZONE", "America/Los_Angeles")
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")

from scripts.obsidian.workflows.file_creation import create_daily_action as mod
from services.obsidian.utils.template_boundary import (
    HUB_UPDATES_SLOT,
    MINDFOLD_DAILY_PLAN_SLOT,
    MINDFOLD_DAILY_REVIEW_SLOT,
)

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
    "Date": "2026-09-17",
    "Previous Day": ["[[DA 2026-09-16]]"],
    "Next Day": ["[[DA 2026-09-18]]"],
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


def _initiative_node(name: str, status: str = "Active", labels: list[str] | None = None) -> dict:
    return {
        "id": f"id-{name}",
        "name": name,
        "status": status,
        "labels": {"nodes": [{"id": f"l-{label}", "name": label} for label in (labels or [])]},
    }


def _run_create(
    *,
    template_content=BODY_ONLY_TEMPLATE,
    include_templates=True,
    include_template_file=True,
    existing=False,
    download_error=None,
    linear_nodes=None,
    linear_error=None,
    main_thread_updates=None,
    main_thread_error=None,
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

    linear_patch = (
        patch(f"{MODULE}.fetch_all_pages", side_effect=linear_error)
        if linear_error is not None
        else patch(f"{MODULE}.fetch_all_pages", return_value=linear_nodes or [])
    )
    updates_patch = (
        patch(f"{MODULE}.fetch_initiative_updates", side_effect=main_thread_error)
        if main_thread_error is not None
        else patch(
            f"{MODULE}.fetch_initiative_updates",
            return_value=main_thread_updates or [],
        )
    )

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
        patch(f"{MODULE}.LINEAR_API_KEY", "test-linear-key"),
        linear_patch,
        updates_patch,
    ):
        result = mod.create_daily_action(use_today=False)
    return result, mock_dbx


def _assert_body_order(body: str, *, has_main_thread: bool = False, header_names=None):
    review = body.index(MINDFOLD_DAILY_REVIEW_SLOT)
    hub = body.index(HUB_UPDATES_SLOT)
    plan = body.index(MINDFOLD_DAILY_PLAN_SLOT)
    leverage = body.index("highest leverage")
    divider = body.index("\n---\n", leverage)
    assert review < hub < plan < leverage < divider
    if has_main_thread:
        main = body.index("#### Main Thread")
        assert hub < main < plan
    else:
        assert "#### Main Thread" not in body
    if header_names:
        last = divider
        for name in header_names:
            pos = body.index(f"##### {name}")
            assert pos > last
            last = pos
    else:
        assert "##### " not in body


def _uploaded_body(mock_dbx) -> str:
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    _meta, body = _parse_note(uploaded)
    return body


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
    assert meta["Date"] == GENERATED["Date"]
    assert meta["Previous Day"] == GENERATED["Previous Day"]
    assert meta["Next Day"] == GENERATED["Next Day"]
    assert BODY_ONLY_TEMPLATE.strip() in body
    _assert_body_order(body)


def test_create_daily_action_strips_template_frontmatter_if_present(caplog):
    result, mock_dbx = _run_create(template_content=TEMPLATE_WITH_FRONTMATTER)
    assert result is True
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    meta, body = _parse_note(uploaded)
    assert list(meta.keys()) == list(GENERATED.keys())
    assert meta["Date"] == GENERATED["Date"]
    assert "Shuffler" not in meta
    assert BODY_ONLY_TEMPLATE.strip() in body
    _assert_body_order(body)
    assert any("Ignoring YAML frontmatter" in rec.message for rec in caplog.records)


def test_create_daily_action_missing_template_uses_fallback(caplog):
    result, mock_dbx = _run_create(include_templates=False)
    assert result is True
    mock_dbx.files_download.assert_not_called()
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    meta, body = _parse_note(uploaded)
    assert meta["_Journal"] == GENERATED["_Journal"]
    assert BODY_ONLY_TEMPLATE.strip() in body
    assert mod.FALLBACK_MAIN_CONTENT.strip() in body
    _assert_body_order(body)
    assert "Vision Objective" not in body
    assert any("fallback" in rec.message.lower() for rec in caplog.records)


def test_create_daily_action_missing_template_file_uses_fallback(caplog):
    result, mock_dbx = _run_create(include_template_file=False)
    assert result is True
    mock_dbx.files_download.assert_not_called()
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    assert mod.FALLBACK_MAIN_CONTENT.strip() in uploaded
    _assert_body_order(_uploaded_body(mock_dbx))
    assert any("fallback" in rec.message.lower() for rec in caplog.records)


def test_create_daily_action_download_error_uses_fallback(caplog):
    result, mock_dbx = _run_create(download_error=_download_missing())
    assert result is True
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    assert mod.FALLBACK_MAIN_CONTENT.strip() in uploaded
    _assert_body_order(_uploaded_body(mock_dbx))
    assert any("fallback" in rec.message.lower() for rec in caplog.records)


def test_create_daily_action_does_not_overwrite_existing():
    result, mock_dbx = _run_create(existing=True)
    assert result is True
    mock_dbx.files_upload.assert_not_called()
    mock_dbx.files_download.assert_not_called()


def test_select_active_initiatives_excludes_non_active_archived_shape_and_main_thread():
    nodes = [
        _initiative_node("Keep Me"),
        _initiative_node("Main Thread", labels=["main-thread"]),
        _initiative_node("Planned Work", status="Planned"),
        _initiative_node("Done Work", status="Completed"),
        _initiative_node("MAIN-THREAD-ALT", labels=["Main-Thread"]),
    ]
    selected = mod._select_active_initiatives(nodes, exclude_label="main-thread")
    assert [n["name"] for n in selected] == ["Keep Me"]


def test_select_active_initiatives_sorts_case_insensitive():
    nodes = [
        _initiative_node("zeta"),
        _initiative_node("Alpha"),
        _initiative_node("beta"),
    ]
    selected = mod._select_active_initiatives(nodes, exclude_label="main-thread")
    assert [n["name"] for n in selected] == ["Alpha", "beta", "zeta"]


def test_select_active_initiatives_honors_custom_exclude_label():
    nodes = [
        _initiative_node("Keep", labels=["main-thread"]),
        _initiative_node("Skip", labels=["parked"]),
    ]
    selected = mod._select_active_initiatives(nodes, exclude_label="parked")
    assert [n["name"] for n in selected] == ["Keep"]


def test_select_active_initiatives_empty_exclude_label_keeps_main_thread():
    nodes = [
        _initiative_node("Keep", labels=["main-thread"]),
        _initiative_node("Also", labels=[]),
    ]
    selected = mod._select_active_initiatives(nodes, exclude_label="")
    assert [n["name"] for n in selected] == ["Also", "Keep"]


def test_append_initiative_headers_after_template_divider():
    names = ["Alpha", "beta"]
    result = mod._append_initiative_headers(BODY_ONLY_TEMPLATE, names)
    assert result.startswith(BODY_ONLY_TEMPLATE.rstrip("\n"))
    divider = result.index("\n---\n")
    alpha = result.index("##### Alpha")
    beta = result.index("##### beta")
    assert divider < alpha < beta
    assert "- \n" in result[alpha:]
    lines = result.splitlines()
    assert lines[lines.index("##### Alpha") + 1] == "- "
    assert lines[lines.index("##### Alpha") + 2] == ""


def test_append_initiative_headers_is_idempotent():
    names = ["Alpha", "beta"]
    once = mod._append_initiative_headers(BODY_ONLY_TEMPLATE, names)
    twice = mod._append_initiative_headers(once, names)
    assert twice == once
    assert once.count("##### Alpha") == 1
    assert once.count("##### beta") == 1


def test_append_initiative_headers_adds_only_missing_names():
    existing = mod._append_initiative_headers(BODY_ONLY_TEMPLATE, ["Alpha"])
    result = mod._append_initiative_headers(existing, ["Alpha", "beta"])
    assert result.count("##### Alpha") == 1
    assert "##### beta" in result
    assert result.index("##### Alpha") < result.index("##### beta")


def test_create_daily_action_includes_active_initiative_headers():
    result, mock_dbx = _run_create(
        linear_nodes=[
            _initiative_node("Zeta Labs"),
            _initiative_node("alpha OS"),
        ]
    )
    assert result is True
    body = _uploaded_body(mock_dbx)
    assert BODY_ONLY_TEMPLATE.strip() in body
    _assert_body_order(body, header_names=["alpha OS", "Zeta Labs"])
    assert "##### alpha OS\n- \n" in body
    assert "##### Zeta Labs\n- \n" in body


def test_create_daily_action_linear_failure_still_creates(caplog):
    result, mock_dbx = _run_create(linear_error=requests.exceptions.Timeout("Linear timed out"))
    assert result is True
    body = _uploaded_body(mock_dbx)
    assert BODY_ONLY_TEMPLATE.strip() in body
    _assert_body_order(body)
    assert any("Failed to fetch Linear initiatives" in rec.message for rec in caplog.records)


def test_create_daily_action_linear_empty_omits_headers():
    result, mock_dbx = _run_create(linear_nodes=[])
    assert result is True
    body = _uploaded_body(mock_dbx)
    assert BODY_ONLY_TEMPLATE.strip() in body
    _assert_body_order(body)


def test_create_daily_action_excludes_main_thread_and_non_active():
    result, mock_dbx = _run_create(
        linear_nodes=[
            _initiative_node("Hub OS"),
            _initiative_node("Main Thread", labels=["main-thread"]),
            _initiative_node("Future", status="Planned"),
        ]
    )
    assert result is True
    body = _uploaded_body(mock_dbx)
    assert "##### Hub OS" in body
    assert "##### Main Thread" not in body
    assert "##### Future" not in body


def test_create_daily_action_orders_headers_case_insensitive():
    result, mock_dbx = _run_create(
        linear_nodes=[
            _initiative_node("zeta"),
            _initiative_node("Alpha"),
            _initiative_node("beta"),
        ]
    )
    body = _uploaded_body(mock_dbx)
    assert body.index("##### Alpha") < body.index("##### beta") < body.index("##### zeta")


def test_create_daily_action_respects_exclude_label_env(monkeypatch):
    monkeypatch.setenv("DAILY_ACTION_EXCLUDE_INITIATIVE_LABEL", "parked")
    result, mock_dbx = _run_create(
        linear_nodes=[
            _initiative_node("Keep", labels=["main-thread"]),
            _initiative_node("Skip", labels=["parked"]),
        ]
    )
    assert result is True
    body = _uploaded_body(mock_dbx)
    assert "##### Keep" in body
    assert "##### Skip" not in body


MAIN_THREAD_UPDATE = {
    "id": "upd-latest",
    "url": "https://linear.app/chapters/initiativeUpdate/upd-latest",
    "body": "## Previous Day's Wins\n- Shipped the DA layout\n\n---\n\nKeep going.",
    "createdAt": "2026-10-14T23:00:00.000Z",
    "updatedAt": "2026-10-14T23:00:00.000Z",
}


def test_create_daily_action_includes_latest_main_thread_update():
    result, mock_dbx = _run_create(
        linear_nodes=[
            _initiative_node("Hub OS"),
            _initiative_node("Main Thread", labels=["main-thread"]),
        ],
        main_thread_updates=[
            {
                "id": "upd-old",
                "url": "https://linear.app/chapters/initiativeUpdate/upd-old",
                "body": "older",
                "createdAt": "2026-10-13T23:00:00.000Z",
            },
            MAIN_THREAD_UPDATE,
        ],
    )
    assert result is True
    body = _uploaded_body(mock_dbx)
    _assert_body_order(body, has_main_thread=True, header_names=["Hub OS"])
    assert "#### Main Thread (Oct 14, 2026 4:00 PM PT)" in body
    assert "<!-- main-thread-update: https://linear.app/chapters/initiativeUpdate/upd-latest -->" in body
    assert "Shipped the DA layout" in body
    assert "***" in body
    assert body.count("---") >= 1
    assert "older" not in body
    assert "##### Main Thread" not in body


def test_create_daily_action_omits_main_thread_when_updates_fail(caplog):
    result, mock_dbx = _run_create(
        linear_nodes=[
            _initiative_node("Hub OS"),
            _initiative_node("Main Thread", labels=["main-thread"]),
        ],
        main_thread_error=requests.exceptions.Timeout("updates timed out"),
    )
    assert result is True
    body = _uploaded_body(mock_dbx)
    _assert_body_order(body, header_names=["Hub OS"])
    assert any("Failed to fetch main-thread initiative updates" in rec.message for rec in caplog.records)


def test_create_daily_action_omits_main_thread_when_none():
    result, mock_dbx = _run_create(
        linear_nodes=[_initiative_node("Main Thread", labels=["main-thread"])],
        main_thread_updates=[],
    )
    assert result is True
    body = _uploaded_body(mock_dbx)
    _assert_body_order(body)
    assert MINDFOLD_DAILY_REVIEW_SLOT in body
    assert HUB_UPDATES_SLOT in body
    assert MINDFOLD_DAILY_PLAN_SLOT in body


def test_da_adjacent_yaml_uses_list_form_for_tomorrow_and_today():
    with patch(f"{MODULE}._get_target_day", side_effect=_fake_target_day):
        tomorrow = mod._da_adjacent_yaml(use_today=False)
        today = mod._da_adjacent_yaml(use_today=True)
    assert tomorrow == {
        "Date": "2026-09-17",
        "Previous Day": ["[[DA 2026-09-16]]"],
        "Next Day": ["[[DA 2026-09-18]]"],
    }
    assert today == {
        "Date": "2026-09-16",
        "Previous Day": ["[[DA 2026-09-15]]"],
        "Next Day": ["[[DA 2026-09-17]]"],
    }


def test_create_daily_action_yaml_has_date_and_adjacent_da_links():
    result, mock_dbx = _run_create()
    assert result is True
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    meta, _body = _parse_note(uploaded)
    assert meta["Date"] == "2026-09-17"
    assert meta["Previous Day"] == ["[[DA 2026-09-16]]"]
    assert meta["Next Day"] == ["[[DA 2026-09-18]]"]
    assert "Previous Day:\n- '[[DA 2026-09-16]]'" in uploaded or "Previous Day:\n- [[DA 2026-09-16]]" in uploaded
    assert "Next Day:\n- '[[DA 2026-09-18]]'" in uploaded or "Next Day:\n- [[DA 2026-09-18]]" in uploaded


def test_format_main_thread_block_sanitizes_divider_and_embeds_url():
    block = mod._format_main_thread_block(MAIN_THREAD_UPDATE)
    assert block.startswith("#### Main Thread (Oct 14, 2026 4:00 PM PT)\n")
    assert "<!-- main-thread-update: https://linear.app/chapters/initiativeUpdate/upd-latest -->" in block
    assert "***" in block
    assert "\n---\n" not in f"\n{block}"
