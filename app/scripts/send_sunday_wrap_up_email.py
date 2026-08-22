#!/usr/bin/env python3
"""Send Sunday wrap-up email (rolling 7-day consumption + completions).

Four sections, no journal-prose summary:
1. Readwise highlights + Reader documents created in the window
2. Knowledge Hub notes whose YAML Journal date falls in the window
3. Linear issues the viewer completed in the window
4. Git commits by GITHUB_USERNAME in the window

Usage:
    python -m scripts.send_sunday_wrap_up_email
    python -m scripts.send_sunday_wrap_up_email --dry-run
    python -m scripts.send_sunday_wrap_up_email --output sunday_wrap_up.html
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import dropbox
import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import SYSTEM_TZ
from scripts.linear.sync_utils import get_dropbox_client
from services.email.gmail_client import send_html_email
from services.github.activity import (
    MAX_PAGES,
    PER_PAGE,
    fetch_user_events,
    summarize_events,
)
from services.obsidian.add_readwise_buffet import HIGHLIGHT_OPEN_URL, READER_DOC_URL
from services.obsidian.add_shared_link import (
    _extract_frontmatter,
    _find_knowledge_hub_path,
)
from services.readwise.export import iter_export_highlights
from services.readwise.reader import iter_reader_documents

logger = logging.getLogger(__name__)

LINEAR_API_URL = "https://api.linear.app/graphql"
ISSUES_PAGE_SIZE = 50
SHORT_QUOTE_LIMIT = 180
GITHUB_EVENTS_CAP = PER_PAGE * MAX_PAGES
KNOWLEDGE_HUB_SUFFIX = "_Knowledge-Hub"
MONTH_ABBREV_TO_NUM = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
JOURNAL_WIKILINK_RE = re.compile(
    r"\[\[\s*([A-Za-z]{3})\.?\s+(\d{1,2}),\s+(\d{4})\s*\]\]"
)
AUTHOR_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]+)")
TWITTER_HOSTS = {"twitter.com", "x.com", "mobile.twitter.com"}

VIEWER_QUERY = """
query Viewer {
  viewer {
    id
    name
    email
  }
}
"""

COMPLETED_ISSUES_QUERY = """
query CompletedIssuesForWrapUp(
  $first: Int!
  $after: String
  $completedAtGte: DateTimeOrDuration!
  $completedAtLte: DateTimeOrDuration!
) {
  issues(
    first: $first
    after: $after
    orderBy: updatedAt
    filter: {
      completedAt: { gte: $completedAtGte, lte: $completedAtLte }
    }
  ) {
    nodes {
      id
      identifier
      title
      url
      completedAt
      state { name type }
      assignee { id name }
      completedBy { id name }
      initiative { id name }
      project {
        id
        name
        initiatives { nodes { id name } }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

COMPLETED_ISSUES_QUERY_WITHOUT_ISSUE_INITIATIVE = """
query CompletedIssuesForWrapUp(
  $first: Int!
  $after: String
  $completedAtGte: DateTimeOrDuration!
  $completedAtLte: DateTimeOrDuration!
) {
  issues(
    first: $first
    after: $after
    orderBy: updatedAt
    filter: {
      completedAt: { gte: $completedAtGte, lte: $completedAtLte }
    }
  ) {
    nodes {
      id
      identifier
      title
      url
      completedAt
      state { name type }
      assignee { id name }
      completedBy { id name }
      project {
        id
        name
        initiatives { nodes { id name } }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

COMPLETED_ISSUES_QUERY_MINIMAL = """
query CompletedIssuesForWrapUp(
  $first: Int!
  $after: String
  $completedAtGte: DateTimeOrDuration!
  $completedAtLte: DateTimeOrDuration!
) {
  issues(
    first: $first
    after: $after
    orderBy: updatedAt
    filter: {
      completedAt: { gte: $completedAtGte, lte: $completedAtLte }
    }
  ) {
    nodes {
      id
      identifier
      title
      url
      completedAt
      state { name type }
      assignee { id name }
      project {
        id
        name
        initiatives { nodes { id name } }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def _ensure_system_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        return SYSTEM_TZ.localize(value)
    return value.astimezone(SYSTEM_TZ)


def rolling_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Rolling 7-day window ending at run time in SYSTEM_TZ."""
    end = _ensure_system_timezone(now) if now else datetime.now(SYSTEM_TZ)
    return end - timedelta(days=7), end


def window_journal_dates(window_start: datetime, window_end: datetime) -> set[date]:
    start_d = _ensure_system_timezone(window_start).date()
    end_d = _ensure_system_timezone(window_end).date()
    days = (end_d - start_d).days
    return {start_d + timedelta(days=offset) for offset in range(days + 1)}


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_linear_iso(dt: datetime) -> str:
    utc_value = dt.astimezone(timezone.utc).replace(microsecond=0)
    return utc_value.isoformat().replace("+00:00", "Z")


def _in_window(value: datetime | None, window_start: datetime, window_end: datetime) -> bool:
    if value is None:
        return False
    start = window_start if window_start.tzinfo else SYSTEM_TZ.localize(window_start)
    end = window_end if window_end.tzinfo else SYSTEM_TZ.localize(window_end)
    ts = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return start <= ts <= end


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def _nonempty(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _twitter_handle_from_url(url: object) -> str | None:
    text = _nonempty(url)
    if not text:
        return None
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in TWITTER_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if parts else None


def highlight_source_label(payload: dict[str, Any]) -> str:
    """Author name, or tweet handle when the highlight is from Twitter/X."""
    author = _nonempty(payload.get("author"))
    category = (_nonempty(payload.get("category")) or "").lower()
    source = (_nonempty(payload.get("source")) or "").lower()
    title = _nonempty(payload.get("title")) or ""
    is_tweet = (
        category == "tweets"
        or source == "twitter"
        or title.lower().startswith("tweets from")
    )
    if is_tweet:
        if author:
            match = AUTHOR_HANDLE_RE.search(author)
            if match:
                return f"@{match.group(1)}"
        handle = _twitter_handle_from_url(payload.get("source_url"))
        if handle:
            return f"@{handle}"
    return author or "Unknown"


def highlight_open_url(payload: dict[str, Any]) -> str:
    highlight_id = payload.get("id")
    if highlight_id is not None and highlight_id != "":
        return HIGHLIGHT_OPEN_URL.format(highlight_id=highlight_id)
    return ""


def document_reader_url(payload: dict[str, Any]) -> str:
    url = _nonempty(payload.get("url"))
    if url and url.startswith(("http://", "https://")):
        return url
    doc_id = payload.get("id")
    if doc_id is not None and doc_id != "":
        return READER_DOC_URL.format(document_id=doc_id)
    return ""


def highlight_event_time(payload: dict[str, Any]) -> datetime | None:
    return _parse_iso_datetime(
        payload.get("highlighted_at") or payload.get("created_at")
    )


def document_created_time(payload: dict[str, Any]) -> datetime | None:
    return _parse_iso_datetime(payload.get("created_at") or payload.get("saved_at"))


def filter_highlights_in_window(
    payloads: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for payload in payloads:
        if _in_window(highlight_event_time(payload), window_start, window_end):
            selected.append(payload)
    return selected


def filter_documents_in_window(
    payloads: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for payload in payloads:
        if _in_window(document_created_time(payload), window_start, window_end):
            selected.append(payload)
    return selected


def split_readwise_items(
    highlights: list[dict[str, Any]],
    documents: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Keep highlights and documents in separate groups for the email."""
    return {"highlights": list(highlights), "documents": list(documents)}


def parse_journal_wikilink(value: object) -> date | None:
    text = _nonempty(value)
    if not text:
        return None
    match = JOURNAL_WIKILINK_RE.search(text)
    if not match:
        return None
    month_abbrev, day_str, year_str = match.groups()
    month = MONTH_ABBREV_TO_NUM.get(month_abbrev.lower())
    if month is None:
        return None
    try:
        return date(int(year_str), month, int(day_str))
    except ValueError:
        return None


def journal_dates_from_frontmatter(frontmatter: dict[str, Any]) -> list[date]:
    raw = frontmatter.get("Journal")
    if raw is None:
        raw = frontmatter.get("journal")
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    dates: list[date] = []
    for value in values:
        parsed = parse_journal_wikilink(value)
        if parsed is not None:
            dates.append(parsed)
    return dates


def classify_kh_kind(frontmatter: dict[str, Any]) -> str:
    tags = frontmatter.get("Tags")
    if tags is None:
        tags = frontmatter.get("tags")
    if tags is None:
        tags = []
    if isinstance(tags, str):
        tags = [tags]
    tag_names = {str(tag).strip().lower() for tag in tags}
    if "youtube" in tag_names:
        return "youtube"
    url = str(frontmatter.get("URL") or frontmatter.get("url") or "").lower()
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    return "article"


def notes_consumed_in_window(
    notes: list[dict[str, Any]],
    window_dates: set[date],
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for note in notes:
        journal_dates = note.get("journal_dates") or journal_dates_from_frontmatter(
            note.get("frontmatter") or {}
        )
        if any(journal_date in window_dates for journal_date in journal_dates):
            matched.append(note)
    return matched


def resolve_linear_initiative_name(issue: dict[str, Any]) -> str:
    initiative = issue.get("initiative") or {}
    if initiative.get("name"):
        return str(initiative["name"])
    project = issue.get("project") or {}
    nodes = ((project.get("initiatives") or {}).get("nodes")) or []
    if nodes and nodes[0].get("name"):
        return str(nodes[0]["name"])
    return "No initiative"


def is_viewer_completion(issue: dict[str, Any], viewer_id: str | None) -> bool:
    state = issue.get("state") or {}
    if (state.get("type") or "").lower() == "canceled":
        return False
    if not viewer_id:
        return True
    completed_by_id = ((issue.get("completedBy") or {}).get("id"))
    if completed_by_id:
        return completed_by_id == viewer_id
    assignee_id = ((issue.get("assignee") or {}).get("id"))
    if assignee_id:
        return assignee_id == viewer_id
    return True


def select_linear_completed(
    raw_issues: list[dict[str, Any]],
    viewer_id: str | None,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for issue in raw_issues:
        if not is_viewer_completion(issue, viewer_id):
            continue
        completed_at = _parse_iso_datetime(issue.get("completedAt"))
        if not _in_window(completed_at, window_start, window_end):
            continue
        project = issue.get("project") or {}
        selected.append(
            {
                "id": issue.get("id"),
                "identifier": issue.get("identifier") or issue.get("id") or "",
                "title": issue.get("title") or "(Untitled issue)",
                "url": issue.get("url") or "",
                "project_name": project.get("name") or "No Project",
                "initiative_name": resolve_linear_initiative_name(issue),
                "completed_at": completed_at,
            }
        )
    return selected


def group_linear_completed(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group completed issues by initiative (when present), then project."""
    grouped: dict[str, dict[str, Any]] = {}
    for issue in issues:
        initiative_name = issue.get("initiative_name") or "No initiative"
        project_name = issue.get("project_name") or "No Project"
        initiative = grouped.setdefault(
            initiative_name,
            {"initiative_name": initiative_name, "projects": {}},
        )
        project = initiative["projects"].setdefault(
            project_name,
            {"project_name": project_name, "issues": []},
        )
        project["issues"].append(issue)

    def _initiative_sort_key(name: str) -> tuple[int, str]:
        return (1 if name == "No initiative" else 0, name.lower())

    result: list[dict[str, Any]] = []
    for initiative_name in sorted(grouped, key=_initiative_sort_key):
        initiative = grouped[initiative_name]
        projects = []
        for project_name in sorted(initiative["projects"], key=str.lower):
            project = initiative["projects"][project_name]
            project["issues"] = sorted(
                project["issues"],
                key=lambda item: (item.get("identifier") or "").lower(),
            )
            projects.append(project)
        result.append(
            {
                "initiative_name": initiative_name,
                "projects": projects,
            }
        )
    return result


def sort_repos_by_commit_count(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order repos by commit count descending; ties break on repo name."""
    return sorted(
        repos,
        key=lambda repo: (-len(repo.get("commits") or []), (repo.get("repo") or "").lower()),
    )


def flatten_repo_commits(repos: list[dict[str, Any]]) -> list[dict[str, str]]:
    commits: list[dict[str, str]] = []
    for repo in repos:
        repo_name = repo.get("repo") or "unknown"
        for commit in repo.get("commits") or []:
            commits.append(
                {
                    "repo": repo_name,
                    "sha": commit.get("sha") or "",
                    "message": commit.get("message") or "",
                }
            )
    return commits


# ---------------------------------------------------------------------------
# Source collectors (network I/O)
# ---------------------------------------------------------------------------


def collect_readwise_section(
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    if not os.getenv("READWISE_TOKEN"):
        raise EnvironmentError("READWISE_TOKEN not set")

    updated_after = _to_linear_iso(window_start)
    highlights = filter_highlights_in_window(
        list(iter_export_highlights(updated_after=updated_after)),
        window_start,
        window_end,
    )
    documents = filter_documents_in_window(
        list(iter_reader_documents(updated_after=updated_after)),
        window_start,
        window_end,
    )
    split = split_readwise_items(highlights, documents)
    return {
        "highlights": split["highlights"],
        "documents": split["documents"],
        "count": len(split["highlights"]) + len(split["documents"]),
    }


def _iter_dropbox_files(dbx: dropbox.Dropbox, folder_path: str) -> list[Any]:
    result = dbx.files_list_folder(folder_path, recursive=True)
    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    return entries


def collect_knowledge_hub_notes(
    window_start: datetime,
    window_end: datetime,
    dbx: dropbox.Dropbox | None = None,
) -> dict[str, Any]:
    vault_path = os.getenv("DROPBOX_OBSIDIAN_VAULT_PATH")
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

    client = dbx or get_dropbox_client()
    kh_path = _find_knowledge_hub_path(client, vault_path)
    window_dates = window_journal_dates(window_start, window_end)
    modified_floor = window_start.astimezone(timezone.utc) - timedelta(days=2)

    raw_notes: list[dict[str, Any]] = []
    for entry in _iter_dropbox_files(client, kh_path):
        if not isinstance(entry, dropbox.files.FileMetadata):
            continue
        if not entry.name.lower().endswith(".md"):
            continue
        modified = entry.client_modified or entry.server_modified
        if modified is not None:
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            if modified < modified_floor:
                continue
        _, response = client.files_download(entry.path_lower)
        content = response.content.decode("utf-8")
        frontmatter, _body = _extract_frontmatter(content)
        raw_notes.append(
            {
                "title": Path(entry.name).stem,
                "kind": classify_kh_kind(frontmatter),
                "journal_dates": journal_dates_from_frontmatter(frontmatter),
                "frontmatter": frontmatter,
            }
        )

    notes = notes_consumed_in_window(raw_notes, window_dates)
    notes = sorted(notes, key=lambda note: (note.get("title") or "").lower())
    return {"notes": notes, "count": len(notes)}


def _execute_linear_query(
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    api_key = os.getenv("LINEAR_API_KEY")
    if not api_key:
        raise ValueError("LINEAR_API_KEY environment variable not set")

    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    try:
        response = requests.post(
            LINEAR_API_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": api_key,
            },
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


def _linear_errors_mention(exc: Exception, *needles: str) -> bool:
    text = str(exc).lower()
    return any(needle in text for needle in needles)


def fetch_linear_viewer() -> dict[str, Any]:
    data = _execute_linear_query(VIEWER_QUERY)
    viewer = data.get("viewer") or {}
    if not viewer.get("id"):
        raise RuntimeError("Unable to resolve Linear viewer from API key")
    return viewer


def fetch_completed_linear_issues(
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    variables = {
        "first": ISSUES_PAGE_SIZE,
        "after": None,
        "completedAtGte": _to_linear_iso(window_start),
        "completedAtLte": _to_linear_iso(window_end),
    }
    queries = (
        COMPLETED_ISSUES_QUERY,
        COMPLETED_ISSUES_QUERY_WITHOUT_ISSUE_INITIATIVE,
        COMPLETED_ISSUES_QUERY_MINIMAL,
    )
    last_error: Exception | None = None
    for query in queries:
        try:
            return _page_completed_issues(query, variables)
        except RuntimeError as exc:
            last_error = exc
            if query is COMPLETED_ISSUES_QUERY and _linear_errors_mention(
                exc, "initiative", "completedby"
            ):
                logger.warning("Retrying Linear completed-issues query without issue.initiative")
                continue
            if query is COMPLETED_ISSUES_QUERY_WITHOUT_ISSUE_INITIATIVE and _linear_errors_mention(
                exc, "completedby"
            ):
                logger.warning("Retrying Linear completed-issues query without completedBy")
                continue
            raise
    raise last_error or RuntimeError("Linear completed-issues query failed")


def _page_completed_issues(
    query: str,
    base_variables: dict[str, Any],
) -> list[dict[str, Any]]:
    all_issues: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        variables = {**base_variables, "after": after}
        data = _execute_linear_query(query, variables)
        issues_conn = data.get("issues") or {}
        all_issues.extend(issues_conn.get("nodes") or [])
        page_info = issues_conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break
    return all_issues


def collect_linear_section(
    window_start: datetime,
    window_end: datetime,
    viewer_id: str | None = None,
) -> dict[str, Any]:
    if not os.getenv("LINEAR_API_KEY"):
        raise EnvironmentError("LINEAR_API_KEY environment variable not set")

    resolved_viewer_id = viewer_id
    if resolved_viewer_id is None:
        resolved_viewer_id = fetch_linear_viewer()["id"]

    raw_issues = fetch_completed_linear_issues(window_start, window_end)
    issues = select_linear_completed(
        raw_issues,
        resolved_viewer_id,
        window_start,
        window_end,
    )
    return {
        "issues": issues,
        "groups": group_linear_completed(issues),
        "count": len(issues),
    }


def collect_git_section(
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    username = os.getenv("GITHUB_USERNAME")
    token = os.getenv("GITHUB_ACCESS_TOKEN")
    if not username or not token:
        raise EnvironmentError("GITHUB_USERNAME or GITHUB_ACCESS_TOKEN not set")

    threshold = window_start.astimezone(timezone.utc)
    events = fetch_user_events(username, token, threshold)
    repos = summarize_events(events, threshold)
    # summarize_events keeps events at/after threshold; drop anything after window end
    window_end_utc = window_end.astimezone(timezone.utc)
    if window_end_utc < datetime.now(timezone.utc) - timedelta(seconds=5):
        repos = _clip_repo_commits_to_window(repos, events, threshold, window_end_utc)
    repos = [repo for repo in repos if repo.get("commits")]
    repos = sort_repos_by_commit_count(repos)

    oldest = None
    if events:
        last_created = events[-1].get("created_at")
        oldest = _parse_iso_datetime(last_created)
    capped = len(events) >= GITHUB_EVENTS_CAP and (
        oldest is None or oldest >= threshold
    )
    return {
        "repos": repos,
        "commits": flatten_repo_commits(repos),
        "count": sum(len(repo.get("commits") or []) for repo in repos),
        "events_capped": capped,
        "events_cap": GITHUB_EVENTS_CAP,
    }


def _clip_repo_commits_to_window(
    repos: list[dict[str, Any]],
    events: list[dict[str, Any]],
    threshold: datetime,
    window_end_utc: datetime,
) -> list[dict[str, Any]]:
    """Re-run summarize on events that fall inside the window when end != now."""
    in_window = []
    for event in events:
        created = _parse_iso_datetime(event.get("created_at"))
        if created is None:
            continue
        if threshold <= created <= window_end_utc:
            in_window.append(event)
    return summarize_events(in_window, threshold)


def collect_section(name: str, collector) -> dict[str, Any]:
    try:
        data = collector()
        data.setdefault("ok", True)
        data.setdefault("error", None)
        data.setdefault("name", name)
        return data
    except Exception as exc:
        logger.exception("%s section failed", name)
        return {
            "name": name,
            "ok": False,
            "error": str(exc),
            "count": 0,
        }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def _pulse_line(
    readwise: dict[str, Any],
    knowledge_hub: dict[str, Any],
    linear: dict[str, Any],
    git: dict[str, Any],
) -> str:
    parts: list[str] = []
    if readwise.get("ok"):
        highlight_count = len(readwise.get("highlights") or [])
        document_count = len(readwise.get("documents") or [])
        parts.append(f"{highlight_count} highlights")
        parts.append(f"{document_count} documents")
    else:
        parts.append("Readwise failed")
    if knowledge_hub.get("ok"):
        parts.append(f"{knowledge_hub.get('count', 0)} Knowledge Hub notes")
    else:
        parts.append("Knowledge Hub failed")
    if linear.get("ok"):
        parts.append(f"{linear.get('count', 0)} Linear issues")
    else:
        parts.append("Linear failed")
    if git.get("ok"):
        parts.append(f"{git.get('count', 0)} commits")
    else:
        parts.append("Git failed")
    return " · ".join(parts)


def _empty_or_failed(section: dict[str, Any], empty_message: str) -> str:
    if not section.get("ok"):
        error = html.escape(section.get("error") or "unknown error")
        return f"<p class='failed'>This section failed: {error}</p>"
    return f"<p class='empty'>{html.escape(empty_message)}</p>"


def _render_readwise_section(section: dict[str, Any]) -> str:
    parts = ["<h2>Readwise</h2>"]
    if not section.get("ok"):
        parts.append(_empty_or_failed(section, ""))
        return "\n".join(parts)

    highlights = section.get("highlights") or []
    documents = section.get("documents") or []
    parts.append(f"<h3>Highlights ({len(highlights)})</h3>")
    if highlights:
        parts.append("<ul>")
        for payload in highlights:
            title = html.escape(_nonempty(payload.get("title")) or "Untitled")
            source = html.escape(highlight_source_label(payload))
            quote = html.escape(
                _truncate(_nonempty(payload.get("text")) or "", SHORT_QUOTE_LIMIT)
            )
            url = highlight_open_url(payload)
            open_html = (
                f" <a href='{html.escape(url)}'>open</a>" if url else ""
            )
            parts.append(
                f"<li><strong>{title}</strong> — {source}<br>"
                f"<span class='quote'>{quote}</span>{open_html}</li>"
            )
        parts.append("</ul>")
    else:
        parts.append("<p class='empty'>No highlights this week.</p>")

    parts.append(f"<h3>Documents ({len(documents)})</h3>")
    if documents:
        parts.append("<ul>")
        for payload in documents:
            title = html.escape(_nonempty(payload.get("title")) or "Untitled")
            url = document_reader_url(payload)
            if url:
                parts.append(
                    f"<li><a href='{html.escape(url)}'>{title}</a></li>"
                )
            else:
                parts.append(f"<li>{title}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p class='empty'>No Reader documents this week.</p>")
    return "\n".join(parts)


def _render_knowledge_hub_section(section: dict[str, Any]) -> str:
    parts = ["<h2>Knowledge Hub consumption</h2>"]
    notes = section.get("notes") or []
    if not section.get("ok") or not notes:
        parts.append(_empty_or_failed(section, "No Knowledge Hub notes this week."))
        return "\n".join(parts)

    parts.append("<ul>")
    for note in notes:
        title = html.escape(note.get("title") or "Untitled")
        kind = note.get("kind") or "article"
        label = "YouTube" if kind == "youtube" else "Article"
        parts.append(
            f"<li>{title} <span class='badge'>{html.escape(label)}</span></li>"
        )
    parts.append("</ul>")
    return "\n".join(parts)


def _render_linear_section(section: dict[str, Any]) -> str:
    parts = ["<h2>Linear completed</h2>"]
    issues = section.get("issues") or []
    if not section.get("ok") or not issues:
        parts.append(_empty_or_failed(section, "No completed Linear issues this week."))
        return "\n".join(parts)

    for group in section.get("groups") or group_linear_completed(issues):
        parts.append(f"<h3>{html.escape(group['initiative_name'])}</h3>")
        for project in group.get("projects") or []:
            parts.append(f"<h4>Project: {html.escape(project['project_name'])}</h4>")
            parts.append("<ul>")
            for issue in project.get("issues") or []:
                identifier = html.escape(issue.get("identifier") or "")
                title = html.escape(issue.get("title") or "")
                url = issue.get("url") or ""
                if url:
                    parts.append(
                        f"<li><a href='{html.escape(url)}'><strong>{identifier}</strong></a>"
                        f" — {title}</li>"
                    )
                else:
                    parts.append(f"<li><strong>{identifier}</strong> — {title}</li>")
            parts.append("</ul>")
    return "\n".join(parts)


def _render_git_section(section: dict[str, Any]) -> str:
    parts = ["<h2>Git commits</h2>"]
    repos = section.get("repos") or []
    if not section.get("ok") or not repos:
        parts.append(_empty_or_failed(section, "No git commits this week."))
        return "\n".join(parts)

    if section.get("events_capped"):
        cap = section.get("events_cap") or GITHUB_EVENTS_CAP
        parts.append(
            f"<p class='meta'>GitHub Events API cap reached ({cap} events). "
            "Older commits in this 7-day window may be missing.</p>"
        )

    for repo in repos:
        repo_name = html.escape(repo.get("repo") or "unknown")
        commits = repo.get("commits") or []
        parts.append(f"<h3>{repo_name} ({len(commits)})</h3>")
        parts.append("<ul>")
        for commit in commits:
            message = html.escape(commit.get("message") or "")
            sha = html.escape(commit.get("sha") or "")
            parts.append(f"<li>{message} <code>{sha}</code> — {repo_name}</li>")
        parts.append("</ul>")
    return "\n".join(parts)


def build_html_email(
    now_local: datetime,
    window_start: datetime,
    window_end: datetime,
    readwise: dict[str, Any],
    knowledge_hub: dict[str, Any],
    linear: dict[str, Any],
    git: dict[str, Any],
) -> str:
    now_local = _ensure_system_timezone(now_local)
    window_start = _ensure_system_timezone(window_start)
    window_end = _ensure_system_timezone(window_end)
    pulse = _pulse_line(readwise, knowledge_hub, linear, git)
    start_label = f"{window_start.strftime('%b')} {window_start.day}"
    end_label = f"{window_end.strftime('%b')} {window_end.day}"

    return "\n".join(
        [
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
            ".pulse { font-size: 1.05rem; margin: 1rem 0 1.5rem; }",
            ".quote { color: #4b5563; }",
            ".empty { color: #6b7280; font-style: italic; }",
            ".failed { color: #b91c1c; }",
            ".badge { display: inline-block; padding: 0.12rem 0.4rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; background: #eef2ff; color: #3730a3; }",
            "code { font-size: 0.85em; background: #f3f4f6; padding: 0.1rem 0.3rem; border-radius: 4px; }",
            "</style>",
            "</head>",
            "<body>",
            "<h1>Sunday wrap-up</h1>",
            f"<p class='meta'>Window: {html.escape(start_label)} – {html.escape(end_label)} "
            f"({html.escape(now_local.tzname() or 'local')})</p>",
            f"<p class='pulse'>{html.escape(pulse)}</p>",
            _render_readwise_section(readwise),
            _render_knowledge_hub_section(knowledge_hub),
            _render_linear_section(linear),
            _render_git_section(git),
            "</body>",
            "</html>",
        ]
    )


def sunday_wrap_up_subject(now_local: datetime) -> str:
    local = _ensure_system_timezone(now_local)
    return f"Sunday wrap-up — {local.strftime('%b')} {local.day}"


def run_sunday_wrap_up_email(
    dry_run: bool = False,
    output: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Generate and send the Sunday wrap-up email. Always sends when generation succeeds."""
    load_dotenv()

    now_local = _ensure_system_timezone(now) if now else datetime.now(SYSTEM_TZ)
    window_start, window_end = rolling_window(now_local)
    logger.info(
        "Sunday wrap-up window %s → %s",
        window_start.isoformat(),
        window_end.isoformat(),
    )

    readwise = collect_section(
        "readwise",
        lambda: collect_readwise_section(window_start, window_end),
    )
    knowledge_hub = collect_section(
        "knowledge_hub",
        lambda: collect_knowledge_hub_notes(window_start, window_end),
    )
    linear = collect_section(
        "linear",
        lambda: collect_linear_section(window_start, window_end),
    )
    git = collect_section(
        "git",
        lambda: collect_git_section(window_start, window_end),
    )

    html_body = build_html_email(
        now_local,
        window_start,
        window_end,
        readwise,
        knowledge_hub,
        linear,
        git,
    )

    if output:
        output_path = Path(output)
        output_path.write_text(html_body, encoding="utf-8")
        logger.info("Saved wrap-up HTML to %s", output_path)

    if dry_run:
        logger.info("Dry run completed; Sunday wrap-up email not sent.")
        return True

    subject = sunday_wrap_up_subject(now_local)
    sent = send_html_email(subject, html_body)
    if sent:
        logger.info("Sunday wrap-up email sent successfully.")
    else:
        logger.error("Failed to send Sunday wrap-up email.")
    return sent


def main():
    parser = argparse.ArgumentParser(description="Send Sunday wrap-up email")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate wrap-up but do not send the email",
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

    success = run_sunday_wrap_up_email(
        dry_run=args.dry_run,
        output=args.output,
    )
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
