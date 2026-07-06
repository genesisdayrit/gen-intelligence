#!/usr/bin/env python3
"""Pull recent activity across active initiatives (excluding 'main-thread').

For every active initiative that is NOT the 'main-thread' initiative, this
reports what changed in the last N hours (default 24):

  * initiative-level status updates edited/created in the window
  * initiative-level documents edited/created in the window
  * for each project under the initiative:
      - project status updates edited/created in the window
      - project documents edited/created in the window

A probe/test script: it only reads, and reuses the fetch helpers from
sync_utils so the field shapes match the rest of the Linear tooling.

Usage:
    python -m scripts.linear.test_recent_initiative_activity
    python -m scripts.linear.test_recent_initiative_activity --hours 48
    python -m scripts.linear.test_recent_initiative_activity --exclude-label main-thread
    python -m scripts.linear.test_recent_initiative_activity --json
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from scripts.linear.sync_utils import (
    fetch_all_pages,
    fetch_initiative_documents,
    fetch_initiative_projects,
    fetch_initiative_updates,
    fetch_project_documents,
    fetch_project_updates,
)

load_dotenv()

ACTIVE_STATUS = "Active"

# sync_utils' INITIATIVES_QUERY doesn't include labels, so use a local one
# that carries labels (needed to identify/exclude the 'main-thread' initiative).
ACTIVE_INITIATIVES_QUERY = """
query ActiveInitiatives($first: Int!, $after: String, $includeArchived: Boolean) {
  initiatives(first: $first, after: $after, includeArchived: $includeArchived, orderBy: updatedAt) {
    nodes {
      id
      name
      url
      status
      labels { nodes { id name } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def parse_dt(value: str | None) -> datetime | None:
    """Parse a Linear ISO 8601 timestamp (e.g. '2026-07-06T12:00:00.000Z')."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def last_touched(item: dict) -> datetime | None:
    """Most recent of an item's createdAt / updatedAt."""
    stamps = [parse_dt(item.get("updatedAt")), parse_dt(item.get("createdAt"))]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def is_recent(item: dict, threshold: datetime) -> bool:
    """True if the item was created or edited at/after `threshold`."""
    touched = last_touched(item)
    return touched is not None and touched >= threshold


def fetch_active_initiatives() -> list[dict]:
    """Active (status == Active, non-archived) initiatives, with label names."""
    nodes = fetch_all_pages(
        ACTIVE_INITIATIVES_QUERY,
        {"first": 50, "includeArchived": False},
        ["initiatives"],
    )
    out = []
    for n in nodes:
        if n.get("status") != ACTIVE_STATUS:
            continue
        out.append(
            {
                "id": n["id"],
                "name": n["name"],
                "url": n.get("url"),
                "labels": [l["name"].lower() for l in n.get("labels", {}).get("nodes", [])],
            }
        )
    return out


def summarize_update(u: dict) -> dict:
    return {
        "id": u["id"],
        "url": u.get("url"),
        "health": u.get("health"),
        "createdAt": u.get("createdAt"),
        "updatedAt": u.get("updatedAt"),
        "author": (u.get("user") or {}).get("name"),
        "body": u.get("body"),
    }


def summarize_document(d: dict) -> dict:
    return {
        "id": d["id"],
        "title": d.get("title"),
        "url": d.get("url"),
        "createdAt": d.get("createdAt"),
        "updatedAt": d.get("updatedAt"),
        "content": d.get("content"),
    }


def collect_recent_activity(initiative: dict, threshold: datetime) -> dict:
    """Gather all in-window updates & documents for an initiative + its projects."""
    iid = initiative["id"]

    ini_updates = [summarize_update(u) for u in fetch_initiative_updates(iid) if is_recent(u, threshold)]
    ini_docs = [summarize_document(d) for d in fetch_initiative_documents(iid) if is_recent(d, threshold)]

    projects = []
    for project in fetch_initiative_projects(iid):
        pid = project["id"]
        p_updates = [summarize_update(u) for u in fetch_project_updates(pid) if is_recent(u, threshold)]
        p_docs = [summarize_document(d) for d in fetch_project_documents(pid) if is_recent(d, threshold)]
        if p_updates or p_docs:
            projects.append(
                {
                    "id": pid,
                    "name": project.get("name"),
                    "url": project.get("url"),
                    "updates": p_updates,
                    "documents": p_docs,
                }
            )

    return {
        "id": iid,
        "name": initiative["name"],
        "url": initiative.get("url"),
        "initiative_updates": ini_updates,
        "initiative_documents": ini_docs,
        "projects": projects,
    }


def has_activity(report: dict) -> bool:
    return bool(
        report["initiative_updates"]
        or report["initiative_documents"]
        or report["projects"]
    )


def print_report(report: dict) -> None:
    print(f"\n■ {report['name']}")
    print(f"  {report['url']}")

    for u in report["initiative_updates"]:
        print(f"    · status update by {u['author']} ({u['health']}) — edited {u['updatedAt']}")
        print(f"      {u['url']}")
    for d in report["initiative_documents"]:
        print(f"    · document '{d['title']}' — edited {d['updatedAt']}")
        print(f"      {d['url']}")

    for p in report["projects"]:
        print(f"    ▸ project: {p['name']}")
        for u in p["updates"]:
            print(f"        · status update by {u['author']} ({u['health']}) — edited {u['updatedAt']}")
            print(f"          {u['url']}")
        for d in p["documents"]:
            print(f"        · document '{d['title']}' — edited {d['updatedAt']}")
            print(f"          {d['url']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pull recent activity across active initiatives (excluding main-thread)."
    )
    parser.add_argument("--hours", type=int, default=24, help="Look-back window in hours (default: 24).")
    parser.add_argument(
        "--exclude-label",
        default="main-thread",
        help="Skip initiatives carrying this label (default: main-thread).",
    )
    parser.add_argument("--json", action="store_true", help="Emit reports as JSON.")
    args = parser.parse_args()

    # datetime.now(timezone.utc) is fine at runtime; scripts (not workflow) may use it.
    threshold = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    exclude = args.exclude_label.strip().lower()

    initiatives = fetch_active_initiatives()
    targets = [i for i in initiatives if exclude not in i["labels"]]
    skipped = [i for i in initiatives if exclude in i["labels"]]

    if not args.json:
        print(f"Window: last {args.hours}h (since {threshold.isoformat()})")
        print(
            f"Active initiatives: {len(initiatives)}  |  scanning: {len(targets)}  |  "
            f"skipped ('{exclude}'): {len(skipped)}"
        )

    reports = [collect_recent_activity(i, threshold) for i in targets]
    active_reports = [r for r in reports if has_activity(r)]

    if args.json:
        print(json.dumps(active_reports, indent=2))
        sys.exit(0)

    if not active_reports:
        print(f"\nNo initiative/project activity in the last {args.hours}h.")
        return

    for r in active_reports:
        print_report(r)

    print(f"\n{len(active_reports)} initiative(s) with recent activity.")


if __name__ == "__main__":
    main()
