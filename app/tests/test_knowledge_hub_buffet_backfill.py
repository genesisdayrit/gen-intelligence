"""Knowledge Hub Content Buffet backfill job tests."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")


@pytest.fixture(autouse=True)
def _force_la_timezone(monkeypatch):
    monkeypatch.setenv("SYSTEM_TIMEZONE", "America/Los_Angeles")
    monkeypatch.setenv("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")


from fastapi.testclient import TestClient

from main import app
from services.obsidian.backfill_knowledge_hub_buffet import (
    DEFAULT_SINCE,
    backfill_knowledge_hub_buffet,
    is_junk_note_stem,
    journal_date_label,
    parse_journal_wikilink,
)

client = TestClient(app)
LA = pytz.timezone("America/Los_Angeles")

KH_PATH = "/obsidian/personal/_knowledge-hub"
JOURNAL_FOLDER = "/obsidian/personal/01_daily/_journal"

JOURNAL_WITHOUT_BUFFET = """---
date: 2026-08-22
---

### Morning Pages

### Content Planning
- plan something
"""

JOURNAL_WITH_PLACEHOLDER = """---
date: 2026-08-22
---

### Content Buffet:
- 

### Content Planning
- plan something
"""

JOURNAL_WITH_ITEM = """---
date: 2026-08-22
---

### Content Buffet:
- [[My Article]]

### Content Planning
- plan something
"""


def _kh_note(*, journal_dates: list[str], title: str = "My Article") -> str:
    journal_yaml = "\n".join(f'  - "[[{d}]]"' for d in journal_dates)
    return f"""---
Journal:
{journal_yaml}
URL: https://example.com/article
---

## {title}
"""


def _mock_dropbox(kh_notes=None, journals=None, missing_journals=None):
    kh_notes = dict(kh_notes or {})
    journals = dict(journals or {})
    missing_journals = set(missing_journals or [])
    uploaded = []

    mock_dbx = MagicMock()

    def list_folder(path, recursive=False):
        result = MagicMock()
        result.has_more = False
        if path == KH_PATH:
            entries = []
            for name in kh_notes:
                entry = MagicMock()
                entry.name = name
                entry.path_lower = f"{KH_PATH}/{name}"
                entries.append(entry)
            result.entries = entries
        else:
            result.entries = []
        return result

    def download(path):
        filename = path.rsplit("/", 1)[-1]
        if path.startswith(KH_PATH) or filename in kh_notes:
            name = filename if filename in kh_notes else None
            if name is None:
                for candidate in kh_notes:
                    if path.endswith(candidate) or path.endswith(candidate.lower()):
                        name = candidate
                        break
            if name is None:
                raise FileNotFoundError(f"Note not found: {path}")
            response = MagicMock()
            response.content = kh_notes[name].encode("utf-8")
            return None, response

        if path in missing_journals or filename in {p.rsplit("/", 1)[-1] for p in missing_journals}:
            raise FileNotFoundError(f"Journal not found: {path}")
        if path not in journals:
            raise FileNotFoundError(f"Journal not found: {path}")
        response = MagicMock()
        response.content = journals[path].encode("utf-8")
        return None, response

    def upload(data, path, mode=None):
        text = data.decode("utf-8")
        journals[path] = text
        uploaded.append({"path": path, "content": text})
        return None

    mock_dbx.files_list_folder.side_effect = list_folder
    mock_dbx.files_download.side_effect = download
    mock_dbx.files_upload.side_effect = upload
    return mock_dbx, uploaded, journals


def _run_backfill(kh_notes=None, journals=None, missing_journals=None, **kwargs):
    mock_dbx, uploaded, store = _mock_dropbox(kh_notes, journals, missing_journals)
    with patch(
        "services.obsidian.backfill_knowledge_hub_buffet._get_dropbox_client",
        return_value=mock_dbx,
    ), patch(
        "services.obsidian.backfill_knowledge_hub_buffet._find_knowledge_hub_path",
        return_value=KH_PATH,
    ), patch(
        "services.obsidian.backfill_knowledge_hub_buffet._resolve_journal_folder",
        return_value=JOURNAL_FOLDER,
    ):
        result = backfill_knowledge_hub_buffet(**kwargs)
    return result, uploaded, store, mock_dbx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_default_since_is_all_journals_floor():
    assert DEFAULT_SINCE == "2018-01-01"


def test_parse_journal_wikilink_share_link_format():
    assert parse_journal_wikilink("[[Aug 22, 2026]]").isoformat() == "2026-08-22"
    assert parse_journal_wikilink('  - "[[Jan 1, 2018]]"').isoformat() == "2018-01-01"
    assert parse_journal_wikilink("not a date") is None


def test_journal_date_label_matches_share_link_filename():
    from datetime import date

    assert journal_date_label(date(2026, 8, 22)) == "Aug 22, 2026"


def test_junk_note_stems():
    assert is_junk_note_stem("") is True
    assert is_junk_note_stem(" ") is True
    assert is_junk_note_stem("%") is True
    assert is_junk_note_stem("+") is True
    assert is_junk_note_stem("_") is True
    assert is_junk_note_stem("A") is True
    assert is_junk_note_stem("My Article") is False


# ---------------------------------------------------------------------------
# Create-section + insert / missing / dedup / since / junk
# ---------------------------------------------------------------------------


def test_create_section_and_insert_on_existing_journal():
    aug_path = f"{JOURNAL_FOLDER}/Aug 22, 2026.md"
    result, uploaded, _, _ = _run_backfill(
        kh_notes={"My Article.md": _kh_note(journal_dates=["Aug 22, 2026"])},
        journals={aug_path: JOURNAL_WITHOUT_BUFFET},
    )
    assert result["notes_scanned"] == 1
    assert result["relations"] == 1
    assert result["files_written"] == 1
    assert result["lines_inserted"] == 1
    assert result["lines_skipped_dup"] == 0
    assert result["missing_journal_days"] == 0
    assert result["errors"] == []
    assert len(uploaded) == 1
    assert uploaded[0]["path"] == aug_path
    content = uploaded[0]["content"]
    assert "### Content Buffet:" in content
    buffet_idx = content.index("### Content Buffet:")
    planning_idx = content.index("### Content Planning")
    bullet_idx = content.index("- [[My Article]]")
    assert buffet_idx < bullet_idx < planning_idx


def test_missing_journal_is_skipped_not_created():
    result, uploaded, _, mock_dbx = _run_backfill(
        kh_notes={"My Article.md": _kh_note(journal_dates=["Mar 15, 2019"])},
        missing_journals={f"{JOURNAL_FOLDER}/Mar 15, 2019.md"},
        since="2019-01-01",
    )
    assert result["notes_scanned"] == 1
    assert result["relations"] == 1
    assert result["missing_journal_days"] == 1
    assert result["files_written"] == 0
    assert result["lines_inserted"] == 0
    assert result["errors"] == []
    assert uploaded == []
    mock_dbx.files_upload.assert_not_called()
    assert not any("today" in (u.get("path") or "") for u in uploaded)


def test_duplicate_wikilink_skipped_on_rerun():
    aug_path = f"{JOURNAL_FOLDER}/Aug 22, 2026.md"
    first, uploaded_first, store, _ = _run_backfill(
        kh_notes={"My Article.md": _kh_note(journal_dates=["Aug 22, 2026"])},
        journals={aug_path: JOURNAL_WITH_PLACEHOLDER},
    )
    assert first["lines_inserted"] == 1
    assert first["files_written"] == 1
    assert "- [[My Article]]" in uploaded_first[0]["content"]

    second, uploaded_second, _, _ = _run_backfill(
        kh_notes={"My Article.md": _kh_note(journal_dates=["Aug 22, 2026"])},
        journals=store,
    )
    assert second["relations"] == 1
    assert second["lines_skipped_dup"] == 1
    assert second["lines_inserted"] == 0
    assert second["files_written"] == 0
    assert uploaded_second == []


def test_since_excludes_older_journal_dates():
    aug_path = f"{JOURNAL_FOLDER}/Aug 22, 2026.md"
    jan_path = f"{JOURNAL_FOLDER}/Jan 1, 2017.md"
    result, uploaded, _, _ = _run_backfill(
        kh_notes={
            "My Article.md": _kh_note(journal_dates=["Jan 1, 2017", "Aug 22, 2026"]),
        },
        journals={
            aug_path: JOURNAL_WITHOUT_BUFFET,
            jan_path: JOURNAL_WITHOUT_BUFFET,
        },
        since="2018-01-01",
    )
    assert result["notes_scanned"] == 1
    assert result["relations"] == 1
    assert result["files_written"] == 1
    assert [u["path"] for u in uploaded] == [aug_path]
    assert "Jan 1, 2017" not in "".join(u["path"] for u in uploaded)
    assert "- [[My Article]]" in uploaded[0]["content"]


def test_junk_filename_stems_are_skipped():
    aug_path = f"{JOURNAL_FOLDER}/Aug 22, 2026.md"
    junk_body = _kh_note(journal_dates=["Aug 22, 2026"])
    result, uploaded, _, _ = _run_backfill(
        kh_notes={
            "%.md": junk_body,
            "+.md": junk_body,
            "_.md": junk_body,
            "A.md": junk_body,
            "Keep This.md": _kh_note(journal_dates=["Aug 22, 2026"], title="Keep This"),
        },
        journals={aug_path: JOURNAL_WITHOUT_BUFFET},
    )
    assert result["notes_scanned"] == 5
    assert result["relations"] == 1
    assert result["files_written"] == 1
    assert result["lines_inserted"] == 1
    section = uploaded[0]["content"].split("### Content Buffet:")[1].split("### Content Planning")[0]
    assert "- [[Keep This]]" in section
    assert "- [[%]]" not in section
    assert "- [[+]]" not in section
    assert "- [[_]]" not in section
    assert "- [[A]]" not in section


def test_two_notes_same_day_write_journal_once():
    aug_path = f"{JOURNAL_FOLDER}/Aug 22, 2026.md"
    result, uploaded, _, _ = _run_backfill(
        kh_notes={
            "First Note.md": _kh_note(journal_dates=["Aug 22, 2026"], title="First Note"),
            "Second Note.md": _kh_note(journal_dates=["Aug 22, 2026"], title="Second Note"),
        },
        journals={aug_path: JOURNAL_WITHOUT_BUFFET},
    )
    assert result["notes_scanned"] == 2
    assert result["relations"] == 2
    assert result["files_written"] == 1
    assert result["lines_inserted"] == 2
    assert len(uploaded) == 1
    assert "- [[First Note]]" in uploaded[0]["content"]
    assert "- [[Second Note]]" in uploaded[0]["content"]


def test_already_present_title_counts_as_dup_not_write():
    aug_path = f"{JOURNAL_FOLDER}/Aug 22, 2026.md"
    result, uploaded, _, _ = _run_backfill(
        kh_notes={"My Article.md": _kh_note(journal_dates=["Aug 22, 2026"])},
        journals={aug_path: JOURNAL_WITH_ITEM},
    )
    assert result["lines_skipped_dup"] == 1
    assert result["lines_inserted"] == 0
    assert result["files_written"] == 0
    assert uploaded == []


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def test_backfill_job_is_registered_with_2099_trigger():
    from scheduler import SCHEDULED_JOBS

    job = next(j for j in SCHEDULED_JOBS if j["id"] == "backfill_knowledge_hub_buffet")
    assert job["name"] == "Backfill Knowledge Hub Content Buffet (manual)"
    trigger_str = str(job["trigger"])
    assert "2099" in trigger_str


def test_trigger_backfill_passes_since_query_param():
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            "/scheduler/jobs/backfill_knowledge_hub_buffet/run",
            params={"since": "2020-01-01"},
        )
    assert response.status_code == 200
    assert response.json()["job_id"] == "backfill_knowledge_hub_buffet"
    assert response.json()["since"] == "2020-01-01"
    assert "updated_after" not in response.json()
    mock_run.assert_called_once_with("backfill_knowledge_hub_buffet", since="2020-01-01")


def test_trigger_other_job_does_not_forward_since():
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            "/scheduler/jobs/send_arxiv_email/run",
            params={"since": "2020-01-01"},
        )
    assert response.status_code == 200
    mock_run.assert_called_once_with("send_arxiv_email")
