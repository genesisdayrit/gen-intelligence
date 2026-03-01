#!/usr/bin/env python3
"""Send Daily Linear Issues Digest email.

The digest includes two rolling windows from send time:
1) Touched in the past 24 hours
2) Touched in the past 7 days (excluding today / last 24 hours)

An issue is included when the authenticated Linear user is either:
- The creator (in the window), or
- A human updater (in the window), based on Issue.history where:
  - actorId matches the viewer id, and
  - botActor is null (filters out integrations/automations)

Usage:
    python -m scripts.send_linear_digest_email
    python -m scripts.send_linear_digest_email --dry-run
    python -m scripts.send_linear_digest_email --output linear_digest.html
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# Add app directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import SYSTEM_TZ
from services.email.gmail_client import send_html_email

logger = logging.getLogger(__name__)

LINEAR_API_URL = "https://api.linear.app/graphql"
ISSUES_PAGE_SIZE = 50
ISSUE_HISTORY_PAGE_SIZE = 50
# Deliberately hardcoded so enable/disable changes are tracked in version control.
LINEAR_DIGEST_ENABLED = True

VIEWER_QUERY = """
query Viewer {
  viewer {
    id
    name
    email
  }
}
"""

RECENT_ISSUES_QUERY = """
query RecentIssuesForDigest(
  $first: Int!
  $after: String
  $updatedAtGte: DateTimeOrDuration!
  $historyFirst: Int!
) {
  issues(
    first: $first
    after: $after
    orderBy: updatedAt
    filter: { updatedAt: { gte: $updatedAtGte } }
  ) {
    nodes {
      id
      identifier
      title
      description
      url
      createdAt
      updatedAt
      creator {
        id
        name
        email
      }
      team {
        id
        name
        key
      }
      project {
        id
        name
      }
      state {
        id
        name
      }
      history(first: $historyFirst, orderBy: updatedAt) {
        nodes {
          id
          createdAt
          updatedAt
          actorId
          actor {
            id
            name
            email
          }
          botActor {
            id
            name
            type
          }
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def _parse_linear_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_linear_iso(dt: datetime) -> str:
    utc_value = dt.astimezone(timezone.utc).replace(microsecond=0)
    return utc_value.isoformat().replace("+00:00", "Z")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def _execute_linear_query(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    api_key = os.getenv("LINEAR_API_KEY")
    if not api_key:
        raise ValueError("LINEAR_API_KEY environment variable not set")

    headers = {
        "Content-Type": "application/json",
        "Authorization": api_key,
    }

    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    try:
        response = requests.post(
            LINEAR_API_URL,
            headers=headers,
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Linear API request failed: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(f"Linear API returned HTTP {response.status_code}: {response.text}")

    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"Linear GraphQL errors: {body['errors']}")

    data = body.get("data")
    if data is None:
        raise RuntimeError("Linear API response missing 'data'")

    return data


def _fetch_viewer() -> dict[str, Any]:
    data = _execute_linear_query(VIEWER_QUERY)
    viewer = data.get("viewer") or {}
    if not viewer.get("id"):
        raise RuntimeError("Unable to resolve Linear viewer from API key")
    return viewer


def _fetch_recent_workspace_issues(seven_day_start_utc: datetime) -> list[dict[str, Any]]:
    all_issues: list[dict[str, Any]] = []
    after: str | None = None

    while True:
        variables = {
            "first": ISSUES_PAGE_SIZE,
            "after": after,
            "updatedAtGte": _to_linear_iso(seven_day_start_utc),
            "historyFirst": ISSUE_HISTORY_PAGE_SIZE,
        }
        data = _execute_linear_query(RECENT_ISSUES_QUERY, variables)
        issues_conn = data.get("issues") or {}

        nodes = issues_conn.get("nodes") or []
        all_issues.extend(nodes)

        page_info = issues_conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break

    return all_issues


def _extract_user_touch_times(
    issue: dict[str, Any],
    viewer_id: str,
    seven_day_start_utc: datetime,
) -> list[datetime]:
    history_nodes = ((issue.get("history") or {}).get("nodes") or [])
    touch_times: list[datetime] = []

    for event in history_nodes:
        actor_id = event.get("actorId") or ((event.get("actor") or {}).get("id"))
        if actor_id != viewer_id:
            continue

        # In Linear's GraphQL schema, non-human actors are represented via botActor.
        if event.get("botActor"):
            continue

        ts = _parse_linear_datetime(event.get("updatedAt") or event.get("createdAt"))
        if not ts:
            continue
        if ts < seven_day_start_utc:
            continue
        touch_times.append(ts)

    return touch_times


def build_digest_sections(
    raw_issues: list[dict[str, Any]],
    viewer_id: str,
    now_utc: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build section issue lists for 24h and 7d (excluding today) windows."""
    window_24h_start = now_utc - timedelta(hours=24)
    window_7d_start = now_utc - timedelta(days=7)
    today_local = now_utc.astimezone(SYSTEM_TZ).date()

    section_24_by_id: dict[str, dict[str, Any]] = {}
    section_7_by_id: dict[str, dict[str, Any]] = {}

    for issue in raw_issues:
        issue_id = issue.get("id")
        if not issue_id:
            continue

        created_at = _parse_linear_datetime(issue.get("createdAt"))
        if not created_at:
            continue

        creator_id = ((issue.get("creator") or {}).get("id"))
        created_by_viewer = creator_id == viewer_id
        created_in_24h = created_by_viewer and created_at >= window_24h_start
        created_in_7d_excl_today = (
            created_by_viewer
            and window_7d_start <= created_at < window_24h_start
        )

        user_touch_times = _extract_user_touch_times(issue, viewer_id, window_7d_start)
        touch_24h = [ts for ts in user_touch_times if ts >= window_24h_start]
        touch_7d_excl_today = [
            ts for ts in user_touch_times if window_7d_start <= ts < window_24h_start
        ]
        latest_touch_24h = max(touch_24h) if touch_24h else None
        latest_touch_7d = max(touch_7d_excl_today) if touch_7d_excl_today else None

        team = issue.get("team") or {}
        project = issue.get("project") or {}
        state = issue.get("state") or {}
        description = (issue.get("description") or "").strip()

        base_issue = {
            "id": issue_id,
            "identifier": issue.get("identifier") or issue_id,
            "title": issue.get("title") or "(Untitled issue)",
            "status_name": state.get("name") or "Unknown",
            "url": issue.get("url") or "",
            "description": description,
            "team_name": team.get("name") or team.get("key") or "No Team",
            "project_name": project.get("name") or "No Project",
            "created_at": created_at,
            "is_new_today": created_at.astimezone(SYSTEM_TZ).date() == today_local,
        }

        if created_in_24h or latest_touch_24h:
            activity_candidates = [
                ts for ts in [created_at if created_in_24h else None, latest_touch_24h] if ts
            ]
            if activity_candidates:
                activity_at = max(activity_candidates)
                existing = section_24_by_id.get(issue_id)
                if not existing or activity_at > existing["activity_at"]:
                    section_24_by_id[issue_id] = {**base_issue, "activity_at": activity_at}

        if created_in_7d_excl_today or latest_touch_7d:
            activity_candidates = [
                ts
                for ts in [
                    created_at if created_in_7d_excl_today else None,
                    latest_touch_7d,
                ]
                if ts
            ]
            if activity_candidates:
                activity_at = max(activity_candidates)
                existing = section_7_by_id.get(issue_id)
                if not existing or activity_at > existing["activity_at"]:
                    section_7_by_id[issue_id] = {**base_issue, "activity_at": activity_at}

    section_24 = sorted(
        section_24_by_id.values(),
        key=lambda i: i["activity_at"],
        reverse=True,
    )
    section_7 = sorted(
        section_7_by_id.values(),
        key=lambda i: i["activity_at"],
        reverse=True,
    )
    return section_24, section_7


def _group_by_team_then_project(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    for issue in issues:
        team_name = issue["team_name"]
        project_name = issue["project_name"]
        activity_at = issue["activity_at"]

        team_group = grouped.setdefault(
            team_name,
            {
                "team_name": team_name,
                "latest_activity_at": activity_at,
                "projects": {},
            },
        )
        if activity_at > team_group["latest_activity_at"]:
            team_group["latest_activity_at"] = activity_at

        project_group = team_group["projects"].setdefault(
            project_name,
            {
                "project_name": project_name,
                "latest_activity_at": activity_at,
                "issues": [],
            },
        )
        if activity_at > project_group["latest_activity_at"]:
            project_group["latest_activity_at"] = activity_at

        project_group["issues"].append(issue)

    team_groups: list[dict[str, Any]] = []
    for team_group in grouped.values():
        project_groups = list(team_group["projects"].values())
        for project in project_groups:
            project["issues"] = sorted(
                project["issues"],
                key=lambda i: i["activity_at"],
                reverse=True,
            )

        project_groups = sorted(
            project_groups,
            key=lambda p: (
                -p["latest_activity_at"].timestamp(),
                p["project_name"].lower(),
            ),
        )

        team_groups.append({
            "team_name": team_group["team_name"],
            "latest_activity_at": team_group["latest_activity_at"],
            "projects": project_groups,
        })

    team_groups = sorted(
        team_groups,
        key=lambda t: (
            -t["latest_activity_at"].timestamp(),
            t["team_name"].lower(),
        ),
    )
    return team_groups


def _render_description_details(description: str) -> str:
    if not description:
        return (
            "<details class='issue-description'>"
            "<summary>Description</summary>"
            "<div class='description-body'><em>No description provided.</em></div>"
            "</details>"
        )

    single_line_preview = " ".join(description.split())
    summary_text = html.escape(_truncate(single_line_preview, 140))
    body_text = html.escape(_truncate(description, 900)).replace("\n", "<br>")
    return (
        "<details class='issue-description'>"
        f"<summary>{summary_text}</summary>"
        f"<div class='description-body'>{body_text}</div>"
        "</details>"
    )


def _render_issue_item(issue: dict[str, Any]) -> str:
    identifier = html.escape(issue["identifier"])
    title = html.escape(issue["title"])
    status = html.escape(issue["status_name"])
    url = html.escape(issue["url"])
    badge = " <span class='badge badge-new'>New</span>" if issue["is_new_today"] else ""

    if url:
        issue_line = f"<a href='{url}'><strong>{identifier}</strong></a> - {title}{badge}"
    else:
        issue_line = f"<strong>{identifier}</strong> - {title}{badge}"

    return (
        "<li class='issue-item'>"
        f"<div class='issue-title'>{issue_line}</div>"
        f"<div class='issue-meta'>Status: <span class='badge badge-status'>{status}</span></div>"
        f"{_render_description_details(issue['description'])}"
        "</li>"
    )


def _render_section(title: str, issues: list[dict[str, Any]]) -> str:
    groups = _group_by_team_then_project(issues)
    parts = [f"<h2>{html.escape(title)} ({len(issues)})</h2>"]

    for team in groups:
        parts.append(f"<h3>Team: {html.escape(team['team_name'])}</h3>")
        for project in team["projects"]:
            parts.append(f"<h4>Project: {html.escape(project['project_name'])}</h4>")
            parts.append("<ul class='issues-list'>")
            for issue in project["issues"]:
                parts.append(_render_issue_item(issue))
            parts.append("</ul>")

    return "\n".join(parts)


def build_html_email(
    now_utc: datetime,
    issues_24h: list[dict[str, Any]],
    issues_7d_excl_today: list[dict[str, Any]],
) -> str:
    now_local = now_utc.astimezone(SYSTEM_TZ)

    html_parts = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        "<meta charset='utf-8'>",
        "<style>",
        "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1f2937; line-height: 1.55; max-width: 960px; margin: 0 auto; padding: 20px; }",
        "h1 { margin-bottom: 0.25rem; }",
        "h2 { margin-top: 2rem; border-bottom: 1px solid #e5e7eb; padding-bottom: 0.45rem; }",
        "h3 { margin-top: 1.4rem; margin-bottom: 0.5rem; color: #111827; }",
        "h4 { margin-top: 0.6rem; margin-bottom: 0.5rem; color: #374151; }",
        ".meta { color: #6b7280; margin-top: 0; }",
        ".issues-list { margin-top: 0.4rem; margin-bottom: 1rem; padding-left: 1.2rem; }",
        ".issue-item { margin-bottom: 0.9rem; }",
        ".issue-title { margin-bottom: 0.25rem; }",
        ".issue-meta { color: #4b5563; font-size: 0.93rem; margin-bottom: 0.2rem; }",
        ".badge { display: inline-block; padding: 0.12rem 0.4rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; vertical-align: middle; }",
        ".badge-status { background: #eef2ff; color: #3730a3; }",
        ".badge-new { background: #dcfce7; color: #166534; margin-left: 0.35rem; }",
        "details { margin-top: 0.25rem; }",
        "summary { cursor: pointer; color: #374151; }",
        ".description-body { margin-top: 0.3rem; color: #4b5563; white-space: normal; }",
        "</style>",
        "</head>",
        "<body>",
        "<h1>Daily Linear Issues Digest</h1>",
        f"<p class='meta'>Generated: {html.escape(now_local.strftime('%b %d, %Y %I:%M %p %Z'))}</p>",
    ]

    html_parts.append(_render_section("Touched in the Past 24 Hours", issues_24h))
    html_parts.append(
        _render_section(
            "Touched in the Past 7 Days (excluding today)",
            issues_7d_excl_today,
        )
    )

    html_parts.extend(["</body>", "</html>"])
    return "\n".join(html_parts)


def run_linear_digest_email(
    dry_run: bool = False,
    output: str | None = None,
    now_utc: datetime | None = None,
) -> bool:
    """Generate and send the daily Linear digest email."""
    load_dotenv()

    if not LINEAR_DIGEST_ENABLED:
        logger.info("LINEAR_DIGEST_ENABLED is hardcoded to false; skipping daily digest.")
        return True

    if not os.getenv("LINEAR_API_KEY"):
        logger.error("LINEAR_API_KEY environment variable not set")
        return False

    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    seven_day_start = now - timedelta(days=7)

    try:
        viewer = _fetch_viewer()
        viewer_id = viewer["id"]
        logger.info(
            "Building Linear digest for viewer=%s (%s)",
            viewer.get("name") or viewer.get("email") or viewer_id,
            viewer_id,
        )

        raw_issues = _fetch_recent_workspace_issues(seven_day_start)
        logger.info("Fetched %d recently updated issues", len(raw_issues))

        issues_24h, issues_7d_excl_today = build_digest_sections(raw_issues, viewer_id, now)
        total_issues = len(issues_24h) + len(issues_7d_excl_today)
        logger.info(
            "Digest results | last24h=%d | sevenDaysExclToday=%d",
            len(issues_24h),
            len(issues_7d_excl_today),
        )

        if total_issues == 0:
            logger.info("No digest issues found; skipping email send.")
            return True

        html_body = build_html_email(now, issues_24h, issues_7d_excl_today)

        if output:
            output_path = Path(output)
            output_path.write_text(html_body, encoding="utf-8")
            logger.info("Saved digest HTML to %s", output_path)

        if dry_run:
            logger.info("Dry run completed; digest email not sent.")
            return True

        now_local = now.astimezone(SYSTEM_TZ)
        subject = f"Linear Daily Issues Digest ({now_local.strftime('%b %d, %Y')})"
        sent = send_html_email(subject, html_body)
        if sent:
            logger.info("Digest email sent successfully.")
        else:
            logger.error("Failed to send digest email.")
        return sent
    except Exception:
        logger.exception("Linear digest generation failed")
        return False


def main():
    parser = argparse.ArgumentParser(description="Send daily Linear issues digest email")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate digest but do not send the email",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Optional path to write generated HTML output",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    success = run_linear_digest_email(
        dry_run=args.dry_run,
        output=args.output,
    )
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
