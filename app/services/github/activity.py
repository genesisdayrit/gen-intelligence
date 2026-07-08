"""Fetch the user's recent GitHub activity for the main-thread rollup.

Polls the GitHub Events API (GET /users/{username}/events) at rollup time —
the same pull-based pattern the rollup uses for Linear — so no per-repo
webhook configuration is needed. Authenticated as the same user, the endpoint
includes private-repo events, covering pushes to any branch, pull requests,
issues, comments, reviews, branch/tag creates, and releases across every repo
the user touched.

Configuration (both required; the feature silently disables otherwise):
    GITHUB_USERNAME      the GitHub login whose activity to gather
    GITHUB_ACCESS_TOKEN  a personal access token for that same user, so
                         private events are included

Known Events API limits (all fine for a rolling few-hour window):
    - only the most recent 300 events / 90 days are served
    - a PushEvent carries at most 20 commits
    - delivery can lag the action by ~30 seconds
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

GITHUB_EVENTS_URL = "https://api.github.com/users/{username}/events"
PER_PAGE = 100
MAX_PAGES = 3  # the Events API never serves past the most recent 300 events

_MERGE_PREFIXES = ("Merge pull request", "Merge branch", "Merge remote-tracking branch")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _first_line(text: str | None) -> str:
    return (text or "").strip().split("\n")[0]


def _branch(ref: str | None) -> str:
    return (ref or "").removeprefix("refs/heads/")


def fetch_user_events(username: str, token: str, threshold: datetime) -> list[dict]:
    """Fetch the user's events (newest first), paging until a page dips below `threshold`."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    events: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        resp = requests.get(
            GITHUB_EVENTS_URL.format(username=username),
            headers=headers,
            params={"per_page": PER_PAGE, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        events.extend(batch)
        oldest = _parse_dt(batch[-1].get("created_at"))
        if oldest is not None and oldest < threshold:
            break  # pages are newest-first; everything after this is older still
    return events


def summarize_events(events: list[dict], threshold: datetime) -> list[dict]:
    """Reduce raw events at/after `threshold` to per-repo summaries (pure; no network).

    Returns a list sorted by repo name; repos with nothing reportable are omitted:
        {"repo": "owner/name",
         "commits":  [{"sha", "branch", "message"}],
         "prs":      [{"action", "number", "title"}],
         "issues":   [{"action", "number", "title"}],
         "comments": [{"number", "title", "body"}],
         "other":    ["created branch 'x'", ...]}
    """
    repos: dict[str, dict] = {}
    seen_shas: set[str] = set()

    def bucket(name: str) -> dict:
        return repos.setdefault(
            name,
            {"repo": name, "commits": [], "prs": [], "issues": [], "comments": [], "other": []},
        )

    for e in events:
        created = _parse_dt(e.get("created_at"))
        if created is None or created < threshold:
            continue
        repo_name = (e.get("repo") or {}).get("name") or "unknown"
        etype = e.get("type")
        payload = e.get("payload") or {}

        if etype == "PushEvent":
            branch = _branch(payload.get("ref"))
            for c in payload.get("commits") or []:
                if not c.get("distinct"):
                    continue  # re-push of an existing commit (rebase/force-push noise)
                sha = c.get("sha") or ""
                message = _first_line(c.get("message"))
                if sha in seen_shas or message.startswith(_MERGE_PREFIXES):
                    continue
                seen_shas.add(sha)
                bucket(repo_name)["commits"].append(
                    {"sha": sha[:7], "branch": branch, "message": message}
                )
        elif etype == "PullRequestEvent":
            pr = payload.get("pull_request") or {}
            action = payload.get("action")
            if action == "closed":
                action = "merged" if pr.get("merged") else "closed"
            if action not in ("opened", "reopened", "merged", "closed", "ready_for_review"):
                continue  # label/assign/sync churn isn't worth a bullet
            bucket(repo_name)["prs"].append(
                {"action": action, "number": pr.get("number"), "title": _first_line(pr.get("title"))}
            )
        elif etype == "IssuesEvent":
            action = payload.get("action")
            if action not in ("opened", "closed", "reopened"):
                continue
            issue = payload.get("issue") or {}
            bucket(repo_name)["issues"].append(
                {"action": action, "number": issue.get("number"), "title": _first_line(issue.get("title"))}
            )
        elif etype == "IssueCommentEvent":
            if payload.get("action") != "created":
                continue
            issue = payload.get("issue") or {}
            body = _first_line((payload.get("comment") or {}).get("body"))
            if len(body) > 140:
                body = body[:137] + "..."
            bucket(repo_name)["comments"].append(
                {"number": issue.get("number"), "title": _first_line(issue.get("title")), "body": body}
            )
        elif etype == "PullRequestReviewEvent":
            pr = payload.get("pull_request") or {}
            bucket(repo_name)["other"].append(
                f"reviewed PR #{pr.get('number')} '{_first_line(pr.get('title'))}'"
            )
        elif etype == "CreateEvent":
            ref_type = payload.get("ref_type")
            if ref_type == "repository":
                bucket(repo_name)["other"].append("created the repository")
            elif ref_type in ("branch", "tag"):
                bucket(repo_name)["other"].append(f"created {ref_type} '{payload.get('ref')}'")
        elif etype == "ReleaseEvent":
            if payload.get("action") == "published":
                release = payload.get("release") or {}
                name = release.get("name") or release.get("tag_name")
                bucket(repo_name)["other"].append(f"published release '{name}'")
        # everything else (Watch, Fork, Delete, Member, ...) is noise for the rollup

    out = [
        r
        for r in repos.values()
        if r["commits"] or r["prs"] or r["issues"] or r["comments"] or r["other"]
    ]
    return sorted(out, key=lambda r: r["repo"].lower())


def collect_recent_github_activity(threshold: datetime) -> list[dict] | None:
    """Gather the user's GitHub activity since `threshold`, grouped by repo.

    Returns None when the feature is unconfigured (GITHUB_USERNAME or
    GITHUB_ACCESS_TOKEN missing) and [] when configured but nothing happened —
    or the API call failed. Failures are logged, never raised: GitHub being
    unreachable must not block the Linear rollup.
    """
    username = os.getenv("GITHUB_USERNAME")
    token = os.getenv("GITHUB_ACCESS_TOKEN")
    if not username or not token:
        return None
    try:
        events = fetch_user_events(username, token, threshold)
    except Exception:
        logger.exception("Failed to fetch GitHub events for %s", username)
        return []
    return summarize_events(events, threshold)
