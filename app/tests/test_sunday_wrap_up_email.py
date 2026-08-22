"""Tests for Sunday wrap-up email section builders and dry-run."""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"

from services.readwise.reader import is_parent_reader_document
from scripts.send_sunday_wrap_up_email import (
    build_html_email,
    classify_kh_kind,
    collect_section,
    filter_documents_in_window,
    filter_highlights_in_window,
    group_linear_completed,
    highlight_source_label,
    journal_dates_from_frontmatter,
    notes_consumed_in_window,
    parse_journal_wikilink,
    rolling_window,
    run_sunday_wrap_up_email,
    select_linear_completed,
    sort_repos_by_commit_count,
    split_readwise_items,
    sunday_wrap_up_subject,
    window_journal_dates,
)

LA = pytz.timezone("America/Los_Angeles")


def _local(year, month, day, hour=6, minute=0):
    return LA.localize(datetime(year, month, day, hour, minute))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


def test_rolling_window_is_seven_days_ending_at_run_time():
    now = _local(2026, 8, 23, 6, 0)
    start, end = rolling_window(now)
    assert end == now
    assert start == now - timedelta(days=7)
    assert start.tzinfo.zone == "America/Los_Angeles"
    assert end.tzinfo.zone == "America/Los_Angeles"


def test_window_journal_dates_include_local_calendar_days():
    start, end = rolling_window(_local(2026, 8, 23, 6, 0))
    dates = window_journal_dates(start, end)
    assert date(2026, 8, 16) in dates
    assert date(2026, 8, 23) in dates
    assert date(2026, 8, 15) not in dates


def test_sunday_wrap_up_subject_uses_mon_d():
    assert sunday_wrap_up_subject(_local(2026, 8, 23, 6, 0)) == "Sunday wrap-up — Aug 23"


# ---------------------------------------------------------------------------
# Readwise
# ---------------------------------------------------------------------------


def test_filter_highlights_in_window_uses_highlighted_at():
    now = _local(2026, 8, 23, 6, 0)
    start, end = rolling_window(now)
    in_window = {
        "id": 1,
        "title": "Deep Work",
        "author": "Cal Newport",
        "text": "Focus is a skill.",
        "highlighted_at": _iso(now - timedelta(days=2)),
    }
    too_old = {
        "id": 2,
        "title": "Old Book",
        "author": "Someone",
        "text": "Stale.",
        "highlighted_at": _iso(now - timedelta(days=10)),
    }
    selected = filter_highlights_in_window([in_window, too_old], start, end)
    assert [item["id"] for item in selected] == [1]


def test_readwise_splits_highlights_and_documents():
    highlights = [{"id": 1, "title": "Book", "text": "Quote"}]
    documents = [{"id": "doc-1", "title": "Article"}]
    split = split_readwise_items(highlights, documents)
    assert split["highlights"] == highlights
    assert split["documents"] == documents
    assert "highlights" in split and "documents" in split


def test_filter_documents_in_window_uses_created_at():
    now = _local(2026, 8, 23, 6, 0)
    start, end = rolling_window(now)
    created = {
        "id": "doc-1",
        "title": "New save",
        "created_at": _iso(now - timedelta(days=1)),
    }
    old = {
        "id": "doc-2",
        "title": "Old save",
        "created_at": _iso(now - timedelta(days=20)),
    }
    selected = filter_documents_in_window([created, old], start, end)
    assert [item["id"] for item in selected] == ["doc-1"]


def test_readwise_empty_sources_are_empty_lists():
    now = _local(2026, 8, 23, 6, 0)
    start, end = rolling_window(now)
    assert filter_highlights_in_window([], start, end) == []
    assert filter_documents_in_window([], start, end) == []
    split = split_readwise_items([], [])
    assert split["highlights"] == []
    assert split["documents"] == []


def test_reader_parent_documents_skip_highlights_and_notes():
    assert is_parent_reader_document({"category": "article", "parent_id": None}) is True
    assert is_parent_reader_document({"category": "highlight"}) is False
    assert is_parent_reader_document({"category": "note"}) is False
    assert is_parent_reader_document({"category": "article", "parent_id": "parent-1"}) is False


def test_highlight_source_label_prefers_tweet_handle():
    tweet = {
        "title": "Tweets from @naval",
        "author": "Naval Ravikant on Twitter",
        "category": "tweets",
        "source": "twitter",
        "source_url": "https://twitter.com/naval/status/1",
    }
    book = {
        "title": "Deep Work",
        "author": "Cal Newport",
        "category": "books",
    }
    assert highlight_source_label(tweet) == "@naval"
    assert highlight_source_label(book) == "Cal Newport"


# ---------------------------------------------------------------------------
# Knowledge Hub
# ---------------------------------------------------------------------------


def test_parse_journal_wikilink_mon_d_yyyy():
    assert parse_journal_wikilink("[[Aug 20, 2026]]") == date(2026, 8, 20)
    assert parse_journal_wikilink('  - "[[Aug 20, 2026]]"') == date(2026, 8, 20)
    assert parse_journal_wikilink("not a date") is None


def test_kh_notes_match_journal_dates_in_window():
    window_dates = {date(2026, 8, 20), date(2026, 8, 21)}
    notes = [
        {
            "title": "In Window Article",
            "kind": "article",
            "journal_dates": [date(2026, 8, 20)],
        },
        {
            "title": "Older Note",
            "kind": "article",
            "journal_dates": [date(2026, 7, 1)],
        },
    ]
    matched = notes_consumed_in_window(notes, window_dates)
    assert [note["title"] for note in matched] == ["In Window Article"]


def test_kh_empty_when_no_journal_overlap():
    assert notes_consumed_in_window([], {date(2026, 8, 20)}) == []
    notes = [{"title": "Miss", "journal_dates": [date(2026, 1, 1)]}]
    assert notes_consumed_in_window(notes, {date(2026, 8, 20)}) == []


def test_kh_classifies_youtube_vs_article():
    youtube = {
        "Tags": ["youtube"],
        "URL": "https://www.youtube.com/watch?v=abcdefghijk",
    }
    article = {
        "Tags": [],
        "URL": "https://example.com/essay",
    }
    url_only_youtube = {"URL": "https://youtu.be/abcdefghijk"}
    assert classify_kh_kind(youtube) == "youtube"
    assert classify_kh_kind(article) == "article"
    assert classify_kh_kind(url_only_youtube) == "youtube"


def test_journal_dates_from_frontmatter_list():
    frontmatter = {"Journal": ['[[Aug 20, 2026]]', "[[Aug 21, 2026]]"]}
    assert journal_dates_from_frontmatter(frontmatter) == [
        date(2026, 8, 20),
        date(2026, 8, 21),
    ]


# ---------------------------------------------------------------------------
# Linear
# ---------------------------------------------------------------------------


def _completed_issue(
    issue_id,
    identifier,
    title,
    *,
    project="Digest",
    initiative=None,
    project_initiatives=None,
    completed_at=None,
    state_type="completed",
    assignee_id="viewer-1",
    completed_by_id=None,
):
    issue = {
        "id": issue_id,
        "identifier": identifier,
        "title": title,
        "url": f"https://linear.app/chapters/issue/{identifier.lower()}",
        "completedAt": _iso(completed_at or _local(2026, 8, 20, 12, 0)),
        "state": {"name": "Done", "type": state_type},
        "assignee": {"id": assignee_id} if assignee_id else None,
        "project": {
            "id": f"proj-{project}",
            "name": project,
            "initiatives": {"nodes": project_initiatives or []},
        },
    }
    if initiative:
        issue["initiative"] = initiative
    if completed_by_id:
        issue["completedBy"] = {"id": completed_by_id}
    return issue


def test_linear_groups_by_initiative_then_project():
    now = _local(2026, 8, 23, 6, 0)
    start, end = rolling_window(now)
    raw = [
        _completed_issue(
            "i-1",
            "GD-1",
            "Alpha work",
            project="Alpha",
            initiative={"id": "init-a", "name": "Initiative A"},
        ),
        _completed_issue(
            "i-2",
            "GD-2",
            "Beta work",
            project="Beta",
            initiative={"id": "init-a", "name": "Initiative A"},
        ),
        _completed_issue(
            "i-3",
            "GD-3",
            "No initiative work",
            project="Chores",
        ),
        _completed_issue(
            "i-4",
            "GD-4",
            "From project initiatives",
            project="Gamma",
            project_initiatives=[{"id": "init-b", "name": "Initiative B"}],
        ),
    ]
    selected = select_linear_completed(raw, "viewer-1", start, end)
    groups = group_linear_completed(selected)
    names = [group["initiative_name"] for group in groups]
    assert names == ["Initiative A", "Initiative B", "No initiative"]

    init_a = groups[0]
    assert [project["project_name"] for project in init_a["projects"]] == ["Alpha", "Beta"]
    assert [issue["identifier"] for issue in init_a["projects"][0]["issues"]] == ["GD-1"]

    chores = next(
        project
        for group in groups
        if group["initiative_name"] == "No initiative"
        for project in group["projects"]
    )
    assert chores["project_name"] == "Chores"


def test_linear_excludes_canceled_and_other_viewers():
    now = _local(2026, 8, 23, 6, 0)
    start, end = rolling_window(now)
    raw = [
        _completed_issue("i-1", "GD-1", "Mine", assignee_id="viewer-1"),
        _completed_issue(
            "i-2",
            "GD-2",
            "Canceled",
            state_type="canceled",
            assignee_id="viewer-1",
        ),
        _completed_issue("i-3", "GD-3", "Someone else", assignee_id="other"),
        _completed_issue(
            "i-4",
            "GD-4",
            "Completed by viewer",
            assignee_id="other",
            completed_by_id="viewer-1",
        ),
    ]
    selected = select_linear_completed(raw, "viewer-1", start, end)
    assert [issue["identifier"] for issue in selected] == ["GD-1", "GD-4"]


def test_linear_empty_when_no_completed_issues():
    now = _local(2026, 8, 23, 6, 0)
    start, end = rolling_window(now)
    assert select_linear_completed([], "viewer-1", start, end) == []
    assert group_linear_completed([]) == []


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


def test_git_repos_sorted_by_commit_count_descending():
    repos = [
        {
            "repo": "me/zeta",
            "commits": [{"sha": "aaa1111", "message": "one"}],
        },
        {
            "repo": "me/alpha",
            "commits": [
                {"sha": "bbb2222", "message": "two"},
                {"sha": "ccc3333", "message": "three"},
                {"sha": "ddd4444", "message": "four"},
            ],
        },
        {
            "repo": "me/mid",
            "commits": [
                {"sha": "eee5555", "message": "five"},
                {"sha": "fff6666", "message": "six"},
            ],
        },
    ]
    ordered = sort_repos_by_commit_count(repos)
    assert [repo["repo"] for repo in ordered] == ["me/alpha", "me/mid", "me/zeta"]


def test_git_empty_repos_stay_empty():
    assert sort_repos_by_commit_count([]) == []


# ---------------------------------------------------------------------------
# HTML / dry-run / failures
# ---------------------------------------------------------------------------


def _empty_ok_section(**extra):
    payload = {"ok": True, "error": None, "count": 0}
    payload.update(extra)
    return payload


def test_build_html_email_has_pulse_and_empty_section_copy():
    now = _local(2026, 8, 23, 6, 0)
    start, end = rolling_window(now)
    html_body = build_html_email(
        now,
        start,
        end,
        _empty_ok_section(highlights=[], documents=[]),
        _empty_ok_section(notes=[]),
        _empty_ok_section(issues=[], groups=[]),
        _empty_ok_section(repos=[], commits=[]),
    )
    assert "Sunday wrap-up" in html_body
    assert "0 highlights · 0 documents · 0 Knowledge Hub notes · 0 Linear issues · 0 commits" in html_body
    assert "No highlights this week." in html_body
    assert "No Reader documents this week." in html_body
    assert "No Knowledge Hub notes this week." in html_body
    assert "No completed Linear issues this week." in html_body
    assert "No git commits this week." in html_body


def test_failed_section_is_marked_and_other_sections_render():
    now = _local(2026, 8, 23, 6, 0)
    start, end = rolling_window(now)
    html_body = build_html_email(
        now,
        start,
        end,
        {"ok": False, "error": "Readwise down", "count": 0},
        _empty_ok_section(notes=[{"title": "Saved Essay", "kind": "article"}]),
        _empty_ok_section(issues=[], groups=[]),
        _empty_ok_section(repos=[], commits=[]),
    )
    assert "Readwise failed" in html_body
    assert "This section failed: Readwise down" in html_body
    assert "Saved Essay" in html_body
    assert "Article" in html_body


def test_collect_section_logs_error_and_marks_failed():
    def boom():
        raise RuntimeError("source exploded")

    result = collect_section("readwise", boom)
    assert result["ok"] is False
    assert result["error"] == "source exploded"
    assert result["count"] == 0


def test_html_includes_readwise_highlight_vs_document_groups():
    now = _local(2026, 8, 23, 6, 0)
    start, end = rolling_window(now)
    html_body = build_html_email(
        now,
        start,
        end,
        {
            "ok": True,
            "highlights": [
                {
                    "id": 9,
                    "title": "Deep Work",
                    "author": "Cal Newport",
                    "text": "Focus is a skill.",
                }
            ],
            "documents": [
                {
                    "id": "doc-1",
                    "title": "Reader piece",
                    "url": "https://read.readwise.io/read/doc-1",
                }
            ],
            "count": 2,
        },
        _empty_ok_section(notes=[]),
        _empty_ok_section(issues=[], groups=[]),
        _empty_ok_section(repos=[], commits=[]),
    )
    assert "Highlights (1)" in html_body
    assert "Documents (1)" in html_body
    assert "Deep Work" in html_body
    assert "Cal Newport" in html_body
    assert "Focus is a skill." in html_body
    assert "https://readwise.io/open/9" in html_body
    assert "Reader piece" in html_body
    assert "https://read.readwise.io/read/doc-1" in html_body


def test_html_includes_git_repos_in_commit_count_order():
    now = _local(2026, 8, 23, 6, 0)
    start, end = rolling_window(now)
    repos = sort_repos_by_commit_count(
        [
            {"repo": "me/small", "commits": [{"sha": "aaa1111", "message": "tiny"}]},
            {
                "repo": "me/big",
                "commits": [
                    {"sha": "bbb2222", "message": "first"},
                    {"sha": "ccc3333", "message": "second"},
                ],
            },
        ]
    )
    html_body = build_html_email(
        now,
        start,
        end,
        _empty_ok_section(highlights=[], documents=[]),
        _empty_ok_section(notes=[]),
        _empty_ok_section(issues=[], groups=[]),
        {"ok": True, "repos": repos, "count": 3, "events_capped": True, "events_cap": 300},
    )
    assert html_body.index("me/big") < html_body.index("me/small")
    assert "first" in html_body
    assert "bbb2222" in html_body
    assert "GitHub Events API cap reached (300 events)" in html_body


def test_run_sunday_wrap_up_email_dry_run_does_not_send():
    now = _local(2026, 8, 23, 6, 0)
    empty = {
        "highlights": [],
        "documents": [],
        "notes": [],
        "issues": [],
        "groups": [],
        "repos": [],
        "commits": [],
        "count": 0,
    }
    with patch(
        "scripts.send_sunday_wrap_up_email.collect_readwise_section",
        return_value={**empty},
    ), patch(
        "scripts.send_sunday_wrap_up_email.collect_knowledge_hub_notes",
        return_value={**empty},
    ), patch(
        "scripts.send_sunday_wrap_up_email.collect_linear_section",
        return_value={**empty},
    ), patch(
        "scripts.send_sunday_wrap_up_email.collect_git_section",
        return_value={**empty},
    ), patch(
        "scripts.send_sunday_wrap_up_email.send_html_email"
    ) as mock_send:
        success = run_sunday_wrap_up_email(dry_run=True, now=now)

    assert success is True
    mock_send.assert_not_called()


def test_run_sunday_wrap_up_email_sends_even_when_sections_empty():
    now = _local(2026, 8, 23, 6, 0)
    empty = {
        "highlights": [],
        "documents": [],
        "notes": [],
        "issues": [],
        "groups": [],
        "repos": [],
        "commits": [],
        "count": 0,
    }
    with patch(
        "scripts.send_sunday_wrap_up_email.collect_readwise_section",
        return_value={**empty},
    ), patch(
        "scripts.send_sunday_wrap_up_email.collect_knowledge_hub_notes",
        return_value={**empty},
    ), patch(
        "scripts.send_sunday_wrap_up_email.collect_linear_section",
        return_value={**empty},
    ), patch(
        "scripts.send_sunday_wrap_up_email.collect_git_section",
        return_value={**empty},
    ), patch(
        "scripts.send_sunday_wrap_up_email.send_html_email",
        return_value=True,
    ) as mock_send:
        success = run_sunday_wrap_up_email(dry_run=False, now=now)

    assert success is True
    mock_send.assert_called_once()
    subject, html_body = mock_send.call_args.args
    assert subject == "Sunday wrap-up — Aug 23"
    assert "Sunday wrap-up" in html_body
