#!/usr/bin/env python3
"""Post a Daily Initiative Update on the main-thread Linear initiative.

The cron runs at 04:00 America/Los_Angeles, gathers the last 3 days of
Daily Action notes from Obsidian/Dropbox plus the last 3 initiative
updates on the initiative labeled `main-thread`, asks an LLM to
synthesize a markdown summary (Wins / In-progress / Follow-ups), and
posts it as a new initiative update. The existing Linear webhook
handler mirrors the new update into today's Daily Action note
automatically.

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
import re
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
MAIN_THREAD_LABEL = "main-thread"
SUMMARY_MODEL = "gpt-4o-mini"

SUMMARY_SYSTEM_PROMPT = """You write daily initiative updates for a personal productivity system.

Goals:
- Reinforce real wins from YESTERDAY only — sourced from yesterday's daily action note.
- Surface in-progress work clearly so trajectory is visible — synthesize across both the daily action notes AND the in-progress lines from prior initiative updates.
- Flag anything still incomplete or carried-over so it can be followed up — synthesize across both the daily action notes AND the follow-up lines from prior initiative updates.

Constraints:
- Output GitHub-flavored markdown only — no preamble, no closing remarks.
- Use exactly these three section headers in this order: `## Previous Day's Wins`, `## In-progress`, `## Follow-ups`.
- `## Previous Day's Wins` reflects only YESTERDAY's completed work. Sweep ALL of the following sources and MERGE them — this is additive, not a fallback hierarchy. Capture every distinct win you find; do not impose a cap on bullet count.
  - User-authored bullets directly under `Win 1:`, `Win 2:`, `Win 3:` (or similar `Win N:` labels) in yesterday's daily action note. When present, these are the highest-confidence wins and should appear first in the section.
  - Wins-shaped content in yesterday's journal entry — accomplishments, things-that-went-well, completed work, breakthroughs, meaningful experiences the user wrote about in prose.
  - Concrete completions in yesterday's daily action note: `### Todoist Completed Tasks:`, `### Linear Issues Touched:` with done-status, `### Manus Tasks:` with done-status, and any other clearly-completed items mentioned in the user's prose anywhere in that day's note.
  - De-duplicate when sources overlap (e.g. a Todoist completion that the user also wrote about in the journal — surface once, prefer the user's wording).
  - QUALITY BAR: include only items that represent meaningful completed work or genuine experiences. Skip trivial/admin signal ("checked email", "opened a tab", routine pings, webhook test events). Do not fabricate — every bullet must trace back to something concrete in the sources.
  - Do NOT carry over wins from older daily action notes, and do NOT copy wins from prior initiative updates — their wins describe earlier days. (Their wins sections have already been stripped from the dedup context for safety.)
- `## In-progress` and `## Follow-ups` are sourced EXCLUSIVELY from the three daily action notes — never from prior initiative updates. Prior update bodies have been content-redacted in the context block; their existence is shown only so you know an update was already posted on those days. Rules:
  - **Source from current evidence only.** An item belongs in In-progress / Follow-ups only if you can point to ACTIVE evidence in the past three DA notes (a completion, a status change, a new sub-task, a deliberate mention in the user's prose, a Linear issue touched, a Todoist task completed).
  - Mere persistence in auto-synced sections like `### Manus Tasks:` does NOT count as evidence — Manus tasks linger in the DA note long after they're no longer being worked on. Treat unchanged Manus entries as background noise, not active work.
  - Prefer recent evidence: items active in yesterday's DA note are more current than items only visible in older DA notes.
  - When in doubt about whether an item is current, DROP it. It's better to be sparse and accurate than complete and stale.
- Each section is a bulleted list. If a section has nothing real to say, write a single bullet `- (nothing notable)`.
- Keep bullets short (one line each). For `## Previous Day's Wins` there is no cap — include every distinct meaningful win you find. For `## In-progress` and `## Follow-ups` keep it tight (around 3-6 bullets) since those benefit from being curated rather than exhaustive.
- First person, plain voice. No hype, no emojis."""

SUMMARY_USER_PROMPT_TEMPLATE = """Generate today's initiative update for {today_local}. Yesterday is {yesterday_local}.

# Sourcing rules — strictly enforced

`## Previous Day's Wins` is sourced EXCLUSIVELY from the two blocks below labelled "YESTERDAY DA NOTE" and "YESTERDAY JOURNAL". Sweep both additively — no cap on bullet count — and merge: (a) user-authored bullets under `Win 1:` / `Win 2:` / `Win 3:` (or similar `Win N:` labels) in the yesterday DA note, (b) wins-shaped content in the yesterday journal (accomplishments, things that went well, completed work, breakthroughs, meaningful experiences the user wrote about), and (c) concrete completions elsewhere in the yesterday DA note — completed Todoist tasks, Linear issues moved to done, Manus tasks moved to done, clearly-completed items in the user's prose. De-duplicate where sources overlap (prefer the user's wording). Quality bar: only meaningful items, skip trivial admin signal, never fabricate. If yesterday is genuinely sparse, write `- (nothing notable)` — it is better to honestly report a quiet day than to reach into older notes. DO NOT pull wins from the OLDER DA NOTES block or from the PRIOR INITIATIVE UPDATES block.

`## In-progress` and `## Follow-ups` are sourced from the YESTERDAY DA NOTE and the OLDER DA NOTES blocks. Prior updates' bodies have been redacted; their existence is visible only so you know an update was posted. Each item must be backed by active evidence in the DA notes (a completion, a status change, a new sub-task, deliberate mention in prose, a Linear issue touched, a Todoist task completed). Mere persistence in auto-synced Manus Tasks is NOT evidence — drop those. Prefer recent evidence; when in doubt, drop.

# Context

=== PRIOR INITIATIVE UPDATES — last {recent_updates_count}, oldest first; bodies redacted, content NOT a valid source ===
{recent_updates_block}

=== YESTERDAY JOURNAL — only valid source for Previous Day's Wins (alongside Yesterday DA Note) ===
{journal_block}

=== YESTERDAY DA NOTE — only valid source for Previous Day's Wins (alongside Yesterday Journal); also a source for In-progress/Follow-ups ===
{yesterday_da_note_block}

=== OLDER DA NOTES — context for In-progress/Follow-ups ONLY; NOT a valid source for Previous Day's Wins ===
{older_da_notes_block}
"""


# =============================================================================
# Main-thread initiative resolution + idempotency
# =============================================================================


def _initiative_label_names(initiative: dict) -> list[str]:
    """Return lowercased label names from a fetch_initiatives node."""
    nodes = (initiative.get("labels") or {}).get("nodes") or []
    return [str(n.get("name", "")).lower() for n in nodes if n.get("name")]


def _resolve_main_thread_initiative(initiatives: list[dict]) -> dict:
    """Return the single Active initiative carrying the main-thread label.

    Multiple Active initiatives are allowed; the update target is chosen by
    the `main-thread` label. Aborts if zero or multiple Active initiatives
    carry that label.
    """
    active = [i for i in initiatives if i.get("status") == ACTIVE_STATUS_NAME]
    matches = [i for i in active if MAIN_THREAD_LABEL in _initiative_label_names(i)]
    if len(matches) == 0:
        raise RuntimeError(
            f"No active initiative carries the '{MAIN_THREAD_LABEL}' label "
            f"(active count: {len(active)})."
        )
    if len(matches) > 1:
        names = ", ".join(i.get("name", "?") for i in matches)
        raise RuntimeError(
            f"Expected exactly one '{MAIN_THREAD_LABEL}' initiative among "
            f"active initiatives, found {len(matches)}: {names}"
        )
    return matches[0]


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


def _find_journal_folder(dbx: dropbox.Dropbox, daily_folder_path: str) -> str:
    """Find folder ending with '_Journal' in the daily folder."""
    result = dbx.files_list_folder(daily_folder_path)
    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata) and entry.name.endswith("_Journal"):
                return entry.path_lower
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)
    raise FileNotFoundError("Could not find '_Journal' folder in Dropbox")


def _journal_filename_for_date(d) -> str:
    """Build the journal filename for a date. Format: `{Mmm} {D}, {YYYY}.md` (no zero-padded day)."""
    return f"{d.strftime('%b')} {d.day}, {d.strftime('%Y')}.md"


def _download_da_note(dbx: dropbox.Dropbox, folder_path: str, date_str: str) -> str | None:
    """Download `DA YYYY-MM-DD.md` content. Returns None if not present."""
    path = f"{folder_path}/DA {date_str}.md"
    try:
        _, response = dbx.files_download(path)
        return response.content.decode("utf-8")
    except dropbox.exceptions.ApiError:
        logger.info("Daily Action note not found: %s", path)
        return None


# The Linear webhook mirrors every initiative update into the current day's DA
# note under `### Initiative Updates:`. If we feed that mirrored content back to
# the LLM, it will treat our own prior cron output as fresh activity and loop
# yesterday's "Previous Day's Wins" forward forever. Strip the section before
# summarizing.
_INITIATIVE_UPDATES_SECTION_RE = re.compile(
    r"^###\s+Initiative Updates:\s*$.*?(?=^###\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _strip_initiative_updates_section(content: str) -> str:
    """Remove the `### Initiative Updates:` section (and its body) from a DA note."""
    return _INITIATIVE_UPDATES_SECTION_RE.sub("", content)


# Defensive second strip: in DA notes that were written by older / buggy upsert
# code, the H2 subsections of a mirrored update body (`## Previous Day's Wins`,
# `## In-progress`, `## Follow-ups`) can survive outside the parent `### Initiative
# Updates:` section — e.g. orphaned under `### Completed Tasks on Todoist:` after
# a partial wipe. Those H2 headers never appear in the user's hand-written
# template (which uses inline `Win 1:` labels, not H2s), so it is safe to strip
# any of them wherever they appear in a DA note before feeding to the LLM.
_DA_MIRRORED_UPDATE_H2_RE = re.compile(
    r"^##\s+(?:Previous Day's\s+Wins|Wins|In-progress|Follow-ups)\s*$.*?(?=^#{1,6}\s|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)


def _strip_mirrored_update_fragments(content: str) -> str:
    """Remove H2 wins/in-progress/follow-ups subsections from anywhere in a DA note.

    Defends against orphaned fragments left behind by older upsert bugs. Safe
    because these H2 headers are not part of the user's template.
    """
    return _DA_MIRRORED_UPDATE_H2_RE.sub("", content)


def _sanitize_da_note_for_llm(content: str) -> str:
    """Apply all DA-note strips before feeding the note to the LLM."""
    cleaned = _strip_initiative_updates_section(content)
    cleaned = _strip_mirrored_update_fragments(cleaned)
    return cleaned


# Prior initiative updates are passed to the LLM only as "an update was posted"
# metadata. ALL of their H2 sections (Previous Day's Wins, Wins, In-progress,
# Follow-ups) are stripped before display, because:
#
# 1. Their wins describe earlier days and must not source today's wins.
# 2. Their In-progress / Follow-ups self-reinforce indefinitely if passed
#    through — once an item is in a prior update, the model treats it as
#    "real trajectory" and carries it forward into the next update, and the
#    next, and the next. The DA notes are the only reliable source of truth
#    for what is currently active; the prior updates' body is just historical
#    artifact.
_PRIOR_UPDATE_H2_SECTIONS_RE = re.compile(
    r"^##\s+(?:Previous Day's\s+Wins|Wins|In-progress|Follow-ups)\s*$.*?(?=^#{1,6}\s|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)


def _strip_h2_sections_from_update_body(body: str) -> str:
    """Strip Wins / In-progress / Follow-ups H2 sections from a prior update body.

    Prior updates' bodies are effectively just a structural skeleton after this
    runs — anything outside the four standard H2s survives, but in practice the
    body becomes empty. That's intentional: the prior updates contribute as
    "an update was posted" signal, not as content.
    """
    return _PRIOR_UPDATE_H2_SECTIONS_RE.sub("", body)


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


def load_yesterday_journal(now_local: datetime) -> tuple[str, str | None]:
    """Load yesterday's journal entry from Obsidian.

    Returns (filename, content_or_none). Missing journal returns content=None.
    """
    vault_path = os.getenv("DROPBOX_OBSIDIAN_VAULT_PATH")
    if not vault_path:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")

    dbx = get_dropbox_client()
    daily_folder = _find_daily_folder(dbx, vault_path)
    journal_folder = _find_journal_folder(dbx, daily_folder)

    yesterday = (now_local - timedelta(days=1)).date()
    filename = _journal_filename_for_date(yesterday)
    path = f"{journal_folder}/{filename}"
    try:
        _, response = dbx.files_download(path)
        return filename, response.content.decode("utf-8")
    except dropbox.exceptions.ApiError:
        logger.info("Yesterday's journal not found: %s", path)
        return filename, None


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
        raw_body = (u.get("body") or "").strip()
        body = _strip_h2_sections_from_update_body(raw_body).strip() or "(body redacted — content not a valid source for this update)"
        lines.append(f"--- update created {created} ---\n{body}")
    return "\n\n".join(lines)


def _format_yesterday_da_note_block(notes: list[tuple[str, str | None]]) -> str:
    """Format JUST yesterday's DA note — the only DA-note source for Previous Day's Wins.

    `notes` is the full list (oldest first); yesterday is the last entry.
    """
    if not notes:
        return "(no daily action notes available)"
    date_str, content = notes[-1]
    if content is None:
        return f"--- DA {date_str} (no note) ---"
    cleaned = _sanitize_da_note_for_llm(content).strip()
    return f"--- DA {date_str} ---\n{cleaned}"


def _format_older_da_notes_block(notes: list[tuple[str, str | None]]) -> str:
    """Format the older DA notes (everything except yesterday) — used only for
    In-progress / Follow-ups trajectory context, never for wins."""
    if not notes or len(notes) <= 1:
        return "(no older daily action notes available)"
    parts: list[str] = []
    for date_str, content in notes[:-1]:
        if content is None:
            parts.append(f"--- DA {date_str} (no note) ---")
        else:
            cleaned = _sanitize_da_note_for_llm(content).strip()
            parts.append(f"--- DA {date_str} ---\n{cleaned}")
    return "\n\n".join(parts)


def _format_journal_block(journal: tuple[str, str | None]) -> str:
    filename, content = journal
    if not content:
        return f"(no journal entry found for {filename})"
    return f"--- {filename} ---\n{content.strip()}"


def generate_update_body(
    daily_action_notes: list[tuple[str, str | None]],
    recent_updates: list[dict],
    now_local: datetime,
    yesterday_journal: tuple[str, str | None] | None = None,
) -> str:
    """Call OpenAI to produce the markdown body for the new initiative update."""
    client = _get_openai_client()

    yesterday_local = (now_local - timedelta(days=1)).strftime("%A, %B %d, %Y")
    user_prompt = SUMMARY_USER_PROMPT_TEMPLATE.format(
        today_local=now_local.strftime("%A, %B %d, %Y"),
        yesterday_local=yesterday_local,
        recent_updates_count=len(recent_updates),
        recent_updates_block=_format_recent_updates_block(recent_updates),
        yesterday_da_note_block=_format_yesterday_da_note_block(daily_action_notes),
        older_da_notes_block=_format_older_da_notes_block(daily_action_notes),
        journal_block=_format_journal_block(yesterday_journal or ("yesterday's journal", None)),
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
        target = _resolve_main_thread_initiative(initiatives)
        initiative_id = target["id"]
        initiative_name = target.get("name", "(unknown)")
        logger.info(
            "Main-thread initiative resolved: %s (%s)", initiative_name, initiative_id
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

        journal = load_yesterday_journal(now_local)
        logger.info(
            "Loaded yesterday's journal: %s (%s)",
            journal[0],
            "present" if journal[1] else "missing",
        )

        if not present and not recent_updates and not journal[1]:
            logger.warning(
                "No daily action notes, prior updates, or journal; nothing to summarize. Skipping."
            )
            return True

        body = generate_update_body(daily_notes, recent_updates, now_local, journal)
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
        description="Post the daily initiative update to the main-thread Linear initiative."
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
