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
    PROJECT_UPDATES_HEADER,
    upsert_daily_action_update,
)
from services.obsidian.add_manus_task import DAILY_ACTION_HEADER, _upsert_daily_action_manus
from services.obsidian.add_todoist_completed import (
    TODOIST_COMPLETED_HEADER,
    _find_todoist_insert_position,
    append_todoist_completed,
)
from services.obsidian.utils.template_boundary import (
    HUB_UPDATES_SLOT,
    MINDFOLD_DAILY_PLAN_SLOT,
    MINDFOLD_DAILY_REVIEW_SLOT,
    fallback_webhook_insert_index,
    is_initiative_work_header,
    template_end_insert_index,
    url_in_main_thread_snapshot,
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

INITIATIVE_HEADERS = """##### Alpha OS
- 

##### zeta
- 
"""

NOTE_WITH_INITIATIVE_HEADERS = BODY_ONLY_NOTE + "\n" + INITIATIVE_HEADERS

MAIN_THREAD_URL = "https://linear.app/chapters/initiativeUpdate/upd-latest"

NOTE_WITH_SLOTS = f"""---
_Journal: '[[Sep 17, 2026]]'
Date: '2026-09-17'
Previous Day:
- '[[DA 2026-09-16]]'
Next Day:
- '[[DA 2026-09-18]]'
---

{MINDFOLD_DAILY_REVIEW_SLOT}

{HUB_UPDATES_SLOT}

#### Main Thread (Oct 14, 2026 4:00 PM PT)
<!-- main-thread-update: {MAIN_THREAD_URL} -->
## Previous Day's Wins
- Shipped the DA layout

{MINDFOLD_DAILY_PLAN_SLOT}

{LEVERAGE_Q1}
{LEVERAGE_Q2}

---

{INITIATIVE_HEADERS}"""

NOTE_WITH_SLOTS_AND_MINDFOLD_DUMP = (
    NOTE_WITH_SLOTS
    + "\n<!-- appended from mindfold 2026-09-17 -->\n---\n### Initiative Updates:\n"
    "this dump must be ignored\n"
)

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


def test_is_initiative_work_header_matches_h5_only():
    assert is_initiative_work_header("##### Alpha OS")
    assert is_initiative_work_header("  ##### zeta  ")
    assert not is_initiative_work_header("### Initiative Updates:")
    assert not is_initiative_work_header("###### deeper")
    assert not is_initiative_work_header("- ")


def test_template_end_insert_index_skips_initiative_headers():
    lines = _lines_of_body(NOTE_WITH_INITIATIVE_HEADERS)
    idx = template_end_insert_index(lines)
    divider = next(i for i, line in enumerate(lines) if line.strip() == "---")
    alpha = next(i for i, line in enumerate(lines) if line.strip() == "##### Alpha OS")
    zeta = next(i for i, line in enumerate(lines) if line.strip() == "##### zeta")
    assert idx > divider
    assert idx > alpha
    assert idx > zeta
    assert idx == len(lines) or not is_initiative_work_header(lines[idx])


def test_issues_touched_and_todoist_positions_after_initiative_headers():
    lines = _lines_of_body(NOTE_WITH_INITIATIVE_HEADERS)
    pos_issues = _find_issues_touched_insert_position(lines, 0)
    pos_todoist = _find_todoist_insert_position(lines, 0)
    zeta = next(i for i, line in enumerate(lines) if line.strip() == "##### zeta")
    assert pos_issues > zeta
    assert pos_todoist > zeta
    assert pos_issues == template_end_insert_index(lines)
    assert pos_todoist == template_end_insert_index(lines)


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


def _assert_after_initiative_headers(uploaded: str, header: str) -> None:
    _assert_after_template(uploaded, header)
    assert uploaded.index("##### zeta") < uploaded.index(header)
    assert uploaded.index("##### Alpha OS") < uploaded.index(header)


def test_linear_update_lands_after_initiative_headers():
    mock_dbx, uploaded = _mock_writer(
        "services.obsidian.add_daily_action_updates", NOTE_WITH_INITIATIVE_HEADERS
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
    _assert_after_initiative_headers(uploaded["content"], INITIATIVE_UPDATES_HEADER)
    assert uploaded["content"].count("##### Alpha OS") == 1


def test_todoist_append_lands_after_initiative_headers():
    mock_dbx, uploaded = _mock_writer(
        "services.obsidian.add_todoist_completed", NOTE_WITH_INITIATIVE_HEADERS
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
    _assert_after_initiative_headers(uploaded["content"], TODOIST_COMPLETED_HEADER)


def test_manus_lands_after_initiative_headers():
    mock_dbx, uploaded = _mock_writer(
        "services.obsidian.add_manus_task", NOTE_WITH_INITIATIVE_HEADERS
    )
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
    _assert_after_initiative_headers(uploaded["content"], DAILY_ACTION_HEADER)


def test_issues_touched_lands_after_initiative_headers():
    mock_dbx, uploaded = _mock_writer(
        "services.obsidian.add_daily_action_issues_touched", NOTE_WITH_INITIATIVE_HEADERS
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
        result = upsert_daily_action_issue_touched(
            issue_identifier="GD-328",
            project_name="Centralizing OS",
            issue_title="Add Issues Touched",
            status_name="In Progress",
            issue_url="https://linear.app/chapters/issue/gd-328/add-issues-touched",
            status_changed=False,
        )
    assert result["success"] is True
    _assert_after_initiative_headers(uploaded["content"], ISSUES_TOUCHED_HEADER)


def _assert_in_updates_area(uploaded: str, header: str) -> None:
    review = uploaded.index(MINDFOLD_DAILY_REVIEW_SLOT)
    hub = uploaded.index(HUB_UPDATES_SLOT)
    main = uploaded.index("#### Main Thread")
    plan = uploaded.index(MINDFOLD_DAILY_PLAN_SLOT)
    leverage = uploaded.index(LEVERAGE_Q1)
    header_pos = uploaded.index(header)
    assert review < header_pos < hub < main < plan < leverage


def test_fallback_prefers_hub_slot_on_new_notes():
    lines = _lines_of_body(NOTE_WITH_SLOTS)
    idx = fallback_webhook_insert_index(lines, 0, None)
    assert lines[idx].strip() == HUB_UPDATES_SLOT
    assert idx < next(i for i, line in enumerate(lines) if line.startswith("#### Main Thread"))


def test_template_end_insert_index_after_headers_ignores_slots_and_main_thread():
    lines = _lines_of_body(NOTE_WITH_SLOTS)
    idx = template_end_insert_index(lines)
    zeta = next(i for i, line in enumerate(lines) if line.strip() == "##### zeta")
    assert idx > zeta
    assert "#### Main Thread" in "\n".join(lines[:idx])


def test_mindfold_appended_dump_does_not_steal_divider_or_hub_slot():
    lines = _lines_of_body(NOTE_WITH_SLOTS_AND_MINDFOLD_DUMP)
    assert fallback_webhook_insert_index(lines, 0, None) == next(
        i for i, line in enumerate(lines) if line.strip() == HUB_UPDATES_SLOT
    )
    idx = template_end_insert_index(lines)
    marker = next(i for i, line in enumerate(lines) if line.strip().startswith("<!-- appended from mindfold"))
    dump_divider = next(i for i, line in enumerate(lines) if i > marker and line.strip() == "---")
    # Placement stops at the mindfold dump, so the dump --- cannot become the template end.
    assert idx <= marker < dump_divider
    assert "this dump must be ignored" not in "\n".join(lines[:idx])


def test_url_in_main_thread_snapshot_matches_embedded_url():
    lines = _lines_of_body(NOTE_WITH_SLOTS)
    assert url_in_main_thread_snapshot(lines, MAIN_THREAD_URL)
    assert not url_in_main_thread_snapshot(lines, "https://linear.app/other")


def test_linear_update_lands_at_hub_slot_above_main_thread():
    mock_dbx, uploaded = _mock_writer(
        "services.obsidian.add_daily_action_updates", NOTE_WITH_SLOTS
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
            "https://linear.app/x/hub-update",
            "Hub OS",
            "shipped the placement fix",
        )
    assert result["success"] is True
    _assert_in_updates_area(uploaded["content"], INITIATIVE_UPDATES_HEADER)
    assert uploaded["content"].count("#### Main Thread") == 1


def test_main_thread_duplicate_update_is_skipped():
    mock_dbx, uploaded = _mock_writer(
        "services.obsidian.add_daily_action_updates", NOTE_WITH_SLOTS
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
            MAIN_THREAD_URL,
            "Main Thread",
            "same snapshot body",
        )
    assert result["success"] is True
    assert result["action"] == "skipped"
    assert "content" not in uploaded
    assert INITIATIVE_UPDATES_HEADER not in NOTE_WITH_SLOTS


def test_todoist_and_project_and_issues_land_at_hub_slot():
    note = NOTE_WITH_SLOTS
    mock_dbx, uploaded = _mock_writer("services.obsidian.add_todoist_completed", note)
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
    _assert_in_updates_area(uploaded["content"], TODOIST_COMPLETED_HEADER)

    mock_dbx, uploaded = _mock_writer(
        "services.obsidian.add_daily_action_updates", uploaded["content"]
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
            "project",
            "https://linear.app/x/project-update",
            "Centralizing OS",
            "moved the tracker",
        )
    assert result["success"] is True
    _assert_in_updates_area(uploaded["content"], PROJECT_UPDATES_HEADER)

    mock_dbx, uploaded = _mock_writer(
        "services.obsidian.add_daily_action_issues_touched", uploaded["content"]
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
        result = upsert_daily_action_issue_touched(
            issue_identifier="GD-328",
            project_name="Centralizing OS",
            issue_title="Add Issues Touched",
            status_name="In Progress",
            issue_url="https://linear.app/chapters/issue/gd-328/add-issues-touched",
            status_changed=False,
        )
    assert result["success"] is True
    _assert_in_updates_area(uploaded["content"], ISSUES_TOUCHED_HEADER)
