#!/usr/bin/env python3
"""Post a Daily Initiative Update on the active Linear initiative.

The cron runs at 04:00 America/Los_Angeles, gathers the last 3 days of
Daily Action notes from Obsidian/Dropbox plus the last 3 initiative
updates on the active initiative, asks an LLM to synthesize a
markdown summary (Wins / In-progress / Follow-ups), and posts it as a
new initiative update. The existing Linear webhook handler mirrors the
new update into today's Daily Action note automatically.

Usage:
    python -m scripts.send_daily_initiative_update
    python -m scripts.send_daily_initiative_update --dry-run
    python -m scripts.send_daily_initiative_update --output update.md
    python -m scripts.send_daily_initiative_update --debug
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import dropbox
from dotenv import load_dotenv
from openai import OpenAI

# Add app directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import SYSTEM_TZ
from scripts.linear.sync_utils import (
    create_initiative_update,
    fetch_initiative_updates,
    fetch_initiatives,
    get_dropbox_client,
)

logger = logging.getLogger(__name__)

# Deliberately hardcoded so enable/disable changes are tracked in version control.
DAILY_INITIATIVE_UPDATE_ENABLED = True

LOOKBACK_DAYS = 3
RECENT_UPDATES_CONTEXT = 3
ACTIVE_STATUS_NAME = "Active"
SUMMARY_MODEL = "gpt-4o-mini"

SUMMARY_SYSTEM_PROMPT = """You write daily initiative updates for a personal productivity system.

Goals:
- Reinforce real wins from YESTERDAY only — sourced from yesterday's daily action note.
- Surface in-progress work clearly so progress is visible — draw from all three daily action notes.
- Flag anything that looks incomplete or carried-over so it can be followed up — draw from all three daily action notes.

Constraints:
- Output GitHub-flavored markdown only — no preamble, no closing remarks.
- Use exactly these three section headers in this order: `## Previous Day's Wins`, `## In-progress`, `## Follow-ups`.
- `## Previous Day's Wins` reflects only YESTERDAY's completed work (the most recent daily action note, dated as specified in the user message). Do NOT carry over wins from older daily action notes, and do NOT copy wins from prior initiative updates — those prior updates describe earlier days and their wins belong to those days, not this one.
- The prior initiative updates are provided ONLY as deduplication context for the `## In-progress` and `## Follow-ups` sections (so you don't restate items already reported there). They are never a source for `## Previous Day's Wins`.
- `## In-progress` and `## Follow-ups` may draw from any of the three daily action notes, but should avoid repeating items already covered in the prior initiative updates.
- Each section is a bulleted list. If a section has nothing real to say, write a single bullet `- (nothing notable)`.
- Keep bullets short (one line each). Aim for 3-6 bullets per section total across the post.
- First person, plain voice. No hype, no emojis."""

SUMMARY_USER_PROMPT_TEMPLATE = """Generate today's initiative update for {today_local}.

Yesterday is {yesterday_local}. The `## Previous Day's Wins` section must reflect only that day's completed work, sourced from the daily action note dated {yesterday_local}.

Do not source wins from older daily action notes (they belong to earlier days) and do not source wins from prior initiative updates (their wins were yesterday-of-when-they-were-written, not yesterday-of-today). The prior initiative updates below are deduplication context for In-progress and Follow-ups only.

The daily action notes capture raw activity; they may be incomplete or noisy. Synthesize a coherent picture rather than copying lines verbatim.

=== Last {recent_updates_count} initiative updates (oldest first, for dedup context only) ===
{recent_updates_block}

=== Daily Action notes (oldest first) ===
{daily_action_block}
"""


# =============================================================================
# Active-initiative resolution + idempotency
# =============================================================================


def _resolve_active_initiative(initiatives: list[dict]) -> dict:
    """Return the single initiative whose status is Active.

    Aborts if zero or multiple initiatives are active.
    """
    active = [i for i in initiatives if i.get("status") == ACTIVE_STATUS_NAME]
    if len(active) == 0:
        raise RuntimeError("No active initiative found.")
    if len(active) > 1:
        names = ", ".join(i.get("name", "?") for i in active)
        raise RuntimeError(
            f"Expected exactly one active initiative, found {len(active)}: {names}"
        )
    return active[0]


def _already_posted_today(updates: list[dict], now_local: datetime) -> bool:
    """Return True if any update was created on or after today (local tz).

    `updates` is the result of `fetch_initiative_updates` — each item has
    an ISO-8601 `createdAt` in UTC. We compare against start-of-today in
    SYSTEM_TZ.
    """
    today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    for u in updates:
        created_at_raw = u.get("createdAt")
        if not created_at_raw:
            continue
        try:
            created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Skipping update with unparsable createdAt=%r", created_at_raw)
            continue
        created_local = created_at.astimezone(SYSTEM_TZ)
        if created_local >= today_start_local:
            return True
    return False


# =============================================================================
# Daily Action notes loader
# =============================================================================


def _find_daily_folder(dbx: dropbox.Dropbox, vault_path: str) -> str:
    """Find folder ending with '_Daily' in the vault."""
    result = dbx.files_list_folder(vault_path)
    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Daily"):
                return entry.path_lower
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)
    raise FileNotFoundError("Could not find '_Daily' folder in Dropbox")


def _find_daily_action_folder(dbx: dropbox.Dropbox, daily_folder_path: str) -> str:
    """Find folder ending with '_Daily-Action' in the daily folder."""
    result = dbx.files_list_folder(daily_folder_path)
    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Daily-Action"):
                return entry.path_lower
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)
    raise FileNotFoundError("Could not find '_Daily-Action' folder in Dropbox")


def _download_da_note(dbx: dropbox.Dropbox, folder_path: str, date_str: str) -> str | None:
    """Download `DA YYYY-MM-DD.md` content. Returns None if not present."""
    path = f"{folder_path}/DA {date_str}.md"
    try:
        _, response = dbx.files_download(path)
        return response.content.decode("utf-8")
    except dropbox.exceptions.ApiError:
        logger.info("Daily Action note not found: %s", path)
        return None


def load_recent_daily_action_notes(
    now_local: datetime, lookback_days: int = LOOKBACK_DAYS
) -> list[tuple[str, str | None]]:
    """Load the last `lookback_days` Daily Action notes from Obsidian.

    Returns list of (date_str, content_or_none) tuples, oldest first.
    Notes for missing dates are returned with content=None.
    """
    vault_path = os.getenv("DROPBOX_OBSIDIAN_VAULT_PATH")
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

    dbx = get_dropbox_client()
    daily_folder = _find_daily_folder(dbx, vault_path)
    daily_action_folder = _find_daily_action_folder(dbx, daily_folder)

    today_local = now_local.date()
    dates = [today_local - timedelta(days=offset) for offset in range(lookback_days, 0, -1)]
    notes: list[tuple[str, str | None]] = []
    for d in dates:
        date_str = d.strftime("%Y-%m-%d")
        notes.append((date_str, _download_da_note(dbx, daily_action_folder, date_str)))
    return notes


# =============================================================================
# Summarizer
# =============================================================================


def _get_openai_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY not set")
    return OpenAI(api_key=api_key)


def _format_recent_updates_block(updates: list[dict]) -> str:
    if not updates:
        return "(no prior initiative updates)"
    lines: list[str] = []
    for u in updates:
        created = u.get("createdAt", "?")
        body = (u.get("body") or "").strip() or "(empty body)"
        lines.append(f"--- update created {created} ---\n{body}")
    return "\n\n".join(lines)


def _format_daily_action_block(notes: list[tuple[str, str | None]]) -> str:
    if not notes:
        return "(no daily action notes available)"
    parts: list[str] = []
    for date_str, content in notes:
        if content is None:
            parts.append(f"--- DA {date_str} (no note) ---")
        else:
            parts.append(f"--- DA {date_str} ---\n{content.strip()}")
    return "\n\n".join(parts)


def generate_update_body(
    daily_action_notes: list[tuple[str, str | None]],
    recent_updates: list[dict],
    now_local: datetime,
) -> str:
    """Call OpenAI to produce the markdown body for the new initiative update."""
    client = _get_openai_client()

    yesterday_local = (now_local - timedelta(days=1)).strftime("%A, %B %d, %Y")
    user_prompt = SUMMARY_USER_PROMPT_TEMPLATE.format(
        today_local=now_local.strftime("%A, %B %d, %Y"),
        yesterday_local=yesterday_local,
        recent_updates_count=len(recent_updates),
        recent_updates_block=_format_recent_updates_block(recent_updates),
        daily_action_block=_format_daily_action_block(daily_action_notes),
    )

    response = client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
    )

    content = response.choices[0].message.content or ""
    return content.strip()


# =============================================================================
# Orchestrator
# =============================================================================


def run_daily_initiative_update(
    dry_run: bool = False,
    output: str | None = None,
    now_local: datetime | None = None,
) -> bool:
    """Generate and post the daily initiative update.

    Returns True on success or intentional skip; False on failure.
    """
    load_dotenv()

    if not DAILY_INITIATIVE_UPDATE_ENABLED:
        logger.info("DAILY_INITIATIVE_UPDATE_ENABLED is hardcoded to false; skipping.")
        return True

    if not os.getenv("LINEAR_API_KEY"):
        logger.error("LINEAR_API_KEY environment variable not set")
        return False

    now_local = (now_local or datetime.now(SYSTEM_TZ)).astimezone(SYSTEM_TZ)

    try:
        initiatives = fetch_initiatives()
        active = _resolve_active_initiative(initiatives)
        initiative_id = active["id"]
        initiative_name = active.get("name", "(unknown)")
        logger.info(
            "Active initiative resolved: %s (%s)", initiative_name, initiative_id
        )

        # Fetch updates once, use for both idempotency and context.
        all_updates = fetch_initiative_updates(initiative_id)

        if _already_posted_today(all_updates, now_local):
            logger.info(
                "An initiative update already exists for today (%s); skipping.",
                now_local.strftime("%Y-%m-%d"),
            )
            return True

        # Newest first from API; sort to be safe, then take N newest.
        sorted_updates = sorted(
            (u for u in all_updates if u.get("createdAt")),
            key=lambda u: u["createdAt"],
            reverse=True,
        )
        recent_updates = list(reversed(sorted_updates[:RECENT_UPDATES_CONTEXT]))
        logger.info("Pulled %d recent initiative updates for context", len(recent_updates))

        daily_notes = load_recent_daily_action_notes(now_local)
        present = [d for d, c in daily_notes if c is not None]
        logger.info(
            "Loaded daily action notes: %d/%d present (%s)",
            len(present),
            len(daily_notes),
            ", ".join(present) or "none",
        )

        if not present and not recent_updates:
            logger.warning(
                "No daily action notes and no prior updates; nothing to summarize. Skipping."
            )
            return True

        body = generate_update_body(daily_notes, recent_updates, now_local)
        if not body:
            logger.error("LLM returned empty body; aborting.")
            return False

        logger.info("Generated body (%d chars)", len(body))

        if output:
            output_path = Path(output)
            output_path.write_text(body, encoding="utf-8")
            logger.info("Wrote body to %s", output_path)

        if dry_run:
            logger.info("Dry run; not posting to Linear.")
            print(body)
            return True

        result = create_initiative_update(initiative_id=initiative_id, body=body)
        logger.info(
            "Posted initiative update: id=%s url=%s",
            result.get("id"),
            result.get("url"),
        )
        return True
    except Exception:
        logger.exception("Daily initiative update failed")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Post the daily initiative update to the active Linear initiative."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate the body but do not post to Linear.",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Optional path to write the generated markdown body.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    success = run_daily_initiative_update(
        dry_run=args.dry_run,
        output=args.output,
    )
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
