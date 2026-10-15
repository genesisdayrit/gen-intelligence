"""End-to-end Daily Action day: create → webhook writes → mindfold → reconcile."""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/test/vault")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")
os.environ.setdefault("SYSTEM_TIMEZONE", "America/Los_Angeles")

from scripts.obsidian.workflows.file_creation.create_daily_action import (
    _compose_daily_action_body,
    _da_adjacent_yaml,
    _format_main_thread_block,
)
from services.obsidian.add_daily_action_issues_touched import (
    ISSUES_TOUCHED_HEADER,
    upsert_daily_action_issue_touched,
)
from services.obsidian.add_daily_action_updates import (
    INITIATIVE_UPDATES_HEADER,
    PROJECT_UPDATES_HEADER,
    upsert_daily_action_update,
)
from services.obsidian.add_todoist_completed import (
    TODOIST_COMPLETED_HEADER,
    append_todoist_completed,
)
from services.obsidian.reconcile.runner import reconcile_missed_obsidian_writes
from services.obsidian.utils.template_boundary import (
    HUB_UPDATES_SLOT,
    MINDFOLD_DAILY_PLAN_SLOT,
    MINDFOLD_DAILY_REVIEW_SLOT,
)

LA = pytz.timezone("America/Los_Angeles")
DA_PATH = "/test/vault/_Daily/_Daily-Action/DA 2026-10-15.md"
LEVERAGE_Q1 = (
    "What is the highest leverage thing that you can do today to move the ball "
    "forward on what you need to?"
)
LEVERAGE_Q2 = (
    "If you only had 2 hours to work today, what would you need to get done to "
    "move forward towards your goals or master vision?"
)
TEMPLATE = f"{LEVERAGE_Q1}\n{LEVERAGE_Q2}\n\n---\n"
MAIN_THREAD_URL = "https://linear.app/chapters/initiativeUpdate/upd-latest"
HUB_UPDATE_URL = "https://linear.app/x/hub-update"
PROJECT_UPDATE_URL = "https://linear.app/x/project-update"
MAIN_THREAD_UPDATE = {
    "url": MAIN_THREAD_URL,
    "body": "## Previous Day's Wins\n- Shipped the DA layout\n\n---\n\nKeep going.",
    "createdAt": "2026-10-14T23:00:00.000Z",
}


def _initial_note() -> str:
    adjacent = {
        "Date": "2026-10-15",
        "Previous Day": ["[[DA 2026-10-14]]"],
        "Next Day": ["[[DA 2026-10-16]]"],
    }
    yaml_block = (
        "---\n"
        "_Journal: '[[Oct 15, 2026]]'\n"
        "_Weekly-Cycle:\n"
        "- '[[Weekly Cycle Oct. 14 - Oct. 20, 2026]]'\n"
        "_Long-Cycle:\n"
        "- '[[6-Week Cycle]]'\n"
        "_Weekly-Map:\n"
        "- '[[Weekly Map 2026-10-18]]'\n"
        f"Date: '{adjacent['Date']}'\n"
        "Previous Day:\n"
        f"- '{adjacent['Previous Day'][0]}'\n"
        "Next Day:\n"
        f"- '{adjacent['Next Day'][0]}'\n"
        "---\n\n"
    )
    body = _compose_daily_action_body(
        TEMPLATE,
        ["Hub OS"],
        MAIN_THREAD_UPDATE,
    )
    return yaml_block + body


def _mock_dbx(content: str):
    uploaded = {}
    mock_dbx = MagicMock()
    metadata = MagicMock()
    metadata.rev = "aaaaaaaaaaaaaaaa"
    metadata.path_display = DA_PATH
    response = MagicMock()
    response.content = content.encode("utf-8")
    mock_dbx.files_download.return_value = (metadata, response)

    def capture_upload(data, path, mode=None, autorename=None):
        uploaded["content"] = data.decode("utf-8")
        uploaded["mode"] = mode

    mock_dbx.files_upload.side_effect = capture_upload
    return mock_dbx, uploaded


def _patch_writer(module: str, mock_dbx):
    return (
        patch(f"{module}._get_dropbox_client", return_value=mock_dbx),
        patch(f"{module}._find_daily_folder", return_value="/test/vault/_Daily"),
        patch(
            f"{module}._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch(f"{module}._get_today_daily_action_path", return_value=DA_PATH),
    )


def _insert_after_slot(content: str, slot: str, insertion: str) -> str:
    return content.replace(slot, f"{slot}\n{insertion}", 1)


def _assert_final_order(note: str) -> None:
    review = note.index(MINDFOLD_DAILY_REVIEW_SLOT)
    review_body = note.index("Win: shipped the Daily Action layout")
    initiative = note.index(INITIATIVE_UPDATES_HEADER)
    project = note.index(PROJECT_UPDATES_HEADER)
    todoist = note.index(TODOIST_COMPLETED_HEADER)
    issues = note.index(ISSUES_TOUCHED_HEADER)
    hub = note.index(HUB_UPDATES_SLOT)
    main = note.index("#### Main Thread")
    plan = note.index(MINDFOLD_DAILY_PLAN_SLOT)
    plan_body = note.index("- Finish the DA layout")
    leverage = note.index(LEVERAGE_Q1)
    divider = note.index("\n---\n", leverage)
    header = note.index("##### Hub OS")
    assert (
        review
        < review_body
        < initiative
        < project
        < todoist
        < issues
        < hub
        < main
        < plan
        < plan_body
        < leverage
        < divider
        < header
    )
    assert note.count(INITIATIVE_UPDATES_HEADER) == 1
    assert MAIN_THREAD_URL in note
    assert "same snapshot body" not in note
    assert "### Manus Tasks:" not in note


def test_full_day_create_writes_mindfold_and_reconcile_preserve_order():
    note = _initial_note()
    assert MINDFOLD_DAILY_REVIEW_SLOT in note
    assert HUB_UPDATES_SLOT in note
    assert "#### Main Thread (Oct 14, 2026 4:00 PM PT)" in note
    assert MINDFOLD_DAILY_PLAN_SLOT in note
    assert "##### Hub OS" in note
    assert _format_main_thread_block(MAIN_THREAD_UPDATE).strip() in note
    with patch(
        "scripts.obsidian.workflows.file_creation.create_daily_action._get_target_day",
        return_value=LA.localize(datetime(2026, 10, 15, 18, 5)),
    ):
        adjacent = _da_adjacent_yaml(use_today=True)
    assert adjacent["Date"] == "2026-10-15"
    assert adjacent["Previous Day"] == ["[[DA 2026-10-14]]"]
    assert adjacent["Next Day"] == ["[[DA 2026-10-16]]"]

    mock_dbx, uploaded = _mock_dbx(note)
    with _patch_writer("services.obsidian.add_daily_action_updates", mock_dbx)[0], \
            _patch_writer("services.obsidian.add_daily_action_updates", mock_dbx)[1], \
            _patch_writer("services.obsidian.add_daily_action_updates", mock_dbx)[2], \
            _patch_writer("services.obsidian.add_daily_action_updates", mock_dbx)[3]:
        result = upsert_daily_action_update(
            "initiative", HUB_UPDATE_URL, "Hub OS", "shipped placement"
        )
    assert result["success"] is True
    assert result["action"] == "inserted"
    note = uploaded["content"]

    mock_dbx, uploaded = _mock_dbx(note)
    with _patch_writer("services.obsidian.add_daily_action_updates", mock_dbx)[0], \
            _patch_writer("services.obsidian.add_daily_action_updates", mock_dbx)[1], \
            _patch_writer("services.obsidian.add_daily_action_updates", mock_dbx)[2], \
            _patch_writer("services.obsidian.add_daily_action_updates", mock_dbx)[3]:
        result = upsert_daily_action_update(
            "project", PROJECT_UPDATE_URL, "Centralizing OS", "moved the tracker"
        )
    assert result["action"] == "inserted"
    note = uploaded["content"]

    mock_dbx, uploaded = _mock_dbx(note)
    patches = _patch_writer("services.obsidian.add_todoist_completed", mock_dbx)
    with patches[0], patches[1], patches[2], patches[3]:
        append_todoist_completed("Ship the placement fix")
    note = uploaded["content"]

    mock_dbx, uploaded = _mock_dbx(note)
    patches = _patch_writer("services.obsidian.add_daily_action_issues_touched", mock_dbx)
    with patches[0], patches[1], patches[2], patches[3]:
        result = upsert_daily_action_issue_touched(
            issue_identifier="GD-328",
            project_name="Centralizing OS",
            issue_title="Add Issues Touched",
            status_name="In Progress",
            issue_url="https://linear.app/chapters/issue/gd-328/add-issues-touched",
            status_changed=False,
        )
    assert result["action"] == "inserted"
    note = uploaded["content"]

    mock_dbx, uploaded = _mock_dbx(note)
    patches = _patch_writer("services.obsidian.add_daily_action_updates", mock_dbx)
    with patches[0], patches[1], patches[2], patches[3]:
        result = upsert_daily_action_update(
            "initiative",
            MAIN_THREAD_URL,
            "Main Thread",
            "same snapshot body",
        )
    assert result["success"] is True
    assert result["action"] == "skipped"
    assert "content" not in uploaded

    note = _insert_after_slot(
        note,
        MINDFOLD_DAILY_REVIEW_SLOT,
        "Win: shipped the Daily Action layout\nGratitude: a quiet morning\n",
    )
    note = _insert_after_slot(
        note,
        MINDFOLD_DAILY_PLAN_SLOT,
        "- Finish the DA layout\n- Review Linear\n",
    )
    note = note.rstrip() + "\n\n<!-- appended from mindfold 2026-10-15 -->\n---\nignored dump\n"

    fake_redis = MagicMock()
    fake_redis.get.return_value = "2026-10-15T00:00:00Z"
    fake_redis.set.return_value = True
    with patch("services.obsidian.reconcile.runner.redis_client", fake_redis):
        summary = reconcile_missed_obsidian_writes(
            now=datetime(2026, 10, 15, 18, 12, tzinfo=timezone.utc),
            providers=[],
        )
    assert summary["processed"] == 0

    _assert_final_order(note)
    assert "ignored dump" in note
    assert note.index("<!-- appended from mindfold") > note.index("##### Hub OS")


def test_full_day_sample_note_shape():
    """Stable sample of the composed note (no webhook timestamps) for the PR."""
    note = _initial_note()
    assert note.startswith("---\n_Journal:")
    assert "Date: '2026-10-15'" in note or "Date: 2026-10-15" in _initial_note()
    body = note[note.index(MINDFOLD_DAILY_REVIEW_SLOT):]
    assert body.index(MINDFOLD_DAILY_REVIEW_SLOT) < body.index(HUB_UPDATES_SLOT)
    assert body.index(HUB_UPDATES_SLOT) < body.index("#### Main Thread")
    assert body.index("#### Main Thread") < body.index(MINDFOLD_DAILY_PLAN_SLOT)
    assert body.index(MINDFOLD_DAILY_PLAN_SLOT) < body.index(LEVERAGE_Q1)
    assert body.index(LEVERAGE_Q1) < body.index("\n---\n") < body.index("##### Hub OS")
    assert "<!-- main-thread-update: " + MAIN_THREAD_URL in body
    assert "***" in body
