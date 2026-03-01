"""Tests for daily Linear issues digest email generation."""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.send_linear_digest_email import (
    build_digest_sections,
    build_html_email,
    run_linear_digest_email,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_build_digest_sections_filters_human_updates_and_creator_windows():
    now = datetime(2026, 3, 2, 3, 0, tzinfo=timezone.utc)  # Mar 1, 7pm PT
    viewer_id = "viewer-1"

    raw_issues = [
        # Created by viewer in last 24h -> section 24h
        {
            "id": "i-1",
            "identifier": "GD-101",
            "title": "Created recently by me",
            "description": "Issue one",
            "url": "https://linear.app/chapters/issue/gd-101/created-recently",
            "createdAt": _iso(now - timedelta(hours=12)),
            "updatedAt": _iso(now - timedelta(hours=11)),
            "creator": {"id": viewer_id},
            "team": {"name": "Growth"},
            "project": {"name": "Digest"},
            "state": {"name": "Todo"},
            "history": {"nodes": []},
        },
        # Touched by viewer 3 days ago -> section 7d excl today
        {
            "id": "i-2",
            "identifier": "GD-102",
            "title": "Touched three days ago",
            "description": "Issue two",
            "url": "https://linear.app/chapters/issue/gd-102/touched-three-days-ago",
            "createdAt": _iso(now - timedelta(days=30)),
            "updatedAt": _iso(now - timedelta(days=1)),
            "creator": {"id": "someone-else"},
            "team": {"name": "Growth"},
            "project": {"name": "Digest"},
            "state": {"name": "In Progress"},
            "history": {
                "nodes": [
                    {
                        "id": "h-1",
                        "createdAt": _iso(now - timedelta(days=3)),
                        "updatedAt": _iso(now - timedelta(days=3)),
                        "actorId": viewer_id,
                        "actor": {"id": viewer_id},
                        "botActor": None,
                    }
                ]
            },
        },
        # Bot update should not count
        {
            "id": "i-3",
            "identifier": "GD-103",
            "title": "Only bot touched",
            "description": "Issue three",
            "url": "https://linear.app/chapters/issue/gd-103/only-bot",
            "createdAt": _iso(now - timedelta(days=20)),
            "updatedAt": _iso(now - timedelta(hours=6)),
            "creator": {"id": "someone-else"},
            "team": {"name": "Growth"},
            "project": {"name": "Digest"},
            "state": {"name": "Done"},
            "history": {
                "nodes": [
                    {
                        "id": "h-2",
                        "createdAt": _iso(now - timedelta(hours=6)),
                        "updatedAt": _iso(now - timedelta(hours=6)),
                        "actorId": None,
                        "actor": None,
                        "botActor": {"id": "bot-1", "type": "integration"},
                    }
                ]
            },
        },
        # Created in 7d window and touched in last 24h -> both sections
        {
            "id": "i-4",
            "identifier": "GD-104",
            "title": "Created earlier then touched now",
            "description": "Issue four",
            "url": "https://linear.app/chapters/issue/gd-104/created-and-touched",
            "createdAt": _iso(now - timedelta(days=4)),
            "updatedAt": _iso(now - timedelta(hours=2)),
            "creator": {"id": viewer_id},
            "team": {"name": "Product"},
            "project": {"name": "Roadmap"},
            "state": {"name": "In Progress"},
            "history": {
                "nodes": [
                    {
                        "id": "h-3",
                        "createdAt": _iso(now - timedelta(hours=2)),
                        "updatedAt": _iso(now - timedelta(hours=2)),
                        "actorId": viewer_id,
                        "actor": {"id": viewer_id},
                        "botActor": None,
                    }
                ]
            },
        },
    ]

    issues_24h, issues_7d = build_digest_sections(raw_issues, viewer_id, now)

    ids_24h = [i["id"] for i in issues_24h]
    ids_7d = [i["id"] for i in issues_7d]

    assert "i-1" in ids_24h
    assert "i-4" in ids_24h
    assert "i-2" not in ids_24h
    assert "i-3" not in ids_24h

    assert "i-2" in ids_7d
    assert "i-4" in ids_7d
    assert "i-1" not in ids_7d
    assert "i-3" not in ids_7d

    issue_1 = next(i for i in issues_24h if i["id"] == "i-1")
    issue_4 = next(i for i in issues_24h if i["id"] == "i-4")
    assert issue_1["is_new_today"] is True
    assert issue_4["is_new_today"] is False


def test_build_html_email_contains_sections_grouping_and_details():
    now = datetime(2026, 3, 2, 3, 0, tzinfo=timezone.utc)
    sample_issue = {
        "id": "i-1",
        "identifier": "GD-200",
        "title": "Digest formatting",
        "status_name": "In Progress",
        "url": "https://linear.app/chapters/issue/gd-200/digest-formatting",
        "description": "A fairly long description that should be shown in a details toggle.",
        "team_name": "Growth",
        "project_name": "Digest",
        "created_at": now - timedelta(hours=5),
        "is_new_today": True,
        "activity_at": now - timedelta(hours=2),
    }

    html_content = build_html_email(now, [sample_issue], [])

    assert "Touched in the Past 24 Hours" in html_content
    assert "Touched in the Past 7 Days (excluding today)" in html_content
    assert "Team: Growth" in html_content
    assert "Project: Digest" in html_content
    assert "<details class='issue-description'>" in html_content
    assert "New" in html_content


def test_run_linear_digest_email_skips_when_disabled():
    now = datetime(2026, 3, 2, 3, 0, tzinfo=timezone.utc)

    with patch.dict(os.environ, {"LINEAR_DIGEST_ENABLED": "false"}, clear=False):
        with patch("scripts.send_linear_digest_email._fetch_viewer") as mock_fetch_viewer:
            success = run_linear_digest_email(dry_run=False, now_utc=now)

    assert success is True
    mock_fetch_viewer.assert_not_called()


def test_run_linear_digest_email_no_issues_does_not_send():
    now = datetime(2026, 3, 2, 3, 0, tzinfo=timezone.utc)

    with patch.dict(
        os.environ,
        {
            "LINEAR_DIGEST_ENABLED": "true",
            "LINEAR_API_KEY": "test-linear-key",
        },
        clear=False,
    ):
        with patch(
            "scripts.send_linear_digest_email._fetch_viewer",
            return_value={"id": "viewer-1", "name": "Test User"},
        ), patch(
            "scripts.send_linear_digest_email._fetch_recent_workspace_issues",
            return_value=[],
        ), patch(
            "scripts.send_linear_digest_email.send_html_email"
        ) as mock_send:
            success = run_linear_digest_email(dry_run=False, now_utc=now)

    assert success is True
    mock_send.assert_not_called()
