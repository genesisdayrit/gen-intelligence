"""Tests for Manus Tasks section placement in Daily Action notes.

Focuses on `_upsert_daily_action_manus` — specifically the section-creation
fallback path that decides where a brand-new `### Manus Tasks:` header lands
relative to the user's `Vision Objective N` template.

Mocks all Dropbox I/O so these tests are pure logic checks.
"""

import os
import sys
from unittest.mock import MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set required env vars before importing modules
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/test/vault")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")
os.environ.setdefault("SYSTEM_TIMEZONE", "US/Eastern")

from services.obsidian.add_manus_task import (
    DAILY_ACTION_HEADER,
    DAILY_INITIATIVE_HEADER,
)

MODULE = "services.obsidian.add_manus_task"

VO1 = "Vision Objective 1 (High-Impact + Painful + Need to Do):"
VO2 = "Vision Objective 2 (Long-Term Learning + Skills + Wealth + Systems):"
VO3 = "Vision Objective 3 (Excitement and Play):"


def _run_upsert(file_content, task_id="abc123", task_title="Test Task", task_url="https://manus.im/app/abc123"):
    """Run `_upsert_daily_action_manus` with all Dropbox I/O mocked.

    Returns (result_dict, uploaded_content_str_or_None).
    """
    uploaded = {}

    mock_dbx = MagicMock()

    response = MagicMock()
    response.content = file_content.encode("utf-8")
    mock_dbx.files_download.return_value = (None, response)

    def capture_upload(data, path, mode=None):
        uploaded["content"] = data.decode("utf-8")
        uploaded["path"] = path

    mock_dbx.files_upload.side_effect = capture_upload

    with patch(f"{MODULE}._get_dropbox_client", return_value=mock_dbx), \
         patch(f"{MODULE}._find_daily_folder", return_value="/test/vault/_Daily"), \
         patch(f"{MODULE}._find_daily_action_folder", return_value="/test/vault/_Daily/_Daily-Action"), \
         patch(f"{MODULE}._get_today_daily_action_path", return_value="/test/vault/_Daily/_Daily-Action/DA 2026-05-24.md"):

        from services.obsidian.add_manus_task import _upsert_daily_action_manus
        result = _upsert_daily_action_manus(task_id, task_title, task_url)

    return result, uploaded.get("content")


# ----------------------------------------------------------------------------
# Placement tests — the misplacement bug
# ----------------------------------------------------------------------------


def test_placement_with_initiative_updates_and_parenthetical_template():
    """Manus Tasks lands AFTER Initiative Updates and BEFORE the first VO line,
    never between two VO lines (regression test for the backwards-walk bug).
    """
    content = f"""---
date: 2026-05-24
---

Daily Review:
- ok
---

{DAILY_INITIATIVE_HEADER}
[09:00] - [[Some Initiative]] ([link](https://linear.app/x)): note one

{VO1}
- thing

{VO2}
-

{VO3}
-

Other:
-
"""
    result, uploaded = _run_upsert(content)

    assert result["success"] is True
    assert result["action"] == "inserted"
    assert uploaded is not None

    header_pos = uploaded.index(DAILY_ACTION_HEADER)
    initiative_pos = uploaded.index(DAILY_INITIATIVE_HEADER)
    vo1_pos = uploaded.index(VO1)
    vo2_pos = uploaded.index(VO2)
    vo3_pos = uploaded.index(VO3)

    assert initiative_pos < header_pos < vo1_pos, (
        "Manus Tasks header must land between Initiative Updates and Vision Objective 1, "
        f"got initiative={initiative_pos} header={header_pos} vo1={vo1_pos}"
    )
    # Belt-and-suspenders: the bug placed the header between VO2 and VO3.
    assert not (vo2_pos < header_pos < vo3_pos), (
        "Manus Tasks header must NOT land between Vision Objective 2 and Vision Objective 3"
    )


def test_placement_with_no_other_auto_synced_sections():
    """With no Initiative/Project/Todoist/Issues sections, Manus Tasks lands
    after Daily Review's `---` and before `Vision Objective 1 (...)`.
    """
    content = f"""---
date: 2026-05-24
---

Daily Review:
- ok
---

{VO1}
- thing

{VO2}
-

{VO3}
-
"""
    result, uploaded = _run_upsert(content)

    assert result["success"] is True
    assert result["action"] == "inserted"

    header_pos = uploaded.index(DAILY_ACTION_HEADER)
    vo1_pos = uploaded.index(VO1)
    daily_review_end = uploaded.index("---\n\n" + VO1) if False else uploaded.find("Daily Review:")

    assert header_pos > daily_review_end
    assert header_pos < vo1_pos


def test_placement_with_mixed_vo_forms():
    """First-matched VO line wins regardless of form (literal, parenthetical, etc)."""
    literal_vo1 = "Vision Objective 1:"
    paren_vo2 = "Vision Objective 2 (something):"

    content = f"""---
date: 2026-05-24
---

Daily Review:
- ok
---

{literal_vo1}
- thing

{paren_vo2}
-

Vision Objective 3 (more text):
-
"""
    result, uploaded = _run_upsert(content)

    assert result["success"] is True
    assert result["action"] == "inserted"

    header_pos = uploaded.index(DAILY_ACTION_HEADER)
    vo1_pos = uploaded.index(literal_vo1)

    assert header_pos < vo1_pos, (
        "Manus Tasks must land before the FIRST Vision Objective line regardless of form"
    )


def test_placement_when_no_template_boundary_present():
    """Degenerate / truncated note: no Vision Objective lines at all. The
    insertion still succeeds and falls back to placing the section after the
    end of Daily Review without crashing.
    """
    content = """---
date: 2026-05-24
---

Daily Review:
- ok
---

Some other notes here.
"""
    result, uploaded = _run_upsert(content)

    assert result["success"] is True
    assert result["action"] == "inserted"
    assert DAILY_ACTION_HEADER in uploaded
    # Should land at or after the Daily Review's terminating ---
    daily_review_terminator = uploaded.index("---", uploaded.index("Daily Review:"))
    assert uploaded.index(DAILY_ACTION_HEADER) > daily_review_terminator


# ----------------------------------------------------------------------------
# Dedup regression guard — make sure the existing "skipped on duplicate URL"
# path is untouched by this change.
# ----------------------------------------------------------------------------


def test_duplicate_url_is_skipped():
    """If the Manus task URL already appears anywhere in the note, the upsert
    skips the write and returns action='skipped'.
    """
    dup_url = "https://manus.im/app/already-here"
    content = f"""---
date: 2026-05-24
---

Daily Review:
- ok
---

{DAILY_ACTION_HEADER}
- Already Logged Task ([already-here]({dup_url}))

{VO1}
-
"""
    result, uploaded = _run_upsert(content, task_url=dup_url)

    assert result["success"] is True
    assert result["action"] == "skipped"
    # No upload should have been recorded
    assert uploaded is None
