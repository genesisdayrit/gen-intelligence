#!/usr/bin/env python3
"""Roll up recent cross-initiative activity into a main-thread initiative update.

Gathers everything that changed in the last N hours (default 4) across the
active initiatives that are NOT the 'main-thread' initiative:

  * initiative-level status updates (full body)
  * project-level status updates (full body)
  * initiative/project documents — only TODAY's dated section (the block from
    today's date header down to the next date header)

It hands that context to an LLM, which synthesizes a concise high-level
initiative update, and posts it on the 'main-thread' initiative. This keeps a
single rolling overview of the whole system while preserving the specific
context worth remembering.

Usage:
    python -m scripts.send_main_thread_rollup
    python -m scripts.send_main_thread_rollup --dry-run
    python -m scripts.send_main_thread_rollup --hours 6 --dry-run
    python -m scripts.send_main_thread_rollup --output rollup.md
    python -m scripts.send_main_thread_rollup --debug
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# Add app directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import SYSTEM_TZ
from scripts.linear.sync_utils import (
    create_initiative_update,
    fetch_all_pages,
    fetch_initiative_documents,
    fetch_initiative_labels,
    fetch_initiative_projects,
    fetch_initiative_updates,
    fetch_project_documents,
    fetch_project_updates,
    set_initiative_labels,
)

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 4
MAIN_THREAD_LABEL = "main-thread"
# Adding this label to the main-thread initiative triggers an on-demand rollup;
# the label is removed again once the rollup has been posted.
TRIGGER_LABEL = "status-update"
ACTIVE_STATUS = "Active"
SUMMARY_MODEL = "gpt-4o-mini"

# sync_utils' INITIATIVES_QUERY doesn't carry labels; we need them to identify
# the main-thread target and to exclude it from the sources.
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


# =============================================================================
# Date-header section parsing (for documents)
# =============================================================================

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
    )
}

# A "date header" line: optional leading #'s, a month name (abbrev or full),
# a day number with an optional ordinal suffix, and nothing else on the line.
# Matches both `# Jul 4th` and a bare `Jul 6th`.
_DATE_HEADER_RE = re.compile(
    r"^\s*#*\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*$",
    re.IGNORECASE,
)


def _header_month_day(line: str) -> tuple[int, int] | None:
    """Return (month, day) if the line is a date header, else None."""
    m = _DATE_HEADER_RE.match(line)
    if not m:
        return None
    return _MONTHS[m.group(1).title()[:3]], int(m.group(2))


def extract_today_section(content: str | None, today: tuple[int, int]) -> str | None:
    """Extract the block under the header matching `today` (month, day).

    The section runs from today's date header down to the next date header
    (the "following date" break point). Returns None if there is no section
    dated today.
    """
    if not content:
        return None
    lines = content.split("\n")
    headers = [(i, _header_month_day(line)) for i, line in enumerate(lines)]
    headers = [(i, md) for i, md in headers if md]

    for pos, (idx, md) in enumerate(headers):
        if md == today:
            start = idx
            end = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
            return "\n".join(lines[start:end]).strip()
    return None


# =============================================================================
# Fetching recent activity
# =============================================================================


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def last_touched(item: dict) -> datetime | None:
    stamps = [parse_dt(item.get("updatedAt")), parse_dt(item.get("createdAt"))]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def is_recent(item: dict, threshold: datetime) -> bool:
    touched = last_touched(item)
    return touched is not None and touched >= threshold


def fetch_active_initiatives() -> list[dict]:
    """Active (status == Active, non-archived) initiatives with label names."""
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


def _update_entry(u: dict) -> dict:
    return {
        "author": (u.get("user") or {}).get("name"),
        "health": u.get("health"),
        "updatedAt": u.get("updatedAt") or u.get("createdAt"),
        "url": u.get("url"),
        "body": (u.get("body") or "").strip(),
    }


def collect_recent_activity(
    initiative: dict, threshold: datetime, today: tuple[int, int]
) -> dict:
    """Gather in-window updates and today-section documents for an initiative."""
    iid = initiative["id"]

    ini_updates = [_update_entry(u) for u in fetch_initiative_updates(iid) if is_recent(u, threshold)]

    ini_docs = []
    for d in fetch_initiative_documents(iid):
        if not is_recent(d, threshold):
            continue
        section = extract_today_section(d.get("content"), today)
        if section:  # skip docs with no section dated today
            ini_docs.append({"title": d.get("title"), "url": d.get("url"), "today_section": section})

    projects = []
    for project in fetch_initiative_projects(iid):
        pid = project["id"]
        p_updates = [_update_entry(u) for u in fetch_project_updates(pid) if is_recent(u, threshold)]
        p_docs = []
        for d in fetch_project_documents(pid):
            if not is_recent(d, threshold):
                continue
            section = extract_today_section(d.get("content"), today)
            if section:
                p_docs.append({"title": d.get("title"), "url": d.get("url"), "today_section": section})
        if p_updates or p_docs:
            projects.append(
                {"name": project.get("name"), "updates": p_updates, "documents": p_docs}
            )

    return {
        "name": initiative["name"],
        "updates": ini_updates,
        "documents": ini_docs,
        "projects": projects,
    }


def has_activity(report: dict) -> bool:
    return bool(report["updates"] or report["documents"] or report["projects"])


# =============================================================================
# Context block + LLM summarizer
# =============================================================================

SUMMARY_SYSTEM_PROMPT = """You write concise, high-level "main thread" initiative updates for a personal productivity system.

The main thread is a single rolling overview initiative that sits above the user's other active initiatives and projects. Your job: read the raw activity from the last few hours (status updates and today's document sections) and synthesize a compact update that gives the user a high-level overview while preserving the specific context worth remembering.

Constraints:
- Output GitHub-flavored markdown only — no preamble, no closing remarks.
- Start with a one-line `## Summary` section: 1-2 sentences on the overall shape of the last few hours.
- Then a `## By initiative` section: one `### <Initiative name>` subsection per initiative that had activity, with tight bullets. Nest project activity under its initiative and name the project inline.
- Keep bullets short and high-level, but keep concrete specifics (names, systems, decisions, numbers) — that's the point of the main thread.
- Synthesize and de-duplicate; don't just transcribe. If a status update and a document section cover the same thing, merge them.
- First person, plain voice. No hype, no emojis. Never fabricate — every bullet must trace to the provided context."""

SUMMARY_USER_PROMPT_TEMPLATE = """Generate the main-thread initiative update for {now_local}, covering activity from the last {hours} hours.

Below is the raw activity gathered from the OTHER active initiatives (the main thread itself is excluded). Status updates include their full body; documents include only today's dated section.

# Activity

{activity_block}
"""


def _get_openai_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY not set")
    return OpenAI(api_key=api_key)


def _format_updates(updates: list[dict], indent: str) -> list[str]:
    lines = []
    for u in updates:
        meta = f"status update by {u['author']} ({u['health']}) — {u['updatedAt']}"
        lines.append(f"{indent}- {meta}")
        body = u["body"] or "(empty body)"
        for bl in body.splitlines():
            lines.append(f"{indent}  {bl}")
    return lines


def _format_documents(documents: list[dict], indent: str) -> list[str]:
    lines = []
    for d in documents:
        lines.append(f"{indent}- document '{d['title']}' — today's section:")
        for bl in d["today_section"].splitlines():
            lines.append(f"{indent}  {bl}")
    return lines


def build_activity_block(reports: list[dict]) -> str:
    """Render the gathered activity into a plain-text context block for the LLM."""
    parts: list[str] = []
    for r in reports:
        parts.append(f"=== INITIATIVE: {r['name']} ===")
        parts.extend(_format_updates(r["updates"], ""))
        parts.extend(_format_documents(r["documents"], ""))
        for p in r["projects"]:
            parts.append(f"  --- project: {p['name']} ---")
            parts.extend(_format_updates(p["updates"], "  "))
            parts.extend(_format_documents(p["documents"], "  "))
        parts.append("")
    return "\n".join(parts).strip()


def generate_update_body(reports: list[dict], now_local: datetime, hours: int) -> str:
    """Call OpenAI to synthesize the main-thread update body."""
    client = _get_openai_client()
    user_prompt = SUMMARY_USER_PROMPT_TEMPLATE.format(
        now_local=now_local.strftime("%A, %B %d, %Y %I:%M %p %Z"),
        hours=hours,
        activity_block=build_activity_block(reports),
    )
    response = client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
    )
    return (response.choices[0].message.content or "").strip()


# =============================================================================
# Orchestrator
# =============================================================================


def _resolve_main_thread(initiatives: list[dict]) -> dict:
    """Return the single initiative carrying the main-thread label."""
    matches = [i for i in initiatives if MAIN_THREAD_LABEL in i["labels"]]
    if len(matches) == 0:
        raise RuntimeError(f"No active initiative carries the '{MAIN_THREAD_LABEL}' label.")
    if len(matches) > 1:
        names = ", ".join(i["name"] for i in matches)
        raise RuntimeError(
            f"Expected exactly one '{MAIN_THREAD_LABEL}' initiative, found {len(matches)}: {names}"
        )
    return matches[0]


def run_main_thread_rollup(
    hours: int = DEFAULT_WINDOW_HOURS,
    dry_run: bool = False,
    output: str | None = None,
    now_local: datetime | None = None,
) -> bool:
    """Generate and post the main-thread rollup. Returns True on success/skip."""
    load_dotenv()

    if not os.getenv("LINEAR_API_KEY"):
        logger.error("LINEAR_API_KEY not set")
        return False

    now_local = (now_local or datetime.now(SYSTEM_TZ)).astimezone(SYSTEM_TZ)
    threshold = now_local - timedelta(hours=hours)
    today = (now_local.month, now_local.day)

    try:
        initiatives = fetch_active_initiatives()
        main_thread = _resolve_main_thread(initiatives)
        logger.info("Main-thread target: %s (%s)", main_thread["name"], main_thread["id"])

        sources = [i for i in initiatives if MAIN_THREAD_LABEL not in i["labels"]]
        logger.info(
            "Scanning %d source initiatives for activity since %s",
            len(sources),
            threshold.isoformat(),
        )

        reports = [collect_recent_activity(i, threshold, today) for i in sources]
        active_reports = [r for r in reports if has_activity(r)]
        logger.info("%d initiative(s) had activity in the window", len(active_reports))

        if not active_reports:
            logger.info("No activity in the last %dh; nothing to post. Skipping.", hours)
            return True

        body = generate_update_body(active_reports, now_local, hours)
        if not body:
            logger.error("LLM returned empty body; aborting.")
            return False
        logger.info("Generated body (%d chars)", len(body))

        if output:
            Path(output).write_text(body, encoding="utf-8")
            logger.info("Wrote body to %s", output)

        if dry_run:
            logger.info("Dry run; not posting to Linear.")
            print(body)
            return True

        result = create_initiative_update(initiative_id=main_thread["id"], body=body)
        logger.info("Posted main-thread update: id=%s url=%s", result.get("id"), result.get("url"))
        return True
    except Exception:
        logger.exception("Main-thread rollup failed")
        return False


def run_label_triggered_rollup(initiative_id: str, hours: int = 6) -> bool:
    """On-demand rollup triggered by the `status-update` label on the main thread.

    Meant to be called from the Linear `Initiative` webhook for any initiative
    update. It is a safe no-op unless the given initiative carries BOTH the
    `main-thread` and `status-update` labels — so the webhook can call it for
    every initiative update without gating logic of its own.

    On a real trigger it runs the rollup and then removes the `status-update`
    label so the initiative returns to its resting state (and so removing the
    label doesn't itself re-trigger).

    Returns True if a rollup was run, False if it was a no-op.
    """
    load_dotenv()

    labels = fetch_initiative_labels(initiative_id)
    names = {l["name"].lower() for l in labels}

    if MAIN_THREAD_LABEL not in names or TRIGGER_LABEL not in names:
        # Not the main thread, or the trigger label isn't present — nothing to do.
        return False

    logger.info(
        "'%s' label detected on the main-thread initiative (%s); running on-demand rollup.",
        TRIGGER_LABEL,
        initiative_id,
    )

    try:
        run_main_thread_rollup(hours=hours)
    finally:
        # Always clear the trigger label, even if the rollup failed, so a stuck
        # label doesn't silently re-fire on the next unrelated initiative edit.
        remaining = [l["id"] for l in labels if l["name"].lower() != TRIGGER_LABEL]
        try:
            set_initiative_labels(initiative_id, remaining)
            logger.info("Removed '%s' label from initiative %s.", TRIGGER_LABEL, initiative_id)
        except Exception:
            logger.exception("Failed to remove '%s' label from %s", TRIGGER_LABEL, initiative_id)

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Roll up recent cross-initiative activity into a main-thread initiative update."
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_WINDOW_HOURS,
        help=f"Look-back window in hours (default: {DEFAULT_WINDOW_HOURS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate the body and print it, but do not post to Linear.",
    )
    parser.add_argument("--output", type=str, help="Optional path to write the generated markdown body.")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    success = run_main_thread_rollup(
        hours=args.hours,
        dry_run=args.dry_run,
        output=args.output,
    )
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
