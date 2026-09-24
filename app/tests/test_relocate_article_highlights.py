"""Relocate ### Article highlights from under the KH title to above it."""

import os
import sys
from unittest.mock import MagicMock

import dropbox

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ.setdefault("SYSTEM_TIMEZONE", "America/Los_Angeles")
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")

from services.obsidian.add_readwise_buffet import (  # noqa: E402
    ARTICLE_HIGHLIGHTS_HEADER,
    BOOK_HIGHLIGHTS_HEADER,
    BOOKMARKED_TWEETS_HEADER,
    TRANSCRIPT_HIGHLIGHTS_HEADER,
    reader_knowledge_hub_note_stem,
)
from services.obsidian.relocate_article_highlights import (  # noqa: E402
    analyze_article_highlights_relocation,
    classify_article_highlights_placement,
    format_candidate_report,
    process_note,
    relocate_article_highlights_above_title,
    run_relocate_article_highlights,
    verified_title_heading_index,
)

REV = "aaaaaaaaaaaaaaaa"
KH = "/obsidian/personal/01_knowledge-hub"


def _after_title() -> str:
    return """---
title: "A long essay by The Verge"
author: "[[The Verge]]"
URL: https://www.theverge.com/long-essay
---

# A long essay by The Verge

### Article highlights
- "Most Amazing Highlight Ever" ([Link](https://readwise.io/open/954480))

Scraped article body that must stay after the title.
"""


def _already_correct() -> str:
    return """---
title: "A long essay by The Verge"
author: "[[The Verge]]"
URL: https://www.theverge.com/long-essay
---

### Article highlights
- "Most Amazing Highlight Ever" ([Link](https://readwise.io/open/954480))

# A long essay by The Verge

Scraped article body that must stay after the title.
"""


def _no_section() -> str:
    return """---
title: "A long essay by The Verge"
---

# A long essay by The Verge

Just the article.
"""


def _section_at_bottom() -> str:
    return """---
title: "A long essay by The Verge"
---

# A long essay by The Verge

Long scraped article with
multiple paragraphs.

- a body bullet that is not a highlight

### Article highlights
- "late quote" ([Link](https://readwise.io/open/222))
"""


def _with_book_and_transcript() -> str:
    return """---
title: "A long essay by The Verge"
---

# A long essay by The Verge

### Book highlights
- "book quote" ([Link](https://readwise.io/open/111))

### Article highlights
- "article quote" ([Link](https://readwise.io/open/222))

### Transcript Highlights
- "transcript quote" ([Link](https://readwise.io/open/333))

### Bookmarked Tweets
- "tweet quote" ([Link](https://readwise.io/open/444))
"""


def test_relocate_moves_section_from_after_title_to_before():
    content = _after_title()
    updated, changed = relocate_article_highlights_above_title(content)
    assert changed is True
    assert updated != content
    assert updated.index(ARTICLE_HIGHLIGHTS_HEADER) < updated.index(
        "# A long essay by The Verge"
    )
    assert updated.index("# A long essay by The Verge") < updated.index(
        "Scraped article body that must stay after the title."
    )
    assert '- "Most Amazing Highlight Ever" ([Link](https://readwise.io/open/954480))' in updated
    assert updated.startswith(
        '---\ntitle: "A long essay by The Verge"\nauthor: "[[The Verge]]"\n'
        "URL: https://www.theverge.com/long-essay\n---\n"
    )
    assert updated.count(ARTICLE_HIGHLIGHTS_HEADER) == 1
    again, changed_again = relocate_article_highlights_above_title(updated)
    assert changed_again is False
    assert again == updated


def test_relocate_already_correct_is_unchanged():
    content = _already_correct()
    updated, changed = relocate_article_highlights_above_title(content)
    assert changed is False
    assert updated == content
    assert updated is content


def test_relocate_no_section_is_unchanged():
    content = _no_section()
    updated, changed = relocate_article_highlights_above_title(content)
    assert changed is False
    assert updated == content
    assert classify_article_highlights_placement(content) == "no_section"


def test_relocate_section_at_bottom_after_body_still_moves_above_title():
    content = _section_at_bottom()
    updated, changed = relocate_article_highlights_above_title(content)
    assert changed is True
    assert updated.index(ARTICLE_HIGHLIGHTS_HEADER) < updated.index(
        "# A long essay by The Verge"
    )
    assert updated.index("# A long essay by The Verge") < updated.index(
        "Long scraped article with"
    )
    highlights_at = updated.index(ARTICLE_HIGHLIGHTS_HEADER)
    title_at = updated.index("# A long essay by The Verge")
    body_bullet_at = updated.index("- a body bullet that is not a highlight")
    assert highlights_at < title_at < body_bullet_at
    assert '- "late quote" ([Link](https://readwise.io/open/222))' in updated[
        highlights_at:title_at
    ]


def test_relocate_ignores_book_tweet_and_transcript_headers():
    content = _with_book_and_transcript()
    updated, changed = relocate_article_highlights_above_title(content)
    assert changed is True
    assert updated.index(ARTICLE_HIGHLIGHTS_HEADER) < updated.index(
        "# A long essay by The Verge"
    )
    title_at = updated.index("# A long essay by The Verge")
    assert updated.index(BOOK_HIGHLIGHTS_HEADER) > title_at
    assert updated.index(TRANSCRIPT_HIGHLIGHTS_HEADER) > title_at
    assert updated.index(BOOKMARKED_TWEETS_HEADER) > title_at
    assert updated.index(BOOK_HIGHLIGHTS_HEADER) < updated.index(
        TRANSCRIPT_HIGHLIGHTS_HEADER
    )
    assert '- "book quote"' in updated
    assert '- "transcript quote"' in updated
    assert '- "tweet quote"' in updated
    book_only = """---
title: "A long essay by The Verge"
---

# A long essay by The Verge

### Book highlights
- "book quote" ([Link](https://readwise.io/open/111))

### Transcript Highlights
- "transcript quote" ([Link](https://readwise.io/open/333))

### Bookmarked Tweets
- "tweet quote" ([Link](https://readwise.io/open/444))
"""
    unchanged, book_changed = relocate_article_highlights_above_title(book_only)
    assert book_changed is False
    assert unchanged == book_only


def test_relocate_h2_matching_yaml_title():
    h2 = """---
title: "A long essay"
URL: https://www.theverge.com/long-essay
---

## A long essay

Scraped article body that must stay after the title.

### Article highlights
- "quote"
"""
    updated, changed = relocate_article_highlights_above_title(h2)
    assert changed is True
    assert updated.index(ARTICLE_HIGHLIGHTS_HEADER) < updated.index("## A long essay")
    assert updated.index("## A long essay") < updated.index(
        "Scraped article body that must stay after the title."
    )


def test_relocate_h2_matching_filename_stem_without_yaml_title():
    h2 = """---
URL: https://www.theverge.com/long-essay
---

## A long essay

Scraped article body that must stay after the title.

### Article highlights
- "quote"
"""
    updated, changed = relocate_article_highlights_above_title(
        h2, path=f"{KH}/A long essay.md"
    )
    assert changed is True
    assert updated.index(ARTICLE_HIGHLIGHTS_HEADER) < updated.index("## A long essay")


def test_relocate_matches_title_by_author_stem_not_bare_article_title():
    content = """---
title: "A long essay"
author: "[[The Verge]]"
---

# Something else in the scrape

# A long essay by The Verge

### Article highlights
- "quote"
"""
    stem = reader_knowledge_hub_note_stem("A long essay", "[[The Verge]]")
    assert stem == "A long essay by The Verge"
    updated, changed = relocate_article_highlights_above_title(content)
    assert changed is True
    assert updated.index("# Something else in the scrape") < updated.index(
        ARTICLE_HIGHLIGHTS_HEADER
    )
    assert updated.index(ARTICLE_HIGHLIGHTS_HEADER) < updated.index(
        "# A long essay by The Verge"
    )


def test_relocate_skips_when_first_heading_is_unverified_scrape():
    content = """---
title: "A long essay by The Verge"
---

# A completely different scraped headline

Article body under a scrape H1.

### Article highlights
- "quote"
"""
    updated, changed = relocate_article_highlights_above_title(content)
    assert changed is False
    assert updated == content
    assert classify_article_highlights_placement(content) == "no_verified_title"


def test_relocate_uses_first_matching_heading_not_later_scrape_repeat():
    content = """---
title: "A long essay by The Verge"
---

# A long essay by The Verge

Intro.

# A long essay by The Verge

### Article highlights
- "quote"
"""
    updated, changed = relocate_article_highlights_above_title(content)
    assert changed is True
    first_title = updated.index("# A long essay by The Verge")
    highlights = updated.index(ARTICLE_HIGHLIGHTS_HEADER)
    second_title = updated.index("# A long essay by The Verge", first_title + 1)
    assert highlights < first_title < second_title


def test_relocate_no_heading_is_no_verified_title():
    no_title = """---
title: "No heading note"
---

### Article highlights
- "quote"

Just a paragraph at the top of the body.
"""
    same, no_change = relocate_article_highlights_above_title(no_title)
    assert no_change is False
    assert same == no_title
    assert classify_article_highlights_placement(no_title) == "no_verified_title"


def test_verified_title_ignores_h3_and_requires_text_match():
    lines = [
        "---",
        'title: "A long essay by The Verge"',
        "---",
        "",
        "### Article highlights",
        "# Not the title",
        "## A long essay by The Verge",
    ]
    assert (
        verified_title_heading_index(
            lines, 3, ["A long essay by The Verge"]
        )
        == 6
    )
    assert verified_title_heading_index(lines, 3, ["Missing"]) is None


def test_dry_run_report_includes_titles_and_before_after_sketch():
    analysis = analyze_article_highlights_relocation(_after_title())
    assert analysis.placement == "needs_move"
    report = format_candidate_report(
        f"{KH}/A long essay by The Verge.md", "would_move", analysis
    )
    assert "A long essay by The Verge.md\twould_move" in report
    assert "matched_title: # A long essay by The Verge" in report
    assert "yaml_title: A long essay by The Verge" in report
    assert "before:" in report
    assert "after:" in report
    assert "# A long essay by The Verge" in report
    assert ARTICLE_HIGHLIGHTS_HEADER in report
    before_at = report.index("before:")
    after_at = report.index("after:")
    assert before_at < after_at


def _download(content: str, *, rev: str = REV, path: str | None = None):
    metadata = MagicMock()
    metadata.rev = rev
    metadata.path_display = path
    response = MagicMock()
    response.content = content.encode("utf-8")
    return metadata, response


def _rev_conflict() -> dropbox.exceptions.ApiError:
    reason = dropbox.files.WriteError.conflict(dropbox.files.WriteConflictError.file)
    failed = dropbox.files.UploadWriteFailed(reason=reason, upload_session_id="sess")
    error = dropbox.files.UploadError.path(failed)
    return dropbox.exceptions.ApiError("req", error, "", "")


def test_process_note_dry_run_does_not_upload():
    mock_dbx = MagicMock()
    path = f"{KH}/A long essay by The Verge.md"
    mock_dbx.files_download.return_value = _download(_after_title(), path=path)

    action, analysis = process_note(mock_dbx, path, apply=False)

    assert action == "would_move"
    assert analysis is not None
    assert analysis.matched_title_line == "# A long essay by The Verge"
    assert analysis.yaml_title == "A long essay by The Verge"
    assert analysis.filename_stem == "A long essay by The Verge"
    mock_dbx.files_upload.assert_not_called()


def test_process_note_apply_uploads_rev_safe():
    mock_dbx = MagicMock()
    path = f"{KH}/A long essay by The Verge.md"
    mock_dbx.files_download.return_value = _download(_after_title(), path=path)

    action, _analysis = process_note(mock_dbx, path, apply=True)

    assert action == "moved"
    mock_dbx.files_upload.assert_called_once()
    kwargs = mock_dbx.files_upload.call_args.kwargs
    uploaded = mock_dbx.files_upload.call_args.args[0].decode("utf-8")
    assert kwargs["autorename"] is False
    assert kwargs["mode"].is_update()
    assert kwargs["mode"].get_update() == REV
    assert not kwargs["mode"].is_overwrite()
    assert uploaded.index(ARTICLE_HIGHLIGHTS_HEADER) < uploaded.index(
        "# A long essay by The Verge"
    )


def test_process_note_skips_already_correct_conflicted_rev_and_unverified():
    mock_dbx = MagicMock()
    good = f"{KH}/A long essay by The Verge.md"
    mock_dbx.files_download.return_value = _download(_already_correct(), path=good)
    action, _analysis = process_note(mock_dbx, good, apply=True)
    assert action == "skipped_already_correct"
    mock_dbx.files_upload.assert_not_called()

    conflicted = f"{KH}/Essay (MacBook Pro's conflicted copy 2026-09-19).md"
    action, _analysis = process_note(mock_dbx, conflicted, apply=True)
    assert action == "skipped_conflicted"
    mock_dbx.files_upload.assert_not_called()

    scrape_only = """---
title: "A long essay by The Verge"
---

# Scraped headline that is not the note title

### Article highlights
- "quote"
"""
    mock_dbx.files_download.return_value = _download(scrape_only, path=good)
    action, analysis = process_note(mock_dbx, good, apply=True)
    assert action == "skipped_no_verified_title"
    assert analysis is not None
    assert analysis.matched_title_line is None
    mock_dbx.files_upload.assert_not_called()

    mock_dbx.files_download.return_value = _download(_after_title(), path=good)
    mock_dbx.files_upload.side_effect = _rev_conflict()
    action, _analysis = process_note(mock_dbx, good, apply=True)
    assert action == "skipped_rev"
    mode = mock_dbx.files_upload.call_args.kwargs["mode"]
    assert mode.is_update()
    assert not mode.is_overwrite()


def test_run_apply_is_idempotent_on_already_migrated_notes():
    mock_dbx = MagicMock()
    path = f"{KH}/A long essay by The Verge.md"
    mock_dbx.files_download.return_value = _download(_already_correct(), path=path)

    counts = run_relocate_article_highlights(
        apply=True, dbx=mock_dbx, paths=[path]
    )

    assert counts["moved"] == 0
    assert counts["would_move"] == 0
    assert counts["skipped_already_correct"] == 1
    mock_dbx.files_upload.assert_not_called()


def test_cli_requires_apply_flag_to_write():
    from services.obsidian.relocate_article_highlights import build_parser

    assert build_parser().parse_args([]).apply is False
    assert build_parser().parse_args(["--apply"]).apply is True
