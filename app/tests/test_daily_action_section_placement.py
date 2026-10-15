"""Webhook section placement on body-only Daily Action notes.

Current templates have leverage questions and a trailing `---`, with no
Vision Objective and no Daily Review. Inserters must land after that
divider (or at EOF), not above the prompts.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/test/vault")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")
os.environ.setdefault("SYSTEM_TIMEZONE", "US/Eastern")

from services.obsidian.add_daily_action_issues_touched import (
    ISSUES_TOUCHED_HEADER,
    _find_issues_touched_insert_position,
    upsert_daily_action_issue_touched,
)
from services.obsidian.add_daily_action_updates import (
    INITIATIVE_UPDATES_HEADER,
    upsert_daily_action_update,
)
from services.obsidian.add_manus_task import DAILY_ACTION_HEADER, _upsert_daily_action_manus
from services.obsidian.add_todoist_completed import (
    TODOIST_COMPLETED_HEADER,
    _find_todoist_insert_position,
    append_todoist_completed,
)
from services.obsidian.utils.template_boundary import (
    fallback_webhook_insert_index,
    template_end_insert_index,
)

LEVERAGE_Q1 = (
    "What is the highest leverage thing that you can do today to move the ball "
    "forward on what you need to?"
)
LEVERAGE_Q2 = (
    "If you only had 2 hours to work today, what would you need to get done to "
    "move forward towards your goals or master vision?"
)

BODY_ONLY_NOTE = f"""---
_Journal: '[[Sep 17, 2026]]'
---

{LEVERAGE_Q1}
{LEVERAGE_Q2}

---
"""

DA_PATH = "/test/vault/_Daily/_Daily-Action/DA 2026-05-24.md"


def _lines_of_body(note: str) -> list[str]:
    parts = note.split("---\n", 2)
    body = parts[2].lstrip("\n") if len(parts) > 2 else note
    return body.split("\n")


def test_template_end_insert_index_after_trailing_divider():
    lines = _lines_of_body(BODY_ONLY_NOTE)
    idx = template_end_insert_index(lines)
    assert lines[idx - 1].strip() == "---"
    assert idx == lines.index("---") + 1


def test_template_end_insert_index_eof_when_no_divider():
    assert template_end_insert_index(["just a prompt"]) == 1


def test_fallback_prefers_vision_objective_then_daily_review_then_divider():
    lines = ["Daily Review:", "ok", "---", "Vision Objective 1:", "x"]
    assert fallback_webhook_insert_index(lines, 3, template_boundary_line=3) == 3
    assert fallback_webhook_insert_index(lines, 3, template_boundary_line=None) == 3
    body_only = _lines_of_body(BODY_ONLY_NOTE)
    assert fallback_webhook_insert_index(body_only, 0, None) == template_end_insert_index(
        body_only
    )


def test_issues_touched_position_after_template_divider():
    lines = _lines_of_body(BODY_ONLY_NOTE)
    pos = _find_issues_touched_insert_position(lines, 0)
    assert pos == template_end_insert_index(lines)
    assert pos > next(i for i, line in enumerate(lines) if "highest leverage" in line)


def test_todoist_position_after_template_divider():
    lines = _lines_of_body(BODY_ONLY_NOTE)
    pos = _find_todoist_insert_position(lines, 0)
    assert pos == template_end_insert_index(lines)


def test_todoist_position_after_initiative_not_before_divider():
    lines = _lines_of_body(BODY_ONLY_NOTE) + [
        "",
        INITIATIVE_UPDATES_HEADER,
        "[09:00] - [[Some Initiative]] ([link](https://linear.app/x)): note",
        "",
    ]
    pos = _find_todoist_insert_position(lines, 0)
    divider = next(i for i, line in enumerate(lines) if line.strip() == "---")
    initiative = next(i for i, line in enumerate(lines) if line.strip() == INITIATIVE_UPDATES_HEADER)
    assert pos > divider
    assert pos > initiative


def _mock_writer(module: str, file_content: str):
    uploaded = {}
    mock_dbx = MagicMock()
    metadata = MagicMock()
    metadata.rev = "aaaaaaaaaaaaaaaa"
    metadata.path_display = DA_PATH
    response = MagicMock()
    response.content = file_content.encode("utf-8")
    mock_dbx.files_download.return_value = (metadata, response)

    def capture_upload(data, path, mode=None, autorename=None):
        uploaded["content"] = data.decode("utf-8")
        uploaded["mode"] = mode

    mock_dbx.files_upload.side_effect = capture_upload
    return mock_dbx, uploaded


def _assert_after_template(uploaded: str, header: str) -> None:
    header_pos = uploaded.index(header)
    leverage_pos = uploaded.index(LEVERAGE_Q1)
    divider_pos = uploaded.index("\n---\n", leverage_pos)
    assert leverage_pos < divider_pos < header_pos, (
        f"{header} must land after the template divider, not above the leverage questions"
    )


def test_todoist_append_lands_after_template_divider():
    mock_dbx, uploaded = _mock_writer(
        "services.obsidian.add_todoist_completed", BODY_ONLY_NOTE
    )
    with (
        patch("services.obsidian.add_todoist_completed._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_todoist_completed._find_daily_folder", return_value="/test/vault/_Daily"),
        patch(
            "services.obsidian.add_todoist_completed._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch(
            "services.obsidian.add_todoist_completed._get_today_daily_action_path",
            return_value=DA_PATH,
        ),
    ):
        append_todoist_completed("Ship the placement fix")
    assert TODOIST_COMPLETED_HEADER in uploaded["content"]
    _assert_after_template(uploaded["content"], TODOIST_COMPLETED_HEADER)


def test_manus_lands_after_template_divider():
    mock_dbx, uploaded = _mock_writer("services.obsidian.add_manus_task", BODY_ONLY_NOTE)
    with (
        patch("services.obsidian.add_manus_task._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_manus_task._find_daily_folder", return_value="/test/vault/_Daily"),
        patch(
            "services.obsidian.add_manus_task._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch("services.obsidian.add_manus_task._get_today_daily_action_path", return_value=DA_PATH),
    ):
        result = _upsert_daily_action_manus("abc123", "Test Task", "https://manus.im/app/abc123")
    assert result["success"] is True
    _assert_after_template(uploaded["content"], DAILY_ACTION_HEADER)


def test_manus_stays_after_initiative_on_body_only_note():
    note = BODY_ONLY_NOTE + f"\n{INITIATIVE_UPDATES_HEADER}\n[09:00] - [[Some Initiative]] ([link](https://linear.app/x)): note\n"
    mock_dbx, uploaded = _mock_writer("services.obsidian.add_manus_task", note)
    with (
        patch("services.obsidian.add_manus_task._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_manus_task._find_daily_folder", return_value="/test/vault/_Daily"),
        patch(
            "services.obsidian.add_manus_task._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch("services.obsidian.add_manus_task._get_today_daily_action_path", return_value=DA_PATH),
    ):
        result = _upsert_daily_action_manus("abc123", "Test Task", "https://manus.im/app/abc123")
    assert result["success"] is True
    content = uploaded["content"]
    _assert_after_template(content, DAILY_ACTION_HEADER)
    assert content.index(INITIATIVE_UPDATES_HEADER) < content.index(DAILY_ACTION_HEADER)


def test_linear_update_lands_after_template_divider():
    mock_dbx, uploaded = _mock_writer(
        "services.obsidian.add_daily_action_updates", BODY_ONLY_NOTE
    )
    with (
        patch("services.obsidian.add_daily_action_updates._get_dropbox_client", return_value=mock_dbx),
        patch("services.obsidian.add_daily_action_updates._find_daily_folder", return_value="/test/vault/_Daily"),
        patch(
            "services.obsidian.add_daily_action_updates._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch(
            "services.obsidian.add_daily_action_updates._get_today_daily_action_path",
            return_value=DA_PATH,
        ),
    ):
        result = upsert_daily_action_update(
            "initiative",
            "https://linear.app/x/initiative",
            "Some Initiative",
            "shipped the placement fix",
        )
    assert result["success"] is True
    _assert_after_template(uploaded["content"], INITIATIVE_UPDATES_HEADER)


def test_issues_touched_lands_after_template_divider_and_finds_entry_past_divider():
    mock_dbx, uploaded = _mock_writer(
        "services.obsidian.add_daily_action_issues_touched", BODY_ONLY_NOTE
    )
    kwargs = dict(
        issue_identifier="GD-328",
        project_name="Centralizing OS",
        issue_title="Add Issues Touched",
        status_name="In Progress",
        issue_url="https://linear.app/chapters/issue/gd-328/add-issues-touched",
        status_changed=False,
    )
    with (
        patch("services.obsidian.add_daily_action_issues_touched._get_dropbox_client", return_value=mock_dbx),
        patch(
            "services.obsidian.add_daily_action_issues_touched._find_daily_folder",
            return_value="/test/vault/_Daily",
        ),
        patch(
            "services.obsidian.add_daily_action_issues_touched._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch(
            "services.obsidian.add_daily_action_issues_touched._get_today_daily_action_path",
            return_value=DA_PATH,
        ),
    ):
        result = upsert_daily_action_issue_touched(**kwargs)
    assert result["success"] is True
    assert result["action"] == "inserted"
    content = uploaded["content"]
    _assert_after_template(content, ISSUES_TOUCHED_HEADER)
    assert "[GD-328]" in content

    # The template `---` must not hide the new entry from a later upsert.
    mock_dbx2, uploaded2 = _mock_writer(
        "services.obsidian.add_daily_action_issues_touched", content
    )
    with (
        patch("services.obsidian.add_daily_action_issues_touched._get_dropbox_client", return_value=mock_dbx2),
        patch(
            "services.obsidian.add_daily_action_issues_touched._find_daily_folder",
            return_value="/test/vault/_Daily",
        ),
        patch(
            "services.obsidian.add_daily_action_issues_touched._find_daily_action_folder",
            return_value="/test/vault/_Daily/_Daily-Action",
        ),
        patch(
            "services.obsidian.add_daily_action_issues_touched._get_today_daily_action_path",
            return_value=DA_PATH,
        ),
    ):
        again = upsert_daily_action_issue_touched(**kwargs)
    assert again["success"] is True
    assert again["action"] == "skipped"
    assert "content" not in uploaded2
