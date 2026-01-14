#!/usr/bin/env python3
"""Generate Next Cycle Headlines - Fetch initiative updates from Linear and generate projected headlines.

Usage:
    python app/scripts/generate_next_cycle_headlines.py                    # Generate headlines
    python app/scripts/generate_next_cycle_headlines.py --dry-run          # Show what would be sent to LLM
    python app/scripts/generate_next_cycle_headlines.py --model gpt-4o     # Use specific model

Requires:
    - LINEAR_API_KEY in .env file
    - OPENAI_API_KEY in .env file (for non-dry-run)

Output: app/tests/data/YYYYMMDD_HHMMSS_next_cycle_headlines.json
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Add app directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.linear.sync_utils import (
    fetch_initiatives,
    fetch_initiative_updates,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

DEFAULT_MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = """You are an assistant that generates concise, action-oriented headlines from weekly initiative updates.

Given an initiative name and the "This Week" section from an update, generate a single compelling headline that captures what will be accomplished this week.

Rules:
- Generate exactly ONE headline
- Maximum 12 words
- Use action-oriented language (e.g., "Ship", "Complete", "Launch", "Finalize")
- Focus on the most impactful or significant item
- Be specific, not generic
- Write as if it's a news headline announcing the accomplishment"""

USER_PROMPT_TEMPLATE = """Initiative: {initiative_name}

This Week's Plan:
{this_week_section}

Generate a single projected headline for this week:"""


# =============================================================================
# Update Parsing
# =============================================================================


def extract_this_week_section(body: str) -> str | None:
    """Extract the 'This Week' section from an update body.

    Handles various formats:
    - "This week:" / "This Week:"
    - "This Week:\n* item1\n* item2"
    - Stops at next section header or end of text
    """
    if not body:
        return None

    # Pattern to match "This week:" or "This Week:" (case insensitive)
    pattern = r"(?:^|\n)(?:This [Ww]eek|this week)\s*:?\s*\n?"

    match = re.search(pattern, body, re.IGNORECASE)
    if not match:
        return None

    # Get content after the header
    start_pos = match.end()
    remaining = body[start_pos:]

    # Find the next section header (e.g., "Up next:", "What did you get done", etc.)
    next_section_pattern = r"\n(?:Up [Nn]ext|What did you|Yesterday|Tomorrow|Blockers?|Notes?)\s*:?"
    next_match = re.search(next_section_pattern, remaining, re.IGNORECASE)

    if next_match:
        content = remaining[: next_match.start()]
    else:
        content = remaining

    # Clean up the content
    content = content.strip()

    return content if content else None


# =============================================================================
# Linear API Functions
# =============================================================================


def fetch_active_initiatives_with_updates() -> list[dict]:
    """Fetch all active initiatives and their latest updates from Linear.

    Returns:
        List of dicts with initiative info and latest update
    """
    logger.info("Fetching active initiatives from Linear...")

    # Get all initiatives
    all_initiatives = fetch_initiatives(include_archived=False)

    # Filter to active only
    active_initiatives = [
        i for i in all_initiatives if i.get("status") == "Active"
    ]

    logger.info(f"Found {len(active_initiatives)} active initiatives")

    results = []
    for init in active_initiatives:
        initiative_id = init["id"]
        initiative_name = init.get("name", "Unknown")

        logger.debug(f"Fetching updates for: {initiative_name}")

        # Fetch updates for this initiative
        updates = fetch_initiative_updates(initiative_id)

        if updates:
            # Get the latest update (sorted by createdAt)
            latest_update = max(updates, key=lambda u: u.get("createdAt", ""))
            results.append({
                "initiative_id": initiative_id,
                "initiative_name": initiative_name,
                "initiative_url": init.get("url"),
                "initiative_status": init.get("status"),
                "initiative_health": init.get("health"),
                "latest_update": latest_update,
            })
        else:
            logger.debug(f"  No updates found for {initiative_name}")

    logger.info(f"Found {len(results)} initiatives with updates")
    return results


# =============================================================================
# LLM Integration
# =============================================================================


def generate_headline_with_llm(
    client,
    initiative_name: str,
    this_week_section: str,
    model: str,
) -> dict:
    """Generate a headline using OpenAI.

    Returns:
        Dict with llm_output containing projected_headline and raw_response
    """
    user_prompt = USER_PROMPT_TEMPLATE.format(
        initiative_name=initiative_name,
        this_week_section=this_week_section,
    )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=100,
    )

    raw_response = response.choices[0].message.content.strip()

    # Clean up the headline (remove quotes, leading dashes, etc.)
    headline = raw_response.strip('"\'').lstrip("- ").strip()

    return {
        "projected_headline": headline,
        "raw_response": raw_response,
        "model": model,
        "finish_reason": response.choices[0].finish_reason,
    }


# =============================================================================
# Output Generation
# =============================================================================


def build_output(
    initiatives_data: list[dict],
    model: str,
    dry_run: bool = False,
) -> dict:
    """Build the full output JSON structure.

    Args:
        initiatives_data: List of initiative data with updates
        model: LLM model name
        dry_run: If True, skip LLM calls

    Returns:
        Complete output dict with all headlines
    """
    # Import OpenAI only if not dry run
    client = None
    if not dry_run:
        try:
            from openai import OpenAI
            client = OpenAI()
        except ImportError:
            logger.error("OpenAI package not installed. Run: pip install openai")
            sys.exit(1)

    headlines = []

    for item in initiatives_data:
        initiative_name = item["initiative_name"]
        update = item["latest_update"]
        raw_body = update.get("body", "")

        # Extract This Week section
        this_week_section = extract_this_week_section(raw_body)

        headline_entry = {
            "initiative_id": item["initiative_id"],
            "initiative_name": initiative_name,
            "initiative_url": item.get("initiative_url"),
            "initiative_health": item.get("initiative_health"),
            "update_id": update.get("id"),
            "update_url": update.get("url"),
            "update_created_at": update.get("createdAt"),
            "update_health": update.get("health"),
            "llm_input": {
                "raw_update_body": raw_body,
                "this_week_section": this_week_section,
            },
            "llm_output": None,
        }

        if this_week_section:
            if dry_run:
                logger.info(f"\n{'='*60}")
                logger.info(f"Initiative: {initiative_name}")
                logger.info(f"This Week Section:\n{this_week_section}")
                logger.info("(dry-run: would call LLM here)")
                headline_entry["llm_output"] = {
                    "projected_headline": "(dry-run)",
                    "raw_response": "(dry-run)",
                    "model": model,
                    "skipped": True,
                }
            else:
                logger.info(f"Generating headline for: {initiative_name}")
                llm_result = generate_headline_with_llm(
                    client,
                    initiative_name,
                    this_week_section,
                    model,
                )
                headline_entry["llm_output"] = llm_result
                logger.info(f"  -> {llm_result['projected_headline']}")
        else:
            logger.debug(f"No 'This Week' section found for: {initiative_name}")
            headline_entry["llm_output"] = {
                "projected_headline": None,
                "raw_response": None,
                "model": model,
                "skipped": True,
                "skip_reason": "No 'This Week' section found in update",
            }

        headlines.append(headline_entry)

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "model": model,
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt_template": USER_PROMPT_TEMPLATE,
        "total_initiatives": len(initiatives_data),
        "headlines_generated": sum(
            1 for h in headlines
            if h.get("llm_output", {}).get("projected_headline")
            and not h.get("llm_output", {}).get("skipped")
        ),
        "headlines": headlines,
    }


def save_output(output: dict, dry_run: bool = False) -> Path:
    """Save output to timestamped JSON file."""
    data_dir = Path(__file__).parent.parent / "tests" / "data"
    # Handle symlinks and regular directories
    if not data_dir.exists() and not data_dir.is_symlink():
        data_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "_dry_run" if dry_run else ""
    output_file = data_dir / f"{timestamp}_next_cycle_headlines{suffix}.json"

    with open(output_file, "w") as f:
        json.dump(output, f, indent=2)

    logger.info(f"Saved output to: {output_file}")
    return output_file


# =============================================================================
# Main Entry Point
# =============================================================================


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Generate projected headlines from Linear initiative updates"
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be sent to LLM without making API calls",
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

    # Fetch data from Linear
    initiatives_data = fetch_active_initiatives_with_updates()

    if not initiatives_data:
        logger.warning("No active initiatives with updates found")
        return

    # Build output with LLM headlines
    output = build_output(
        initiatives_data,
        model=args.model,
        dry_run=args.dry_run,
    )

    # Save output
    output_file = save_output(output, dry_run=args.dry_run)

    # Print summary
    print(f"\nGenerated {output['headlines_generated']} headlines")
    print(f"Output saved to: {output_file}")


if __name__ == "__main__":
    main()
