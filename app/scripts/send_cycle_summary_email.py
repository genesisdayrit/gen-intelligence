#!/usr/bin/env python3
"""Send Weekly Cycle AI Summary Email to Gmail.

Generates and sends a comprehensive weekly summary email containing:
1. Headlines from Last Cycle (AI-generated from completed work)
2. New Projected Headlines (AI-generated from "This Week" plans)
3. Initiative Completions (organized by initiative → project → completed issues)
4. Full List of Todoist Completions

Usage:
    python -m scripts.send_cycle_summary_email                    # Send for previous cycle
    python -m scripts.send_cycle_summary_email --current          # Send for current cycle
    python -m scripts.send_cycle_summary_email --dry-run          # Generate without sending
    python -m scripts.send_cycle_summary_email --output email.html # Save HTML to file
    python -m scripts.send_cycle_summary_email --debug            # Enable debug logging
    python -m scripts.send_cycle_summary_email --all-initiatives  # Include all initiatives (not just active)

Requires environment variables:
    - GMAIL_ACCOUNT, GMAIL_PASSWORD (for sending)
    - LINEAR_API_KEY (for Linear data)
    - OPENAI_API_KEY (for AI headline generation)
    - TODOIST_ACCESS_TOKEN (for Todoist completions)
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import pytz
from dotenv import load_dotenv

# Add app directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.generate_cycle_summary_data import (
    get_cycle_bounds,
    enrich_initiative_for_cycle,
)
from scripts.generate_next_cycle_headlines import (
    fetch_active_initiatives_with_updates,
    extract_this_week_section,
    generate_headline_with_llm,
)
from scripts.linear.sync_utils import fetch_initiatives
from services.email.gmail_client import send_html_email
from scripts.generate_latest_headlines import (
    get_openai_client,
    generate_initiative_headlines,
    generate_other_headlines,
    fetch_todoist_completions,
    fetch_all_completed_issues_in_range,
    get_all_completed_issues_from_initiative,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

GPT4O_MINI_MODEL = "gpt-4o-mini"

# =============================================================================
# Data Collection Functions
# =============================================================================


def collect_last_cycle_headlines(
    client,
    cycle_start: datetime,
    cycle_end: datetime,
    include_all_initiatives: bool = False,
) -> dict:
    """Generate headlines from completed work in the previous cycle.

    Returns:
        Dict with initiative_headlines list and other_headlines
    """
    logger.info("Collecting last cycle headlines...")

    # Fetch initiatives
    all_initiatives = fetch_initiatives(include_archived=False)
    if include_all_initiatives:
        initiatives = all_initiatives
        logger.info(f"Found {len(initiatives)} total initiatives (all included)")
    else:
        initiatives = [i for i in all_initiatives if i.get("status") == "Active"]
        logger.info(f"Found {len(initiatives)} active initiatives")

    # Track project IDs for "other" calculation
    active_project_ids = set()

    # Process each initiative
    initiative_headlines = []

    for i, init in enumerate(initiatives):
        init_name = init.get("name", "Unknown")
        logger.info(
            f"Processing initiative {i + 1}/{len(initiatives)}: {init_name}"
        )

        # Enrich with cycle data
        enriched = enrich_initiative_for_cycle(init, cycle_start, cycle_end)
        if enriched is None:
            logger.warning(f"Could not access initiative: {init_name}")
            continue

        # Track project IDs
        for proj in enriched.get("projects", []):
            active_project_ids.add(proj["id"])

        # Get completed issues across all projects
        completed_issues = get_all_completed_issues_from_initiative(enriched)

        # Generate headlines
        headlines_result = generate_initiative_headlines(
            client=client,
            initiative_name=init_name,
            updates=enriched.get("updates_in_cycle", []),
            projects=enriched.get("projects", []),
            completed_issues=completed_issues,
        )

        initiative_headlines.append({
            "initiative_name": init_name,
            "parsed_headlines": headlines_result.get("parsed_headlines", []),
        })

        logger.info(
            f"  Generated {len(headlines_result.get('parsed_headlines', []))} headlines"
        )

    # Get "other" completed issues
    logger.info("Fetching other completed issues...")
    all_completed = fetch_all_completed_issues_in_range(cycle_start, cycle_end)
    other_completed = [
        i
        for i in all_completed
        if i.get("project", {}).get("id") not in active_project_ids
    ]
    logger.info(
        f"Found {len(all_completed)} total, {len(other_completed)} outside initiatives"
    )

    # Get Todoist completions
    logger.info("Fetching Todoist completions...")
    todoist_tasks = fetch_todoist_completions(cycle_start, cycle_end)

    # Generate "other" headlines (passing existing initiative headlines to avoid duplicates)
    logger.info("Generating other headlines...")
    other_headlines = generate_other_headlines(
        client, other_completed, todoist_tasks, existing_headlines=initiative_headlines
    )

    return {
        "initiative_headlines": initiative_headlines,
        "other_headlines": other_headlines.get("parsed_headlines", []),
        "todoist_tasks": todoist_tasks,
    }


def collect_projected_headlines(
    client, model: str = GPT4O_MINI_MODEL, include_all_initiatives: bool = False
) -> list[dict]:
    """Generate projected headlines for the upcoming cycle.

    Args:
        client: OpenAI client
        model: Model to use for generation
        include_all_initiatives: If True, include all initiatives; if False, only active ones

    Returns:
        List of dicts with initiative_name and projected_headline
    """
    logger.info("Collecting projected headlines...")

    initiatives_data = fetch_active_initiatives_with_updates(include_all=include_all_initiatives)

    if not initiatives_data:
        logger.warning("No active initiatives with updates found")
        return []

    projected = []

    for item in initiatives_data:
        initiative_name = item["initiative_name"]
        update = item["latest_update"]
        raw_body = update.get("body", "")

        # Extract This Week section
        this_week_section = extract_this_week_section(raw_body)

        if this_week_section:
            logger.info(f"Generating projected headline for: {initiative_name}")
            llm_result = generate_headline_with_llm(
                client,
                initiative_name,
                this_week_section,
                model,
            )
            projected.append({
                "initiative_name": initiative_name,
                "projected_headline": llm_result.get("projected_headline"),
            })
            logger.info(f"  -> {llm_result.get('projected_headline')}")
        else:
            logger.debug(f"No 'This Week' section found for: {initiative_name}")

    return projected


def collect_initiative_completions(
    cycle_start: datetime,
    cycle_end: datetime,
    last_cycle_headlines: dict,
    projected_headlines: list[dict],
    include_all_initiatives: bool = False,
) -> list[dict]:
    """Collect initiative completions with completed issues per project.

    Returns:
        List of initiative completion dicts
    """
    logger.info("Collecting initiative completions...")

    # Build lookup maps for headlines
    last_headline_map = {
        h["initiative_name"]: h.get("parsed_headlines", [])
        for h in last_cycle_headlines.get("initiative_headlines", [])
    }
    projected_map = {
        h["initiative_name"]: h.get("projected_headline")
        for h in projected_headlines
    }

    # Fetch initiatives
    all_initiatives = fetch_initiatives(include_archived=False)
    if include_all_initiatives:
        initiatives = all_initiatives
    else:
        initiatives = [i for i in all_initiatives if i.get("status") == "Active"]

    completions = []

    for init in initiatives:
        init_name = init.get("name", "Unknown")

        # Enrich with cycle data
        enriched = enrich_initiative_for_cycle(init, cycle_start, cycle_end)
        if enriched is None:
            continue

        # Build project completions
        projects = []
        for proj in enriched.get("projects", []):
            completed_issues = proj.get("completed_issues", [])
            if completed_issues:
                projects.append({
                    "name": proj.get("name", "Unknown Project"),
                    "completed_issues": [
                        {
                            "identifier": issue.get("identifier", "?"),
                            "title": issue.get("title", "Untitled"),
                        }
                        for issue in completed_issues
                    ],
                })

        # Only include if there's activity
        last_headlines = last_headline_map.get(init_name, [])
        projected_headline = projected_map.get(init_name)

        if projects or last_headlines or projected_headline:
            completions.append({
                "initiative_name": init_name,
                "last_cycle_headlines": last_headlines,
                "projected_headline": projected_headline,
                "projects": projects,
            })

    return completions


# =============================================================================
# HTML Formatting Functions
# =============================================================================


def format_date_range(cycle_start: datetime, cycle_end: datetime) -> str:
    """Format cycle date range for display."""
    start_str = cycle_start.strftime("%b %d")
    end_str = cycle_end.strftime("%b %d, %Y")
    return f"{start_str} - {end_str}"


def build_html_email(
    cycle_start: datetime,
    cycle_end: datetime,
    last_cycle_headlines: dict,
    projected_headlines: list[dict],
    initiative_completions: list[dict],
    todoist_tasks: list[dict],
) -> str:
    """Build the full HTML email content."""
    date_range = format_date_range(cycle_start, cycle_end)

    html_parts = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        "<meta charset='utf-8'>",
        "<style>",
        "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }",
        "h2 { color: #1a1a1a; border-bottom: 2px solid #e1e1e1; padding-bottom: 10px; }",
        "h3 { color: #2c3e50; margin-top: 30px; }",
        "h4 { color: #34495e; margin-top: 20px; }",
        "ol, ul { margin: 10px 0; }",
        "li { margin: 5px 0; }",
        "em { color: #666; }",
        ".initiative { margin-bottom: 25px; padding: 15px; background: #f8f9fa; border-radius: 8px; }",
        ".project { margin-left: 20px; margin-top: 10px; }",
        ".todoist-task { color: #555; }",
        ".headline { font-weight: 500; }",
        "</style>",
        "</head>",
        "<body>",
        f"<h2>Weekly Cycle Summary</h2>",
        f"<p><em>Cycle: {date_range}</em></p>",
    ]

    # Section 1: Headlines from Last Cycle
    html_parts.append("<h3>Headlines from Last Cycle:</h3>")
    html_parts.append("<ol>")

    for item in last_cycle_headlines.get("initiative_headlines", []):
        headlines = item.get("parsed_headlines", [])
        if headlines:
            html_parts.append(f"<li><strong>{item['initiative_name']}</strong>")
            html_parts.append("<ol>")
            for headline in headlines:
                html_parts.append(f"<li class='headline'>{headline}</li>")
            html_parts.append("</ol></li>")

    # Add "Other" headlines
    other_headlines = last_cycle_headlines.get("other_headlines", [])
    if other_headlines:
        html_parts.append("<li><strong>Other Headlines</strong>")
        html_parts.append("<ol>")
        for headline in other_headlines:
            html_parts.append(f"<li class='headline'>{headline}</li>")
        html_parts.append("</ol></li>")

    html_parts.append("</ol>")

    # Section 2: New Projected Headlines
    html_parts.append(
        "<h3>New Projected Headlines (anticipate how you're going to win this week):</h3>"
    )
    html_parts.append("<ol>")

    for item in projected_headlines:
        if item.get("projected_headline"):
            html_parts.append(
                f"<li><strong>{item['initiative_name']}</strong>: {item['projected_headline']}</li>"
            )

    html_parts.append("</ol>")

    # Section 3: Initiative Completions
    html_parts.append("<h3>Initiative Completions</h3>")

    for init in initiative_completions:
        html_parts.append(f"<div class='initiative'>")
        html_parts.append(f"<h4>{init['initiative_name']}</h4>")
        html_parts.append("<ul>")

        # AI Parsed Headlines from last cycle
        for headline in init.get("last_cycle_headlines", []):
            html_parts.append(f"<li><em>Last Cycle: {headline}</em></li>")

        # Projected headline for next cycle
        if init.get("projected_headline"):
            html_parts.append(
                f"<li><em>Next: {init['projected_headline']}</em></li>"
            )

        # Projects with completed issues
        for proj in init.get("projects", []):
            html_parts.append(f"<li class='project'><strong>{proj['name']}</strong>")
            html_parts.append("<ul>")
            for issue in proj.get("completed_issues", []):
                html_parts.append(
                    f"<li>{issue['identifier']}: {issue['title']}</li>"
                )
            html_parts.append("</ul></li>")

        html_parts.append("</ul>")
        html_parts.append("</div>")

    # Section 4: Full List of Todoist Completions
    html_parts.append("<h3>Full List of Todoist Completions</h3>")
    html_parts.append("<ul>")

    for task in todoist_tasks:
        content = task.get("content", "Unknown task")
        completed_at = task.get("completed_at", "")
        if completed_at:
            # Parse and format the date
            try:
                dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                date_str = dt.strftime("%b %d")
                html_parts.append(
                    f"<li class='todoist-task'>{content} <em>(completed {date_str})</em></li>"
                )
            except ValueError:
                html_parts.append(f"<li class='todoist-task'>{content}</li>")
        else:
            html_parts.append(f"<li class='todoist-task'>{content}</li>")

    html_parts.append("</ul>")

    html_parts.extend(["</body>", "</html>"])

    return "\n".join(html_parts)


# =============================================================================
# Main Entry Point
# =============================================================================


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Send weekly cycle AI summary email to Gmail"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate email but don't send it",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Save HTML to file (optional)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--current",
        action="store_true",
        help="Use current cycle instead of previous cycle",
    )
    parser.add_argument(
        "--all-initiatives",
        action="store_true",
        help="Include all initiatives (not just active ones)",
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    # Determine cycle
    tz = pytz.timezone(os.getenv("SYSTEM_TIMEZONE", "US/Pacific"))
    cycle_type = "current" if args.current else "previous"
    cycle_start, cycle_end = get_cycle_bounds(cycle_type, tz)

    logger.info(f"Generating summary for cycle: {cycle_start.date()} to {cycle_end.date()}")

    # Initialize OpenAI client
    client = get_openai_client()

    # ==========================================================================
    # 1. Collect Headlines from Last Cycle
    # ==========================================================================
    last_cycle_headlines = collect_last_cycle_headlines(
        client, cycle_start, cycle_end, args.all_initiatives
    )

    # ==========================================================================
    # 2. Collect Projected Headlines
    # ==========================================================================
    projected_headlines = collect_projected_headlines(
        client, include_all_initiatives=args.all_initiatives
    )

    # ==========================================================================
    # 3. Collect Initiative Completions
    # ==========================================================================
    initiative_completions = collect_initiative_completions(
        cycle_start, cycle_end, last_cycle_headlines, projected_headlines, args.all_initiatives
    )

    # ==========================================================================
    # 4. Build HTML Email
    # ==========================================================================
    html_content = build_html_email(
        cycle_start,
        cycle_end,
        last_cycle_headlines,
        projected_headlines,
        initiative_completions,
        last_cycle_headlines.get("todoist_tasks", []),
    )

    # ==========================================================================
    # 5. Save/Send
    # ==========================================================================
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(html_content, encoding="utf-8")
        logger.info(f"Saved HTML to: {output_path}")

    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN - Email would be sent with the following content:")
        print("=" * 60)
        print(f"Subject: Weekly Cycle Summary ({format_date_range(cycle_start, cycle_end)})")
        print("=" * 60)
        if not args.output:
            print(html_content)
        else:
            print(f"(HTML saved to {args.output})")
        print("=" * 60)
        return

    # Send the email
    subject = f"Weekly Cycle Summary ({format_date_range(cycle_start, cycle_end)})"
    success = send_html_email(subject, html_content)

    if success:
        print(f"Email sent successfully!")
        print(f"Subject: {subject}")
    else:
        print("Failed to send email. Check logs for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()
