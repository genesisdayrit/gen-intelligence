"""Tests for GitHub activity in the main-thread rollup.

Covers the pure pieces: reducing raw GitHub events to per-repo summaries
(window scoping, dedup, noise filtering) and rendering the LLM context block.
No network calls.
"""

import os
import sys
from datetime import datetime, timezone

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SYSTEM_TIMEZONE", "US/Eastern")

from services.github.activity import summarize_events, collect_recent_github_activity
from scripts.send_main_thread_rollup import build_github_block

THRESHOLD = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
IN_WINDOW = "2026-07-08T14:30:00Z"
OUT_OF_WINDOW = "2026-07-08T09:00:00Z"


def _push_event(repo="me/repo-a", created=IN_WINDOW, ref="refs/heads/main", commits=None):
    return {
        "type": "PushEvent",
        "created_at": created,
        "repo": {"name": repo},
        "payload": {"ref": ref, "commits": commits or []},
    }


def _commit(sha, message, distinct=True):
    return {"sha": sha, "message": message, "distinct": distinct}


# --- Window scoping ---

def test_events_before_threshold_are_dropped():
    events = [
        _push_event(created=OUT_OF_WINDOW, commits=[_commit("a" * 40, "old work")]),
        _push_event(created=IN_WINDOW, commits=[_commit("b" * 40, "new work")]),
    ]
    repos = summarize_events(events, THRESHOLD)
    assert len(repos) == 1
    messages = [c["message"] for c in repos[0]["commits"]]
    assert messages == ["new work"]


def test_event_exactly_at_threshold_is_kept():
    events = [_push_event(created="2026-07-08T12:00:00Z", commits=[_commit("c" * 40, "edge")])]
    repos = summarize_events(events, THRESHOLD)
    assert repos and repos[0]["commits"][0]["message"] == "edge"


# --- Push events ---

def test_push_commits_parsed_and_shortened():
    events = [
        _push_event(
            ref="refs/heads/feature/x",
            commits=[_commit("abcdef1234567890" + "0" * 24, "Add thing\n\nlong body")],
        )
    ]
    repos = summarize_events(events, THRESHOLD)
    c = repos[0]["commits"][0]
    assert c["sha"] == "abcdef1"
    assert c["branch"] == "feature/x"
    assert c["message"] == "Add thing"


def test_merge_and_non_distinct_commits_skipped():
    events = [
        _push_event(
            commits=[
                _commit("1" * 40, "Merge pull request #5 from me/branch"),
                _commit("2" * 40, "Merge branch 'main' into feature"),
                _commit("3" * 40, "rebased commit", distinct=False),
                _commit("4" * 40, "real work"),
            ]
        )
    ]
    repos = summarize_events(events, THRESHOLD)
    assert [c["message"] for c in repos[0]["commits"]] == ["real work"]


def test_same_sha_across_pushes_deduped():
    sha = "5" * 40
    events = [
        _push_event(ref="refs/heads/main", commits=[_commit(sha, "shared commit")]),
        _push_event(ref="refs/heads/release", commits=[_commit(sha, "shared commit")]),
    ]
    repos = summarize_events(events, THRESHOLD)
    assert len(repos[0]["commits"]) == 1


# --- PRs, issues, comments, other ---

def test_pr_closed_with_merged_flag_becomes_merged():
    events = [
        {
            "type": "PullRequestEvent",
            "created_at": IN_WINDOW,
            "repo": {"name": "me/repo-a"},
            "payload": {
                "action": "closed",
                "pull_request": {"number": 7, "title": "Ship it", "merged": True},
            },
        }
    ]
    repos = summarize_events(events, THRESHOLD)
    assert repos[0]["prs"] == [{"action": "merged", "number": 7, "title": "Ship it"}]


def test_pr_noise_actions_ignored():
    events = [
        {
            "type": "PullRequestEvent",
            "created_at": IN_WINDOW,
            "repo": {"name": "me/repo-a"},
            "payload": {"action": "labeled", "pull_request": {"number": 8, "title": "x"}},
        }
    ]
    assert summarize_events(events, THRESHOLD) == []


def test_issue_and_comment_events():
    events = [
        {
            "type": "IssuesEvent",
            "created_at": IN_WINDOW,
            "repo": {"name": "me/repo-a"},
            "payload": {"action": "closed", "issue": {"number": 3, "title": "Bug"}},
        },
        {
            "type": "IssueCommentEvent",
            "created_at": IN_WINDOW,
            "repo": {"name": "me/repo-a"},
            "payload": {
                "action": "created",
                "issue": {"number": 3, "title": "Bug"},
                "comment": {"body": "x" * 200},
            },
        },
    ]
    repos = summarize_events(events, THRESHOLD)
    assert repos[0]["issues"] == [{"action": "closed", "number": 3, "title": "Bug"}]
    comment = repos[0]["comments"][0]
    assert comment["number"] == 3
    assert len(comment["body"]) == 140 and comment["body"].endswith("...")


def test_create_release_and_ignored_types():
    events = [
        {
            "type": "CreateEvent",
            "created_at": IN_WINDOW,
            "repo": {"name": "me/repo-b"},
            "payload": {"ref_type": "branch", "ref": "feature/y"},
        },
        {
            "type": "ReleaseEvent",
            "created_at": IN_WINDOW,
            "repo": {"name": "me/repo-b"},
            "payload": {"action": "published", "release": {"name": "v1.2.0"}},
        },
        {
            "type": "WatchEvent",
            "created_at": IN_WINDOW,
            "repo": {"name": "me/repo-b"},
            "payload": {"action": "started"},
        },
    ]
    repos = summarize_events(events, THRESHOLD)
    assert repos[0]["other"] == ["created branch 'feature/y'", "published release 'v1.2.0'"]
    assert not repos[0]["commits"] and not repos[0]["prs"]


def test_repos_sorted_and_empty_omitted():
    events = [
        _push_event(repo="me/zeta", commits=[_commit("6" * 40, "z work")]),
        _push_event(repo="me/Alpha", commits=[_commit("7" * 40, "a work")]),
        # a push with only non-distinct commits should not produce a repo entry
        _push_event(repo="me/empty", commits=[_commit("8" * 40, "noise", distinct=False)]),
    ]
    repos = summarize_events(events, THRESHOLD)
    assert [r["repo"] for r in repos] == ["me/Alpha", "me/zeta"]


# --- Configuration gate ---

def test_collect_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("GITHUB_USERNAME", raising=False)
    monkeypatch.delenv("GITHUB_ACCESS_TOKEN", raising=False)
    assert collect_recent_github_activity(THRESHOLD) is None


def test_collect_returns_empty_on_api_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_USERNAME", "me")
    monkeypatch.setenv("GITHUB_ACCESS_TOKEN", "token")

    def boom(*args, **kwargs):
        raise RuntimeError("GitHub down")

    monkeypatch.setattr("services.github.activity.fetch_user_events", boom)
    assert collect_recent_github_activity(THRESHOLD) == []


# --- LLM context block rendering ---

def test_build_github_block():
    repos = [
        {
            "repo": "me/repo-a",
            "commits": [{"sha": "abcdef1", "branch": "main", "message": "Add thing"}],
            "prs": [{"action": "merged", "number": 7, "title": "Ship it"}],
            "issues": [{"action": "closed", "number": 3, "title": "Bug"}],
            "comments": [{"number": 3, "title": "Bug", "body": "fixed by #7"}],
            "other": ["created branch 'feature/y'"],
        }
    ]
    block = build_github_block(repos)
    assert "=== REPO: me/repo-a ===" in block
    assert "- commit abcdef1 on main: Add thing" in block
    assert "- PR #7 merged: Ship it" in block
    assert "- issue #3 closed: Bug" in block
    assert "- commented on #3 'Bug': fixed by #7" in block
    assert "- created branch 'feature/y'" in block


# --- Run tests ---

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
