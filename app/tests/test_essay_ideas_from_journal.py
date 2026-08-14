"""Tests for essay ideas from journal email generation."""

import os
import sys
from datetime import date, datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.send_essay_ideas_from_journal import (
    build_html_email,
    format_obsidian_journal_filename,
    run_essay_ideas_from_journal,
)


def test_format_obsidian_journal_filename_matches_lowercase_convention():
    assert format_obsidian_journal_filename(date(2025, 12, 3)) == "dec 3, 2025.md"


def test_build_html_email_contains_sections_and_citations():
    html_body = build_html_email(
        journal_date=date(2026, 3, 1),
        essay_ideas="## Idea One\n- Explore attention and boredom.",
        supporting_materials="## Reading List\n- [Book](https://example.com/book)",
        citations=[{"title": "Example Book", "url": "https://example.com/book"}],
        generated_at=datetime(2026, 3, 2, 12, 30),
    )

    assert "Essay Ideas From Journal" in html_body
    assert "Based on journal entry from Mar 1, 2026" in html_body
    assert "Essay Ideas" in html_body
    assert "Supporting Materials" in html_body
    assert "Sources" in html_body
    assert "https://example.com/book" in html_body


def test_run_essay_ideas_from_journal_dry_run_writes_output(tmp_path):
    output_path = tmp_path / "essay_ideas.html"
    now = datetime(2026, 3, 2, 12, 30)

    with patch.dict(
        os.environ,
        {
            "DROPBOX_OBSIDIAN_VAULT_PATH": "/vault",
        },
        clear=False,
    ), patch(
        "scripts.send_essay_ideas_from_journal.get_dropbox_client",
        return_value=object(),
    ), patch(
        "scripts.send_essay_ideas_from_journal.fetch_yesterdays_journal_entry",
        return_value=(date(2026, 3, 1), "I kept thinking about ambition and community."),
    ), patch(
        "scripts.send_essay_ideas_from_journal._get_openai_client",
        return_value=object(),
    ), patch(
        "scripts.send_essay_ideas_from_journal.generate_essay_ideas",
        return_value="## Essay Ideas\n- Idea one",
    ), patch(
        "scripts.send_essay_ideas_from_journal.get_supporting_materials_with_web_search",
        return_value=(
            "## Supporting Materials\n- [Essay](https://example.com/essay)",
            [{"title": "Essay", "url": "https://example.com/essay"}],
        ),
    ), patch(
        "scripts.send_essay_ideas_from_journal.send_html_email"
    ) as mock_send:
        success = run_essay_ideas_from_journal(
            dry_run=True,
            output=str(output_path),
            now=now,
        )

    assert success is True
    assert output_path.exists()
    assert "Essay Ideas From Journal" in output_path.read_text(encoding="utf-8")
    mock_send.assert_not_called()
