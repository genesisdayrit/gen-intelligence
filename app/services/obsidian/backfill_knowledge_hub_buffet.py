"""Repeatable Knowledge Hub → journal Content Buffet backfill.

Manual-only (registered with a year=2099 CronTrigger). Walks Knowledge Hub
``.md`` notes, reads YAML ``Journal:`` dates, and appends ``- [[Note Title]]``
to each matching daily journal — grouped so each journal file is written once.
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import dropbox
from dotenv import load_dotenv

from services.obsidian.add_readwise_buffet import (
    _get_dropbox_client,
    _get_file_content,
    _resolve_journal_folder,
    _wikilink_from_note_stem,
    insert_content_buffet_bullet,
    journal_filename,
)
from services.obsidian.add_shared_link import (
    _extract_frontmatter,
    _find_knowledge_hub_path,
)

load_dotenv()

logger = logging.getLogger(__name__)

# Effectively "all journals" — vault notes predate the daily-journal streak.
DEFAULT_SINCE = "2018-01-01"

JOURNAL_WIKILINK_RE = re.compile(
    r"\[\[\s*([A-Za-z]{3})\.?\s+(\d{1,2}),\s+(\d{4})\s*\]\]"
)
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
_JUNK_EXACT_STEMS = {"%", "+", "_"}


def parse_since_date(value: str | date) -> date:
    """Parse an ISO date (YYYY-MM-DD) or ISO8601 datetime into a cutoff date."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1]
    if "T" in text:
        text = text.split("T", 1)[0]
    return date.fromisoformat(text)


def resolve_backfill_since(since: str | None = None) -> str:
    """Resolve since from arg, then env, then the all-journals default."""
    return since or os.getenv("KNOWLEDGE_HUB_BACKFILL_SINCE") or DEFAULT_SINCE


def is_junk_note_stem(stem: str | None) -> bool:
    """True for empty, single-char, or leftover Notion junk stems (``%``/``+``/``_``)."""
    text = (stem or "").strip()
    if not text:
        return True
    if len(text) <= 1:
        return True
    return text in _JUNK_EXACT_STEMS


def parse_journal_wikilink(value: object) -> date | None:
    """Parse a share-link ``[[Mon D, YYYY]]`` wikilink into a calendar date."""
    if value is None:
        return None
    text = str(value).strip()
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


def journal_dates_from_frontmatter(frontmatter: dict) -> list[date]:
    raw = frontmatter.get("Journal")
    if raw is None:
        raw = frontmatter.get("journal")
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    dates: list[date] = []
    seen: set[date] = set()
    for value in values:
        parsed = parse_journal_wikilink(value)
        if parsed is None or parsed in seen:
            continue
        seen.add(parsed)
        dates.append(parsed)
    return dates


def journal_date_label(journal_day: date) -> str:
    """Filename stem matching share-link writes, e.g. ``Aug 22, 2026``."""
    return journal_filename(datetime(journal_day.year, journal_day.month, journal_day.day)).removesuffix(
        ".md"
    )


def buffet_bullet_and_keys(note_title: str) -> tuple[str, list[str]] | None:
    """Same wikilink bullet + dedup keys as ``append_wikilink_to_journal_buffet``."""
    target = _wikilink_from_note_stem(note_title)
    if not target or is_junk_note_stem(target):
        return None
    bullet = f"- [[{target}]]"
    keys = [bullet, f"[[{target}]]", target]
    if note_title and note_title != target:
        keys.append(note_title)
    return bullet, keys


def _empty_summary(since: str) -> dict:
    return {
        "notes_scanned": 0,
        "relations": 0,
        "files_written": 0,
        "lines_inserted": 0,
        "lines_skipped_dup": 0,
        "missing_journal_days": 0,
        "errors": [],
        "since": since,
    }


def _iter_knowledge_hub_md(dbx: dropbox.Dropbox, kh_path: str):
    result = dbx.files_list_folder(kh_path, recursive=True)
    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata):
                continue
            name = getattr(entry, "name", "") or ""
            if not name.lower().endswith(".md"):
                continue
            path = getattr(entry, "path_lower", None) or getattr(entry, "path_display", None)
            if not path:
                continue
            yield name, path
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)


def collect_kh_relations(
    dbx: dropbox.Dropbox,
    kh_path: str,
    since_date: date,
    summary: dict,
) -> dict[str, list[tuple[str, str, list[str]]]]:
    """Scan KH notes and group (title, bullet, keys) by journal-day label."""
    by_day: dict[str, list[tuple[str, str, list[str]]]] = defaultdict(list)
    seen_on_day: dict[str, set[str]] = defaultdict(set)

    for name, path in _iter_knowledge_hub_md(dbx, kh_path):
        summary["notes_scanned"] += 1
        stem = Path(name).stem
        if is_junk_note_stem(stem):
            logger.info("KH buffet backfill skipped junk stem %r", stem)
            continue
        prepared = buffet_bullet_and_keys(stem)
        if prepared is None:
            logger.info("KH buffet backfill skipped unsanitary stem %r", stem)
            continue
        bullet, keys = prepared

        try:
            content = _get_file_content(dbx, path)
        except FileNotFoundError:
            logger.warning("KH buffet backfill note missing after list: %s", path)
            summary["errors"].append(f"{path}: note not found")
            continue
        except Exception as exc:
            logger.warning("KH buffet backfill failed to read note %s: %s", path, exc)
            summary["errors"].append(f"{path}: {exc}")
            continue

        frontmatter, _body = _extract_frontmatter(content)
        for journal_day in journal_dates_from_frontmatter(frontmatter):
            if journal_day < since_date:
                continue
            label = journal_date_label(journal_day)
            summary["relations"] += 1
            if stem in seen_on_day[label]:
                continue
            seen_on_day[label].add(stem)
            by_day[label].append((stem, bullet, keys))

    return by_day


def write_wikilinks_by_journal(
    dbx: dropbox.Dropbox,
    by_day: dict[str, list[tuple[str, str, list[str]]]],
    summary: dict,
) -> None:
    """Write grouped wikilinks; one download/upload per journal day."""
    if not by_day:
        return

    journal_folder = _resolve_journal_folder(dbx)
    for label, items in by_day.items():
        file_path = f"{journal_folder}/{label}.md"
        try:
            try:
                content = _get_file_content(dbx, file_path)
            except FileNotFoundError:
                logger.warning(
                    "KH buffet backfill skipped; journal not found (will not create): %s",
                    file_path,
                )
                summary["missing_journal_days"] += 1
                continue

            original = content
            for _stem, bullet, keys in items:
                content, action = insert_content_buffet_bullet(content, bullet, keys)
                if action in {"inserted", "replaced"}:
                    summary["lines_inserted"] += 1
                elif action == "skipped":
                    summary["lines_skipped_dup"] += 1

            if content != original:
                dbx.files_upload(
                    content.encode("utf-8"),
                    file_path,
                    mode=dropbox.files.WriteMode.overwrite,
                )
                summary["files_written"] += 1
                logger.info("KH buffet backfill wrote path=%s titles=%s", file_path, len(items))
        except Exception as exc:
            logger.exception("KH buffet backfill failed for %s", file_path)
            summary["errors"].append(f"{file_path}: {exc}")


def backfill_knowledge_hub_buffet(since: str | None = None) -> dict:
    """Append KH note wikilinks onto historical journal Content Buffet sections.

    Parameters
    ----------
    since:
        ISO date. Journal YAML dates before this are skipped.
        Default ``2018-01-01`` or ``KNOWLEDGE_HUB_BACKFILL_SINCE``.
    """
    resolved_since = resolve_backfill_since(since)
    summary = _empty_summary(resolved_since)

    try:
        since_date = parse_since_date(resolved_since)
    except ValueError:
        logger.error("KH buffet backfill invalid since=%s", resolved_since)
        summary["errors"].append(f"Invalid since date: {resolved_since}")
        return summary

    logger.info("KH buffet backfill starting since=%s", since_date.isoformat())

    try:
        dbx = _get_dropbox_client()
        vault_path = os.getenv("DROPBOX_OBSIDIAN_VAULT_PATH")
        if not vault_path:
            raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")
        kh_path = _find_knowledge_hub_path(dbx, vault_path)
        by_day = collect_kh_relations(dbx, kh_path, since_date, summary)
        write_wikilinks_by_journal(dbx, by_day, summary)
    except Exception as exc:
        logger.exception("KH buffet backfill failed")
        summary["errors"].append(str(exc))
        return summary

    logger.info(
        "KH buffet backfill finished notes_scanned=%s relations=%s "
        "files_written=%s lines_inserted=%s lines_skipped_dup=%s "
        "missing_journal_days=%s errors=%s",
        summary["notes_scanned"],
        summary["relations"],
        summary["files_written"],
        summary["lines_inserted"],
        summary["lines_skipped_dup"],
        summary["missing_journal_days"],
        len(summary["errors"]),
    )
    if summary["errors"]:
        logger.error("KH buffet backfill errors: %s", summary["errors"])
    return summary
