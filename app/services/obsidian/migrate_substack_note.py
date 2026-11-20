"""One-off: move already-ingested Substack notes onto author pages.

Finds Knowledge Hub notes whose URL is a Substack note
(``https://substack.com/@handle/note/c-<digits>``), generates the AI
title, appends ``Substack Notes from {author}``, rewrites journal
wikilinks, archives the old KH note under ``_conflicted-copies-trash``,
and PATCHes the Reader title.

Dry-run is the default. Pass ``--apply`` to write. Dropbox updates use
``WriteMode.update(rev)``. Archives are a move, never a permanent
delete, and never overwrite an existing trash file. Originals are copied
to a local ``/tmp`` backup directory before any apply write.

Usage (from ``app/``, or via docker exec into the app container):

    uv run python -m services.obsidian.migrate_substack_note
    uv run python -m services.obsidian.migrate_substack_note --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

import dropbox

from services.obsidian.add_readwise_buffet import (
    _append_substack_note_page,
    _get_dropbox_client,
    _resolve_journal_folder,
    _resolve_knowledge_hub_folder,
    _search_match_md_path,
)
from services.obsidian.add_shared_link import _extract_frontmatter
from services.obsidian.utils.dropbox_rev_safe import (
    download_text_with_rev,
    is_conflicted_copy_path,
    is_dropbox_write_conflict,
    upload_if_rev_matches,
)
from services.obsidian.utils.readwise_concurrency import lock_readwise_target
from services.obsidian.utils.substack_note import (
    attach_substack_titles,
    canonical_substack_note_url,
    format_substack_journal_bullet,
    html_to_text,
    is_substack_note_url,
    maybe_rename_reader_document,
    substack_note_page_title,
)

logger = logging.getLogger(__name__)

TRASH_FOLDER_NAME = "_conflicted-copies-trash"
KNOWN_NOTE_NAME = "Celine Nguyen's comment by Celine Nguyen.md"
_BY_AUTHOR = re.compile(r"^(?P<title>.+) by (?P<author>.+)$")
_WIKILINK = re.compile(r"\[\[(?P<target>[^\]]+)\]\]")
_SEARCH_QUERY = "substack.com/@"
_SEARCH_MAX_RESULTS = 200

Action = Literal[
    "would_migrate",
    "migrated",
    "skipped_already_author_page",
    "skipped_not_substack",
    "skipped_rev",
    "skipped_conflicted",
    "skipped_archive_exists",
    "skipped_error",
]

REPORT_ACTIONS = (
    "would_migrate",
    "migrated",
    "skipped_already_author_page",
    "skipped_not_substack",
    "skipped_rev",
    "skipped_conflicted",
    "skipped_archive_exists",
)


@dataclass
class MigrationPlan:
    """Pure plan for one already-ingested Substack note."""

    path: str
    old_stem: str
    source_url: str | None = None
    author: str | None = None
    handle: str | None = None
    summary: str | None = None
    full_title: str | None = None
    author_page_title: str | None = None
    journal_paths: list[str] = field(default_factory=list)
    journal_line: str | None = None
    archive_path: str | None = None
    readwise_id: str | None = None
    backup_note: str | None = None
    skip_reason: Action | None = None
    payload: dict = field(default_factory=dict)


def _http_url(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    if text.startswith(("http://", "https://")):
        return text
    return None


def _plain_yaml(value: object) -> str | None:
    if value is None or isinstance(value, (list, dict, bool)):
        return None
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].strip()
        if "|" in text:
            text = text.split("|", 1)[0].strip()
    return text or None


def _hub_stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name


def _is_author_page_name(name: str) -> bool:
    return name.lower().startswith("substack notes from ") and name.lower().endswith(".md")


def _vault_path() -> str:
    vault = os.getenv("DROPBOX_OBSIDIAN_VAULT_PATH")
    if not vault:
        raise EnvironmentError("DROPBOX_OBSIDIAN_VAULT_PATH not set")
    return vault.rstrip("/")


def trash_folder_path() -> str:
    return f"{_vault_path()}/{TRASH_FOLDER_NAME}"


def _yaml_source_url(content: str) -> str | None:
    frontmatter, body = _extract_frontmatter(content)
    for key in ("URL", "url", "source_url"):
        href = _http_url(frontmatter.get(key))
        if href:
            return href
    for match in re.finditer(r"https?://[^\s)>\"]+", body):
        if is_substack_note_url(match.group(0)):
            return match.group(0)
    return None


def _note_text_from_content(content: str) -> str | None:
    _frontmatter, body = _extract_frontmatter(content)
    text = html_to_text(body) or re.sub(r"\s+", " ", body).strip()
    return text or None


def _payload_from_note(content: str, path: str) -> dict:
    frontmatter, _body = _extract_frontmatter(content)
    source_url = _yaml_source_url(content)
    author = _plain_yaml(frontmatter.get("author")) or _plain_yaml(
        frontmatter.get("People")
    )
    title = _plain_yaml(frontmatter.get("title")) or _hub_stem(path)
    if not author:
        match = _BY_AUTHOR.match(title)
        if match:
            author = match.group("author").strip()
    readwise_id = _plain_yaml(frontmatter.get("readwise_id"))
    readwise_url = _http_url(frontmatter.get("readwise_url"))
    payload = {
        "event_type": "reader.any_document.created",
        "title": title,
        "author": author,
        "source_url": source_url,
        "url": readwise_url,
        "html_content": _note_text_from_content(content),
        "summary": _plain_yaml(frontmatter.get("summary")),
        "id": readwise_id,
        "category": "article",
        "site_name": "Substack",
    }
    return payload


def _search_md_paths(
    dbx: dropbox.Dropbox,
    folder: str,
    query: str,
) -> list[str]:
    options = dropbox.files.SearchOptions(
        path=folder,
        max_results=_SEARCH_MAX_RESULTS,
        filename_only=False,
        file_extensions=["md"],
    )
    paths: list[str] = []
    seen: set[str] = set()
    try:
        result = dbx.files_search_v2(query, options=options)
        while True:
            matches = getattr(result, "matches", None)
            if not isinstance(matches, (list, tuple)):
                break
            for match in matches:
                path = _search_match_md_path(match)
                if not path:
                    continue
                key = path.casefold()
                if key in seen:
                    continue
                seen.add(key)
                paths.append(path)
            if getattr(result, "has_more", False) is not True:
                break
            cursor = getattr(result, "cursor", None)
            if not cursor:
                break
            result = dbx.files_search_continue_v2(cursor)
    except Exception:
        logger.warning("Substack migrate search failed query=%s folder=%s", query, folder, exc_info=True)
    return paths


def discover_candidate_paths(dbx: dropbox.Dropbox, hub_path: str) -> list[str]:
    """KH notes that look like Substack notes, including the known Celine file."""
    paths = _search_md_paths(dbx, hub_path, _SEARCH_QUERY)
    known = f"{hub_path}/{KNOWN_NOTE_NAME}"
    if not any(p.rsplit("/", 1)[-1] == KNOWN_NOTE_NAME for p in paths):
        try:
            dbx.files_get_metadata(known)
            paths.append(known)
        except Exception:
            pass
    return paths


def _unique_archive_path(dbx: dropbox.Dropbox, trash: str, filename: str) -> str:
    """Never overwrite an existing archive; suffix if the name is taken."""
    dest = f"{trash}/{filename}"
    try:
        dbx.files_get_metadata(dest)
    except dropbox.exceptions.ApiError:
        return dest
    stem, ext = os.path.splitext(filename)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{trash}/{stem} ({stamp}){ext}"


def _ensure_trash_folder(dbx: dropbox.Dropbox, trash: str) -> None:
    try:
        dbx.files_create_folder_v2(trash)
    except dropbox.exceptions.ApiError:
        pass


def _backup_text(backup_dir: Path, label: str, content: str) -> str:
    safe = re.sub(r"[\/:*?\"<>|]", "_", label)
    path = backup_dir / safe
    path.write_text(content, encoding="utf-8")
    return str(path)


def _replace_journal_wikilink(content: str, old_stem: str, new_line: str) -> tuple[str | None, str]:
    """Replace standalone ``[[old stem]]`` buffet lines with the new format."""
    old_line = f"- [[{old_stem}]]"
    if old_line in content:
        return content.replace(old_line, new_line, 1), "replaced"
    pattern = re.compile(rf"^- \[\[{re.escape(old_stem)}\]\](?::.*)?$", re.M)
    if not pattern.search(content):
        return None, "skipped"
    return pattern.sub(new_line, content, count=1), "replaced"


def build_plan(
    dbx: dropbox.Dropbox,
    path: str,
    *,
    journal_folder: str,
) -> MigrationPlan:
    plan = MigrationPlan(path=path, old_stem=_hub_stem(path))
    if is_conflicted_copy_path(path):
        plan.skip_reason = "skipped_conflicted"
        return plan
    if _is_author_page_name(path.rsplit("/", 1)[-1]):
        plan.skip_reason = "skipped_already_author_page"
        return plan
    try:
        note = download_text_with_rev(dbx, path)
    except FileNotFoundError:
        plan.skip_reason = "skipped_error"
        return plan
    except ValueError:
        plan.skip_reason = "skipped_rev"
        return plan
    if is_conflicted_copy_path(note.path):
        plan.skip_reason = "skipped_conflicted"
        return plan
    plan.path = note.path
    payload = _payload_from_note(note.content, note.path)
    plan.payload = payload
    source = canonical_substack_note_url(payload.get("source_url"))
    if not source:
        plan.skip_reason = "skipped_not_substack"
        return plan
    plan.source_url = source
    titles = attach_substack_titles(payload)
    if not titles:
        plan.skip_reason = "skipped_error"
        return plan
    plan.author = titles.author
    plan.handle = titles.handle
    plan.summary = titles.summary
    plan.full_title = titles.full_title
    plan.author_page_title = substack_note_page_title(titles.author)
    plan.readwise_id = str(payload.get("id") or "") or None
    link = payload.get("url") or source
    plan.journal_line = format_substack_journal_bullet(titles, link)
    plan.journal_paths = _search_md_paths(dbx, journal_folder, plan.old_stem)
    plan.archive_path = _unique_archive_path(
        dbx, trash_folder_path(), note.path.rsplit("/", 1)[-1]
    )
    return plan


def format_plan_report(plan: MigrationPlan, action: Action) -> str:
    lines = [f"{plan.path}\t{action}"]
    if plan.source_url:
        lines.append(f"  source: {plan.source_url}")
    if plan.author_page_title:
        lines.append(f"  author page: {plan.author_page_title}")
    if plan.full_title:
        lines.append(f"  reader title: {plan.full_title}")
    if plan.journal_line:
        lines.append(f"  journal: {plan.journal_line}")
    if plan.journal_paths:
        lines.append("  journals: " + ", ".join(plan.journal_paths))
    if plan.archive_path:
        lines.append(f"  archive: {plan.archive_path}")
    return "\n".join(lines)


def _move_to_trash(dbx: dropbox.Dropbox, from_path: str, to_path: str) -> bool:
    _ensure_trash_folder(dbx, to_path.rsplit("/", 1)[0])
    try:
        dbx.files_get_metadata(to_path)
        logger.warning("Archive already exists; not overwriting %s", to_path)
        return False
    except dropbox.exceptions.ApiError:
        pass
    try:
        dbx.files_move_v2(from_path, to_path, autorename=False)
        return True
    except dropbox.exceptions.ApiError as exc:
        if is_dropbox_write_conflict(exc):
            logger.warning("Archive move conflict from=%s to=%s", from_path, to_path)
            return False
        raise


def apply_plan(
    dbx: dropbox.Dropbox,
    plan: MigrationPlan,
    *,
    hub_path: str,
    backup_dir: Path,
) -> Action:
    if not plan.payload or not plan.author:
        return "skipped_error"
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        note = download_text_with_rev(dbx, plan.path)
    except (FileNotFoundError, ValueError):
        return "skipped_rev"
    _backup_text(backup_dir, note.path.rsplit("/", 1)[-1], note.content)

    titles = attach_substack_titles(plan.payload)
    if not titles:
        return "skipped_error"
    maybe_rename_reader_document(plan.payload, titles)
    try:
        _append_substack_note_page(dbx, plan.payload, hub_path)
    except Exception:
        logger.exception("Substack migrate author page failed path=%s", plan.path)
        return "skipped_error"

    for journal_path in plan.journal_paths:
        try:
            journal = download_text_with_rev(dbx, journal_path)
        except (FileNotFoundError, ValueError):
            logger.warning("Substack migrate journal skip path=%s", journal_path)
            continue
        _backup_text(backup_dir, journal.path.rsplit("/", 1)[-1], journal.content)
        updated, _action = _replace_journal_wikilink(
            journal.content, plan.old_stem, plan.journal_line or ""
        )
        if not updated or updated == journal.content:
            continue
        result = upload_if_rev_matches(
            dbx, journal.path, updated.encode("utf-8"), journal.rev
        )
        if result.status != "updated":
            logger.warning("Substack migrate journal rev conflict path=%s", journal.path)

    archive_path = plan.archive_path or _unique_archive_path(
        dbx, trash_folder_path(), plan.path.rsplit("/", 1)[-1]
    )
    if not _move_to_trash(dbx, note.path, archive_path):
        return "skipped_archive_exists"
    return "migrated"


def process_note(
    dbx: dropbox.Dropbox,
    path: str,
    *,
    apply: bool,
    hub_path: str,
    journal_folder: str,
    backup_dir: Path,
) -> tuple[Action, MigrationPlan]:
    plan = build_plan(dbx, path, journal_folder=journal_folder)
    if plan.skip_reason:
        return plan.skip_reason, plan
    if not apply:
        return "would_migrate", plan
    action = apply_plan(dbx, plan, hub_path=hub_path, backup_dir=backup_dir)
    return action, plan


def _report(text: str) -> None:
    print(text, flush=True)
    for line in text.split("\n"):
        logger.info("%s", line)


def _empty_counts() -> Counter[str]:
    counts: Counter[str] = Counter()
    for action in REPORT_ACTIONS:
        counts[action] = 0
    return counts


def _print_counts(counts: Counter[str]) -> None:
    ordered = list(REPORT_ACTIONS)
    extras = [key for key in counts if key not in REPORT_ACTIONS and counts[key]]
    parts = [f"{key}={counts[key]}" for key in ordered + extras]
    summary = "counts: " + " ".join(parts)
    print(summary, flush=True)
    logger.info("%s", summary)


def run_migrate_substack_note(
    *,
    apply: bool = False,
    dbx: dropbox.Dropbox | None = None,
    paths: Iterable[str] | None = None,
    backup_dir: Path | None = None,
) -> dict[str, int]:
    """Scan KH Substack notes and migrate them onto author pages.

    ``apply=False`` (default) prints the plan. ``apply=True`` writes
    rev-safe journal/author-page updates, PATCHes Reader, and archives
    the old KH note.
    """
    client = dbx if dbx is not None else _get_dropbox_client()
    hub_path = _resolve_knowledge_hub_folder(client)
    journal_folder = _resolve_journal_folder(client)
    candidate_paths = list(paths) if paths is not None else discover_candidate_paths(
        client, hub_path
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir or Path(f"/tmp/substack-note-migrate-{stamp}")
    counts = _empty_counts()
    mode = "apply" if apply else "dry-run"
    logger.info(
        "Substack note migrate mode=%s candidates=%s backup=%s",
        mode,
        len(candidate_paths),
        backup,
    )
    print(f"mode={mode} candidates={len(candidate_paths)} backup={backup}", flush=True)

    for path in candidate_paths:
        try:
            with lock_readwise_target(path):
                action, plan = process_note(
                    client,
                    path,
                    apply=apply,
                    hub_path=hub_path,
                    journal_folder=journal_folder,
                    backup_dir=backup,
                )
        except Exception:
            logger.exception("Substack note migrate failed path=%s", path)
            action, plan = "skipped_error", MigrationPlan(path=path, old_stem=_hub_stem(path))
        counts[action] += 1
        if action in REPORT_ACTIONS or action == "skipped_error":
            _report(format_plan_report(plan, action))

    _print_counts(counts)
    return dict(counts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-off: move already-ingested Substack notes onto "
            "Substack Notes from {author} pages. Dry-run by default; "
            "pass --apply to write."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write author pages, journals, archive, and Reader PATCH. Default is dry-run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    args = build_parser().parse_args(argv)
    run_migrate_substack_note(apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
