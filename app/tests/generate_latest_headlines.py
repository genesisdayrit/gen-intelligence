#!/usr/bin/env python3
"""Script for generating Latest Headlines from Linear cycle data.

Uses GPT-4o-mini for incremental headline generation per initiative,
and GPT-4o for final synthesis into a markdown section.

Usage:
    python -m tests.generate_latest_headlines              # Current cycle
    python -m tests.generate_latest_headlines --previous   # Previous cycle
    python -m tests.generate_latest_headlines --debug      # With debug logging
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
import pytz
from dotenv import load_dotenv
from openai import OpenAI

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.generate_cycle_summary_data import (
    get_cycle_bounds,
    is_within_cycle,
    fetch_all_completed_issues_in_range,
    enrich_initiative_for_cycle,
)
from scripts.linear.sync_utils import fetch_initiatives

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

GPT4O_MINI_MODEL = "gpt-4o-mini"
GPT4O_MODEL = "gpt-4o"

TODOIST_API_V1_BASE = "https://api.todoist.com/api/v1"
COMPLETED_TASKS_ENDPOINT = f"{TODOIST_API_V1_BASE}/tasks/completed/by_completion_date"

# =============================================================================
# Prompt Templates
# =============================================================================

INITIATIVE_HEADLINE_PROMPT = """You are a technical writer creating weekly headlines for a personal productivity summary.

Given the following data about the "{initiative_name}" initiative from this week:

## Initiative Updates:
{updates_text}

## Project Updates:
{project_updates_text}

## Completed Issues:
{completed_issues_text}

Generate 1-3 concise headlines (max 15 words each) that capture the most significant accomplishments or progress. Focus on outcomes and impact, not just activities.

Rules:
- Each headline should be standalone and meaningful
- Prioritize concrete accomplishments over vague progress
- Use active voice
- If no significant progress this week, return an empty array

Return your response as a JSON object with a "headlines" key containing an array of strings:
{{"headlines": ["Headline 1", "Headline 2"]}}
"""

OTHER_HEADLINES_PROMPT = """You are a technical writer identifying headline-worthy accomplishments.

Given these completed items NOT related to active initiatives:

## Completed Linear Issues:
{issues_text}

## Completed Todoist Tasks:
{todoist_text}

Identify 0-3 items that are genuinely headline-worthy (significant accomplishments, not routine tasks).

Rules:
- Only include truly notable items
- Ignore routine/administrative tasks (meetings, emails, small fixes)
- If nothing is headline-worthy, return an empty array

Return your response as a JSON object with a "headlines" key containing an array of strings:
{{"headlines": ["Headline 1", "Headline 2"]}}
"""

SYNTHESIS_PROMPT = """You are creating the "Latest Headlines" section for a weekly summary.

## Headlines by Initiative:
{initiative_headlines_json}

## Other Headlines:
{other_headlines_json}

Combine these into a cohesive markdown section. Requirements:
1. Group headlines by initiative (use the initiative name as the header)
2. Add "Other Headlines" section at the end if there are any
3. Rewrite for consistency in tone and style
4. Remove duplicates or redundant items
5. Limit to 2-3 headlines per initiative (choose the best)
6. Total should be 8-12 headlines max
7. Skip initiatives with no headlines

Output ONLY the markdown section in this exact format (no additional text):
### Latest Headlines

1. [Initiative Name]
   1. [Headline]
   2. [Headline]
2. [Initiative Name]
   1. [Headline]
3. Other Headlines
   1. [Headline]
"""


# =============================================================================
# OpenAI Client
# =============================================================================


def get_openai_client() -> OpenAI:
    """Initialize OpenAI client."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not set in environment")
        sys.exit(1)
    return OpenAI(api_key=api_key)


# =============================================================================
# Todoist Integration
# =============================================================================


def get_todoist_headers() -> dict[str, str]:
    """Get authorization headers for Todoist API."""
    token = os.getenv("TODOIST_ACCESS_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def fetch_todoist_completions(
    since: datetime, until: datetime, limit: int = 200
) -> list[dict]:
    """Fetch completed tasks from Todoist API within the given date range."""
    token = os.getenv("TODOIST_ACCESS_TOKEN")
    if not token:
        logger.warning("TODOIST_ACCESS_TOKEN not set - skipping Todoist fetch")
        return []

    since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
    until_str = until.strftime("%Y-%m-%dT%H:%M:%S")

    all_tasks: list[dict] = []
    cursor: str | None = None

    logger.info(f"Fetching Todoist completions from {since_str} to {until_str}")

    with httpx.Client(timeout=30.0) as client:
        while True:
            params: dict = {
                "since": since_str,
                "until": until_str,
                "limit": limit,
            }

            if cursor:
                params["cursor"] = cursor

            try:
                response = client.get(
                    COMPLETED_TASKS_ENDPOINT,
                    headers=get_todoist_headers(),
                    params=params,
                )

                if response.status_code != 200:
                    logger.error(
                        f"Failed to fetch Todoist tasks: {response.status_code}"
                    )
                    break

                data = response.json()
                items = data.get("items", [])
                all_tasks.extend(items)

                logger.debug(f"Fetched {len(items)} Todoist tasks")

                next_cursor = data.get("next_cursor")
                if not next_cursor:
                    break
                cursor = next_cursor

            except httpx.RequestError as e:
                logger.error(f"Todoist request error: {e}")
                break

    logger.info(f"Fetched {len(all_tasks)} total Todoist tasks")
    return all_tasks


# =============================================================================
# Data Formatting for AI
# =============================================================================


def format_updates_for_prompt(updates: list[dict]) -> str:
    """Format initiative/project updates for AI prompt."""
    if not updates:
        return "No updates this week."

    lines = []
    for u in updates:
        date = u.get("createdAt", "")[:10]
        body = u.get("body", "").strip()[:500]  # Truncate long updates
        health = u.get("health", "unknown")
        lines.append(f"- [{date}] (Health: {health})\n  {body}")

    return "\n".join(lines)


def format_project_updates_for_prompt(
    projects: list[dict],
) -> str:
    """Format project updates from all projects for AI prompt."""
    if not projects:
        return "No project updates this week."

    lines = []
    for proj in projects:
        proj_name = proj.get("name", "Unknown Project")
        updates = proj.get("updates_in_cycle", [])
        for u in updates:
            date = u.get("createdAt", "")[:10]
            body = u.get("body", "").strip()[:300]
            lines.append(f"- [{proj_name}] [{date}]\n  {body}")

    if not lines:
        return "No project updates this week."

    return "\n".join(lines)


def format_issues_for_prompt(issues: list[dict]) -> str:
    """Format completed issues for AI prompt."""
    if not issues:
        return "No issues completed this week."

    lines = []
    for i in issues:
        identifier = i.get("identifier", "?")
        title = i.get("title", "Untitled")
        lines.append(f"- {identifier}: {title}")

    return "\n".join(lines)


def format_todoist_for_prompt(tasks: list[dict]) -> str:
    """Format Todoist tasks for AI prompt."""
    if not tasks:
        return "No Todoist tasks completed this week."

    lines = []
    for t in tasks:
        content = t.get("content", "Unknown task")
        lines.append(f"- {content}")

    return "\n".join(lines)


def get_all_completed_issues_from_initiative(initiative: dict) -> list[dict]:
    """Extract all completed issues from an initiative's projects."""
    issues = []
    for proj in initiative.get("projects", []):
        issues.extend(proj.get("completed_issues", []))
    return issues


# =============================================================================
# AI Processing Functions
# =============================================================================


def parse_headlines_response(response_text: str) -> list[str]:
    """Parse JSON response containing headlines array."""
    try:
        data = json.loads(response_text)
        if isinstance(data, dict) and "headlines" in data:
            return data["headlines"]
        if isinstance(data, list):
            return data
        return []
    except json.JSONDecodeError:
        logger.warning(f"Could not parse AI response as JSON: {response_text[:100]}")
        return []


def generate_initiative_headlines(
    client: OpenAI,
    initiative_name: str,
    updates: list[dict],
    projects: list[dict],
    completed_issues: list[dict],
) -> dict:
    """Generate headlines for a single initiative using GPT-4o-mini."""
    updates_text = format_updates_for_prompt(updates)
    project_updates_text = format_project_updates_for_prompt(projects)
    issues_text = format_issues_for_prompt(completed_issues)

    prompt = INITIATIVE_HEADLINE_PROMPT.format(
        initiative_name=initiative_name,
        updates_text=updates_text,
        project_updates_text=project_updates_text,
        completed_issues_text=issues_text,
    )

    logger.debug(f"Generating headlines for initiative: {initiative_name}")

    try:
        response = client.chat.completions.create(
            model=GPT4O_MINI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            response_format={"type": "json_object"},
        )

        raw_response = response.choices[0].message.content
        parsed = parse_headlines_response(raw_response)

        return {
            "ai_input": {"prompt": prompt, "model": GPT4O_MINI_MODEL},
            "ai_response": {
                "raw_response": raw_response,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                },
            },
            "parsed_headlines": parsed,
        }
    except Exception as e:
        logger.error(f"Error generating headlines for {initiative_name}: {e}")
        return {
            "ai_input": {"prompt": prompt, "model": GPT4O_MINI_MODEL},
            "ai_response": {"raw_response": None, "error": str(e)},
            "parsed_headlines": [],
        }


def generate_other_headlines(
    client: OpenAI,
    other_issues: list[dict],
    todoist_tasks: list[dict],
) -> dict:
    """Generate headlines for items not related to active initiatives."""
    issues_text = format_issues_for_prompt(other_issues)
    todoist_text = format_todoist_for_prompt(todoist_tasks)

    prompt = OTHER_HEADLINES_PROMPT.format(
        issues_text=issues_text,
        todoist_text=todoist_text,
    )

    logger.debug("Generating other headlines")

    try:
        response = client.chat.completions.create(
            model=GPT4O_MINI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            response_format={"type": "json_object"},
        )

        raw_response = response.choices[0].message.content
        parsed = parse_headlines_response(raw_response)

        return {
            "other_completed_issues": {
                "raw_data": other_issues,
                "count": len(other_issues),
            },
            "todoist_completions": {
                "raw_data": todoist_tasks,
                "count": len(todoist_tasks),
            },
            "ai_input": {"prompt": prompt, "model": GPT4O_MINI_MODEL},
            "ai_response": {
                "raw_response": raw_response,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                },
            },
            "parsed_headlines": parsed,
        }
    except Exception as e:
        logger.error(f"Error generating other headlines: {e}")
        return {
            "other_completed_issues": {"raw_data": other_issues, "count": len(other_issues)},
            "todoist_completions": {"raw_data": todoist_tasks, "count": len(todoist_tasks)},
            "ai_input": {"prompt": prompt, "model": GPT4O_MINI_MODEL},
            "ai_response": {"raw_response": None, "error": str(e)},
            "parsed_headlines": [],
        }


def synthesize_final_markdown(
    client: OpenAI,
    initiative_headlines: list[dict],
    other_headlines: dict,
) -> dict:
    """Use GPT-4o to synthesize final markdown section."""
    # Prepare initiative headlines summary for synthesis
    init_summary = [
        {
            "initiative_name": ih["initiative_name"],
            "headlines": ih["parsed_headlines"],
        }
        for ih in initiative_headlines
        if ih.get("parsed_headlines")
    ]

    other_summary = other_headlines.get("parsed_headlines", [])

    prompt = SYNTHESIS_PROMPT.format(
        initiative_headlines_json=json.dumps(init_summary, indent=2),
        other_headlines_json=json.dumps(other_summary, indent=2),
    )

    logger.debug("Synthesizing final markdown with GPT-4o")

    try:
        response = client.chat.completions.create(
            model=GPT4O_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
        )

        raw_response = response.choices[0].message.content

        return {
            "ai_input": {"prompt": prompt, "model": GPT4O_MODEL},
            "ai_response": {
                "raw_response": raw_response,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                },
            },
        }
    except Exception as e:
        logger.error(f"Error synthesizing final markdown: {e}")
        return {
            "ai_input": {"prompt": prompt, "model": GPT4O_MODEL},
            "ai_response": {"raw_response": None, "error": str(e)},
        }


# =============================================================================
# Output
# =============================================================================


def save_results(
    output: dict, cycle_start: datetime, cycle_end: datetime
) -> Path:
    """Save results to timestamped JSON file."""
    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cycle_range = f"{cycle_start.strftime('%Y%m%d')}-{cycle_end.strftime('%Y%m%d')}"
    filename = f"{timestamp}_latest_headlines_{cycle_range}.json"
    file_path = data_dir / filename

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    logger.info(f"Saved results to: {file_path}")
    return file_path


def format_date_range(cycle_start: datetime, cycle_end: datetime) -> str:
    """Format the date range string for display."""
    start_str = f"{cycle_start.strftime('%b')}. {cycle_start.strftime('%d')}"
    end_str = f"{cycle_end.strftime('%b')}. {cycle_end.strftime('%d')}, {cycle_end.strftime('%Y')}"
    return f"({start_str} - {end_str})"


# =============================================================================
# Main
# =============================================================================


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Generate Latest Headlines from Linear cycle data"
    )
    parser.add_argument(
        "--previous",
        action="store_true",
        help="Process previous cycle instead of current",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    # Determine cycle
    tz = pytz.timezone(os.getenv("SYSTEM_TIMEZONE", "US/Pacific"))
    cycle_type = "previous" if args.previous else "current"
    cycle_start, cycle_end = get_cycle_bounds(cycle_type, tz)
    date_range = format_date_range(cycle_start, cycle_end)

    logger.info(f"Processing {cycle_type} cycle: {date_range}")
    logger.info(f"  Start: {cycle_start.date()}")
    logger.info(f"  End:   {cycle_end.date()}")

    # Initialize OpenAI client
    client = get_openai_client()

    # ==========================================================================
    # 1. Fetch Active Initiatives
    # ==========================================================================
    logger.info("Fetching active initiatives...")
    all_initiatives = fetch_initiatives(include_archived=False)
    active_initiatives = [i for i in all_initiatives if i.get("status") == "Active"]
    logger.info(f"Found {len(active_initiatives)} active initiatives")

    # Track project IDs for "other" calculation
    active_project_ids = set()

    # ==========================================================================
    # 2. Process Each Initiative
    # ==========================================================================
    initiative_headlines = []

    for i, init in enumerate(active_initiatives):
        init_name = init.get("name", "Unknown")
        logger.info(
            f"Processing initiative {i + 1}/{len(active_initiatives)}: {init_name}"
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
            "initiative_id": init["id"],
            "initiative_name": init_name,
            "raw_data": {
                "updates_in_cycle": enriched.get("updates_in_cycle", []),
                "projects": [
                    {
                        "name": p.get("name"),
                        "updates_in_cycle": p.get("updates_in_cycle", []),
                        "completed_issues": p.get("completed_issues", []),
                    }
                    for p in enriched.get("projects", [])
                ],
            },
            **headlines_result,
        })

        logger.info(
            f"  Generated {len(headlines_result.get('parsed_headlines', []))} headlines"
        )

    # ==========================================================================
    # 3. Get "Other" Completed Issues
    # ==========================================================================
    logger.info("Fetching other completed issues...")
    all_completed = fetch_all_completed_issues_in_range(cycle_start, cycle_end)
    other_completed = [
        i
        for i in all_completed
        if i.get("project", {}).get("id") not in active_project_ids
    ]
    logger.info(
        f"Found {len(all_completed)} total completed issues, "
        f"{len(other_completed)} outside active initiatives"
    )

    # ==========================================================================
    # 4. Get Todoist Completions
    # ==========================================================================
    logger.info("Fetching Todoist completions...")
    todoist_tasks = fetch_todoist_completions(cycle_start, cycle_end)

    # ==========================================================================
    # 5. Generate "Other" Headlines
    # ==========================================================================
    logger.info("Generating other headlines...")
    other_headlines = generate_other_headlines(client, other_completed, todoist_tasks)
    logger.info(
        f"Generated {len(other_headlines.get('parsed_headlines', []))} other headlines"
    )

    # ==========================================================================
    # 6. Final Synthesis with GPT-4o
    # ==========================================================================
    logger.info("Synthesizing final markdown with GPT-4o...")
    synthesis_result = synthesize_final_markdown(
        client, initiative_headlines, other_headlines
    )

    final_markdown = synthesis_result.get("ai_response", {}).get("raw_response", "")

    # ==========================================================================
    # 7. Build Output
    # ==========================================================================
    output = {
        "metadata": {
            "generated_at": datetime.now().isoformat(),
            "cycle_type": cycle_type,
            "cycle_start": cycle_start.strftime("%Y-%m-%d"),
            "cycle_end": cycle_end.strftime("%Y-%m-%d"),
            "cycle_range": date_range,
            "active_initiative_count": len(active_initiatives),
            "models_used": {
                "incremental": GPT4O_MINI_MODEL,
                "synthesis": GPT4O_MODEL,
            },
        },
        "initiative_headlines": initiative_headlines,
        "other_headlines": other_headlines,
        "final_synthesis": synthesis_result,
        "final_markdown_section": final_markdown,
    }

    # ==========================================================================
    # 8. Save Results
    # ==========================================================================
    file_path = save_results(output, cycle_start, cycle_end)

    # Print final markdown
    print("\n" + "=" * 60)
    print("FINAL HEADLINES")
    print("=" * 60)
    print(final_markdown)
    print("=" * 60)
    print(f"\nResults saved to: {file_path}")


if __name__ == "__main__":
    main()
