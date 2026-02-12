"""Tests for Linear Issues Touched section in Daily Action and Weekly Cycle.

These tests mock the Dropbox API to verify content manipulation without
making actual API calls (dry-run approach).
"""

import os
import sys
from unittest.mock import MagicMock, patch, call

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set required env vars before importing modules
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/test/vault")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")
os.environ.setdefault("SYSTEM_TIMEZONE", "US/Eastern")

from services.obsidian.add_daily_action_issues_touched import (
    _format_issue_entry,
    _to_native_app_url,
    _find_issues_touched_insert_position,
    _find_daily_review_end,
    ISSUES_TOUCHED_HEADER,
    TODOIST_COMPLETED_HEADER,
    INITIATIVE_UPDATES_HEADER,
    PROJECT_UPDATES_HEADER,
    TEMPLATE_BOUNDARY,
)
from services.obsidian.add_weekly_cycle_issues_touched import (
    ISSUES_TOUCHED_HEADER as WC_ISSUES_TOUCHED_HEADER,
    INITIATIVE_UPDATES_HEADER as WC_INITIATIVE_UPDATES_HEADER,
    PROJECT_UPDATES_HEADER as WC_PROJECT_UPDATES_HEADER,
    COMPLETED_TASKS_HEADER as WC_COMPLETED_TASKS_HEADER,
)

# Module path prefixes for patching
DA_MODULE = "services.obsidian.add_daily_action_issues_touched"
WC_MODULE = "services.obsidian.add_weekly_cycle_issues_touched"


# --- Test URL transformation ---

def test_to_native_app_url_standard():
    url = "https://linear.app/chapters/issue/gd-328/add-linear-issues-touched"
    assert _to_native_app_url(url) == "linear://chapters/issue/gd-328/add-linear-issues-touched"


def test_to_native_app_url_no_match():
    url = "https://other.app/something"
    assert _to_native_app_url(url) == "https://other.app/something"


def test_to_native_app_url_empty():
    assert _to_native_app_url("") == ""


# --- Test entry formatting ---

def test_format_issue_entry_with_project():
    entry = _format_issue_entry(
        issue_identifier="GD-328",
        project_name="Centralizing OS",
        issue_title="Add Linear Issues Touched",
        status_name="In Progress",
        issue_url="https://linear.app/chapters/issue/gd-328/add-linear-issues-touched",
    )
    assert entry == "GD-328 Centralizing OS - Add Linear Issues Touched (In Progress) ([link](linear://chapters/issue/gd-328/add-linear-issues-touched))"


def test_format_issue_entry_without_project():
    entry = _format_issue_entry(
        issue_identifier="GD-100",
        project_name="",
        issue_title="Standalone Issue",
        status_name="Todo",
        issue_url="https://linear.app/chapters/issue/gd-100/standalone",
    )
    assert entry == "GD-100 Standalone Issue (Todo) ([link](linear://chapters/issue/gd-100/standalone))"


def test_format_issue_entry_no_project_no_dash():
    """Test: Entry format when no project name - should not have ' - ' separator."""
    entry = _format_issue_entry(
        issue_identifier="GD-500",
        project_name="",
        issue_title="Orphan Issue",
        status_name="Backlog",
        issue_url="https://linear.app/chapters/issue/gd-500/orphan",
    )
    assert entry == "GD-500 Orphan Issue (Backlog) ([link](linear://chapters/issue/gd-500/orphan))"
    # Should not have " - " between ID and title when no project
    assert "GD-500 Orphan" in entry


# --- Test section insert position ---

def test_insert_position_after_todoist():
    """New section should go after Todoist if it exists."""
    content = f"""Daily Review:
Some review content
---

{INITIATIVE_UPDATES_HEADER}
[14:00] - [[Test Initiative]] ([link](url)): Update

{TODOIST_COMPLETED_HEADER}
[02:00 PM] Test task

{TEMPLATE_BOUNDARY}
template content"""
    lines = content.split('\n')
    daily_review_end = _find_daily_review_end(content)
    pos = _find_issues_touched_insert_position(lines, daily_review_end if daily_review_end else 0)
    # Should be after the Todoist entry line and before template boundary
    todoist_entry_idx = next(i for i, l in enumerate(lines) if "[02:00 PM]" in l)
    template_idx = next(i for i, l in enumerate(lines) if TEMPLATE_BOUNDARY in l)
    assert pos > todoist_entry_idx
    assert pos <= template_idx


def test_insert_position_after_project_updates_no_todoist():
    """If no Todoist section, should go after Project Updates."""
    content = f"""Daily Review:
review
---

{INITIATIVE_UPDATES_HEADER}
[14:00] - [[Test]] ([link](url)): Update

{PROJECT_UPDATES_HEADER}
[15:00] - [[Project]] ([link](url)): Update

{TEMPLATE_BOUNDARY}
template content"""
    lines = content.split('\n')
    daily_review_end = _find_daily_review_end(content)
    pos = _find_issues_touched_insert_position(lines, daily_review_end if daily_review_end else 0)
    project_line = next(i for i, l in enumerate(lines) if "[15:00]" in l)
    assert pos > project_line


def test_insert_position_before_template_boundary():
    """If no other sections, should go before template boundary."""
    content = f"""Daily Review:
review
---

{TEMPLATE_BOUNDARY}
template content"""
    lines = content.split('\n')
    daily_review_end = _find_daily_review_end(content)
    pos = _find_issues_touched_insert_position(lines, daily_review_end if daily_review_end else 0)
    template_line = next(i for i, l in enumerate(lines) if TEMPLATE_BOUNDARY in l)
    assert pos == template_line


# --- Test Daily Action upsert (mocked Dropbox functions) ---

SAMPLE_DAILY_ACTION_CONTENT = f"""---
date: 2026-02-12
---

Daily Review:
Had a good day
---

{INITIATIVE_UPDATES_HEADER}
[14:00] - [[Test Initiative]] ([link](https://linear.app/test)): Update content

{TODOIST_COMPLETED_HEADER}
[02:00 PM] Complete something

{TEMPLATE_BOUNDARY}
Vision Objective 1 content here"""


SAMPLE_DAILY_ACTION_WITH_ISSUES = f"""---
date: 2026-02-12
---

Daily Review:
Had a good day
---

{INITIATIVE_UPDATES_HEADER}
[14:00] - [[Test Initiative]] ([link](https://linear.app/test)): Update content

{TODOIST_COMPLETED_HEADER}
[02:00 PM] Complete something

{ISSUES_TOUCHED_HEADER}
GD-100 Project A - First Issue (Todo) ([link](linear://chapters/issue/gd-100/first))
GD-200 Project B - Second Issue (In Progress) ([link](linear://chapters/issue/gd-200/second))

{TEMPLATE_BOUNDARY}
Vision Objective 1 content here"""


def _run_daily_action_upsert(file_content, **kwargs):
    """Helper to run upsert_daily_action_issue_touched with mocked Dropbox I/O.

    Mocks the individual helper functions to bypass Dropbox entirely
    and just test the content manipulation logic.
    """
    uploaded = {}

    mock_dbx = MagicMock()

    # Mock files_download to return the content
    response = MagicMock()
    response.content = file_content.encode('utf-8')
    mock_dbx.files_download.return_value = (None, response)

    # Track upload
    def capture_upload(data, path, mode=None):
        uploaded['content'] = data.decode('utf-8')
        uploaded['path'] = path

    mock_dbx.files_upload.side_effect = capture_upload

    with patch(f"{DA_MODULE}._get_dropbox_client", return_value=mock_dbx), \
         patch(f"{DA_MODULE}._find_daily_folder", return_value="/test/vault/01_daily"), \
         patch(f"{DA_MODULE}._find_daily_action_folder", return_value="/test/vault/01_daily/01_daily-action"), \
         patch(f"{DA_MODULE}._get_today_daily_action_path", return_value="/test/vault/01_daily/01_daily-action/DA 2026-02-12.md"):

        from services.obsidian.add_daily_action_issues_touched import upsert_daily_action_issue_touched
        result = upsert_daily_action_issue_touched(**kwargs)

    return result, uploaded


def test_insert_new_issue_creates_section():
    """Test: New issue added when section doesn't exist yet."""
    result, uploaded = _run_daily_action_upsert(
        SAMPLE_DAILY_ACTION_CONTENT,
        issue_identifier="GD-328",
        project_name="Centralizing OS",
        issue_title="Add Issues Touched",
        status_name="In Progress",
        issue_url="https://linear.app/chapters/issue/gd-328/add-issues-touched",
        status_changed=False,
    )

    assert result["success"] is True
    assert result["action"] == "inserted"
    content = uploaded.get('content', '')
    assert ISSUES_TOUCHED_HEADER in content
    assert "GD-328 Centralizing OS - Add Issues Touched (In Progress)" in content
    assert "linear://chapters/issue/gd-328/add-issues-touched" in content
    # Section should be after Todoist and before template boundary
    issues_pos = content.index(ISSUES_TOUCHED_HEADER)
    todoist_pos = content.index(TODOIST_COMPLETED_HEADER)
    template_pos = content.index(TEMPLATE_BOUNDARY)
    assert todoist_pos < issues_pos < template_pos


def test_insert_new_issue_to_existing_section():
    """Test: New issue appended to existing section."""
    result, uploaded = _run_daily_action_upsert(
        SAMPLE_DAILY_ACTION_WITH_ISSUES,
        issue_identifier="GD-999",
        project_name="New Project",
        issue_title="Brand New Issue",
        status_name="Todo",
        issue_url="https://linear.app/chapters/issue/gd-999/brand-new",
        status_changed=False,
    )

    assert result["success"] is True
    assert result["action"] == "inserted"
    content = uploaded.get('content', '')
    assert "GD-999 New Project - Brand New Issue (Todo)" in content
    # Original entries should still be there
    assert "GD-100 Project A - First Issue (Todo)" in content
    assert "GD-200 Project B - Second Issue (In Progress)" in content


def test_update_existing_issue_status_changed():
    """Test: Existing issue with status change gets updated."""
    result, uploaded = _run_daily_action_upsert(
        SAMPLE_DAILY_ACTION_WITH_ISSUES,
        issue_identifier="GD-200",
        project_name="Project B",
        issue_title="Second Issue",
        status_name="Done",
        issue_url="https://linear.app/chapters/issue/gd-200/second",
        status_changed=True,
    )

    assert result["success"] is True
    assert result["action"] == "updated"
    content = uploaded.get('content', '')
    # Should have new status
    assert "GD-200 Project B - Second Issue (Done)" in content
    # Old entry line should be gone
    assert "Second Issue (In Progress)" not in content


def test_skip_existing_issue_no_status_change():
    """Test: Existing issue without status change is skipped (no-op)."""
    result, uploaded = _run_daily_action_upsert(
        SAMPLE_DAILY_ACTION_WITH_ISSUES,
        issue_identifier="GD-200",
        project_name="Project B",
        issue_title="Second Issue",
        status_name="In Progress",
        issue_url="https://linear.app/chapters/issue/gd-200/second",
        status_changed=False,
    )

    assert result["success"] is True
    assert result["action"] == "skipped"
    # No upload should have happened
    assert 'content' not in uploaded


def test_issue_found_by_id_not_title():
    """Test: Issue is found by identifier even if title changed."""
    result, uploaded = _run_daily_action_upsert(
        SAMPLE_DAILY_ACTION_WITH_ISSUES,
        issue_identifier="GD-100",
        project_name="Project A",
        issue_title="Renamed First Issue",
        status_name="In Progress",
        issue_url="https://linear.app/chapters/issue/gd-100/first",
        status_changed=True,
    )

    assert result["success"] is True
    assert result["action"] == "updated"
    content = uploaded.get('content', '')
    assert "GD-100 Project A - Renamed First Issue (In Progress)" in content
    # Old title should be gone
    assert "First Issue (Todo)" not in content


def test_issue_no_project():
    """Test: Issue without a project name."""
    result, uploaded = _run_daily_action_upsert(
        SAMPLE_DAILY_ACTION_CONTENT,
        issue_identifier="GD-500",
        project_name="",
        issue_title="No Project Issue",
        status_name="Backlog",
        issue_url="https://linear.app/chapters/issue/gd-500/no-project",
        status_changed=False,
    )

    assert result["success"] is True
    assert result["action"] == "inserted"
    content = uploaded.get('content', '')
    assert "GD-500 No Project Issue (Backlog)" in content
    # Should not have the " - " separator
    assert "GD-500 No Project Issue" in content


# =============================================================================
# Weekly Cycle Tests
# =============================================================================

SAMPLE_WEEKLY_CYCLE_CONTENT = f"""### Thursday -

{WC_INITIATIVE_UPDATES_HEADER}
[14:00] - [[Test Initiative]] ([link](https://linear.app/test)): Update content

{WC_COMPLETED_TASKS_HEADER}
[02:00 PM] Complete something

---
### Friday -

---"""


SAMPLE_WEEKLY_CYCLE_WITH_ISSUES = f"""### Thursday -

{WC_INITIATIVE_UPDATES_HEADER}
[14:00] - [[Test Initiative]] ([link](https://linear.app/test)): Update content

{WC_COMPLETED_TASKS_HEADER}
[02:00 PM] Complete something

{WC_ISSUES_TOUCHED_HEADER}
GD-100 Project A - First Issue (Todo) ([link](linear://chapters/issue/gd-100/first))
GD-200 Project B - Second Issue (In Progress) ([link](linear://chapters/issue/gd-200/second))

---
### Friday -

---"""


def _run_weekly_cycle_upsert(file_content, day_name="Thursday", **kwargs):
    """Helper to run upsert_weekly_cycle_issue_touched with mocked Dropbox I/O."""
    uploaded = {}

    mock_dbx = MagicMock()

    # Mock files_download to return the content
    response = MagicMock()
    response.content = file_content.encode('utf-8')
    mock_dbx.files_download.return_value = (None, response)

    # Mock files_get_metadata (verify _Weekly-Cycles folder exists)
    mock_dbx.files_get_metadata.return_value = MagicMock()

    # Track upload
    def capture_upload(data, path, mode=None):
        uploaded['content'] = data.decode('utf-8')
        uploaded['path'] = path

    mock_dbx.files_upload.side_effect = capture_upload

    with patch(f"{WC_MODULE}._get_dropbox_client", return_value=mock_dbx), \
         patch(f"{WC_MODULE}._find_cycles_folder", return_value="/test/vault/01_cycles"), \
         patch(f"{WC_MODULE}._get_current_week_bounds", return_value=(MagicMock(), MagicMock())), \
         patch(f"{WC_MODULE}._format_date_range", return_value="(Feb. 11 - Feb. 17, 2026)"), \
         patch(f"{WC_MODULE}._find_weekly_cycle_file", return_value=("/test/vault/01_cycles/_Weekly-Cycles/WC (Feb. 11 - Feb. 17, 2026).md", "WC (Feb. 11 - Feb. 17, 2026).md")), \
         patch(f"{WC_MODULE}._get_weekly_cycle_content", return_value=file_content), \
         patch(f"{WC_MODULE}._get_current_day_name", return_value=day_name):

        from services.obsidian.add_weekly_cycle_issues_touched import upsert_weekly_cycle_issue_touched
        result = upsert_weekly_cycle_issue_touched(**kwargs)

    return result, uploaded


def test_wc_insert_new_issue_creates_section():
    """Weekly Cycle: New issue added when section doesn't exist yet."""
    result, uploaded = _run_weekly_cycle_upsert(
        SAMPLE_WEEKLY_CYCLE_CONTENT,
        issue_identifier="GD-328",
        project_name="Centralizing OS",
        issue_title="Add Issues Touched",
        status_name="In Progress",
        issue_url="https://linear.app/chapters/issue/gd-328/add-issues-touched",
        status_changed=False,
    )

    assert result["success"] is True
    assert result["action"] == "inserted"
    content = uploaded.get('content', '')
    assert WC_ISSUES_TOUCHED_HEADER in content
    assert "GD-328 Centralizing OS - Add Issues Touched (In Progress)" in content
    assert "linear://chapters/issue/gd-328/add-issues-touched" in content
    # Section should be within the Thursday day section (before ---)
    thursday_pos = content.index("### Thursday -")
    issues_pos = content.index(WC_ISSUES_TOUCHED_HEADER)
    separator_pos = content.index("---", issues_pos)
    assert thursday_pos < issues_pos < separator_pos


def test_wc_insert_new_issue_to_existing_section():
    """Weekly Cycle: New issue appended to existing section."""
    result, uploaded = _run_weekly_cycle_upsert(
        SAMPLE_WEEKLY_CYCLE_WITH_ISSUES,
        issue_identifier="GD-999",
        project_name="New Project",
        issue_title="Brand New Issue",
        status_name="Todo",
        issue_url="https://linear.app/chapters/issue/gd-999/brand-new",
        status_changed=False,
    )

    assert result["success"] is True
    assert result["action"] == "inserted"
    content = uploaded.get('content', '')
    assert "GD-999 New Project - Brand New Issue (Todo)" in content
    # Original entries should still be there
    assert "GD-100 Project A - First Issue (Todo)" in content
    assert "GD-200 Project B - Second Issue (In Progress)" in content


def test_wc_update_existing_issue_status_changed():
    """Weekly Cycle: Existing issue with status change gets updated."""
    result, uploaded = _run_weekly_cycle_upsert(
        SAMPLE_WEEKLY_CYCLE_WITH_ISSUES,
        issue_identifier="GD-200",
        project_name="Project B",
        issue_title="Second Issue",
        status_name="Done",
        issue_url="https://linear.app/chapters/issue/gd-200/second",
        status_changed=True,
    )

    assert result["success"] is True
    assert result["action"] == "updated"
    content = uploaded.get('content', '')
    assert "GD-200 Project B - Second Issue (Done)" in content
    assert "Second Issue (In Progress)" not in content


def test_wc_skip_existing_issue_no_status_change():
    """Weekly Cycle: Existing issue without status change is skipped."""
    result, uploaded = _run_weekly_cycle_upsert(
        SAMPLE_WEEKLY_CYCLE_WITH_ISSUES,
        issue_identifier="GD-200",
        project_name="Project B",
        issue_title="Second Issue",
        status_name="In Progress",
        issue_url="https://linear.app/chapters/issue/gd-200/second",
        status_changed=False,
    )

    assert result["success"] is True
    assert result["action"] == "skipped"
    assert 'content' not in uploaded


def test_wc_issue_found_by_id_not_title():
    """Weekly Cycle: Issue found by identifier even when title changed."""
    result, uploaded = _run_weekly_cycle_upsert(
        SAMPLE_WEEKLY_CYCLE_WITH_ISSUES,
        issue_identifier="GD-100",
        project_name="Project A",
        issue_title="Renamed First Issue",
        status_name="In Progress",
        issue_url="https://linear.app/chapters/issue/gd-100/first",
        status_changed=True,
    )

    assert result["success"] is True
    assert result["action"] == "updated"
    content = uploaded.get('content', '')
    assert "GD-100 Project A - Renamed First Issue (In Progress)" in content
    assert "First Issue (Todo)" not in content


def test_wc_issue_scoped_to_day_section():
    """Weekly Cycle: Issues are scoped to the correct day section."""
    # Attempt to find an issue that exists in Thursday section, but we're
    # looking in Friday section - should insert, not find it
    content_with_friday = f"""### Thursday -

{WC_ISSUES_TOUCHED_HEADER}
GD-100 Project A - First Issue (Todo) ([link](linear://chapters/issue/gd-100/first))

---
### Friday -

---"""

    result, uploaded = _run_weekly_cycle_upsert(
        content_with_friday,
        day_name="Friday",
        issue_identifier="GD-100",
        project_name="Project A",
        issue_title="First Issue",
        status_name="In Progress",
        issue_url="https://linear.app/chapters/issue/gd-100/first",
        status_changed=True,
    )

    assert result["success"] is True
    assert result["action"] == "inserted"
    content = uploaded.get('content', '')
    # Should have a NEW issues touched section in Friday
    # The original Thursday entry should remain unchanged
    lines = content.split('\n')
    friday_idx = next(i for i, l in enumerate(lines) if "### Friday -" in l)
    friday_section = '\n'.join(lines[friday_idx:])
    assert WC_ISSUES_TOUCHED_HEADER in friday_section
    assert "GD-100 Project A - First Issue (In Progress)" in friday_section


def test_wc_missing_day_section_returns_error():
    """Weekly Cycle: Returns error when day section doesn't exist."""
    result, uploaded = _run_weekly_cycle_upsert(
        SAMPLE_WEEKLY_CYCLE_CONTENT,
        day_name="Saturday",  # Not in our sample content
        issue_identifier="GD-328",
        project_name="Test",
        issue_title="Test Issue",
        status_name="Todo",
        issue_url="https://linear.app/chapters/issue/gd-328/test",
        status_changed=False,
    )

    assert result["success"] is False
    assert "Saturday" in result["error"]


# --- Run tests ---

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
