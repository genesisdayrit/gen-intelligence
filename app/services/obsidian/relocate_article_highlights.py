"""One-off migration: move ``### Article highlights`` above the KH title.

PR #213 writes new article-page highlights above the first H1 / H2. Older
Knowledge Hub notes still have that section under the title (or at the
bottom after scraped body). This module relocates those existing sections
without touching book / tweet / transcript headings.

Dry-run is the default. Pass ``--apply`` to upload. Writes use the shared
rev-safe Dropbox path (``WriteMode.update(rev)``, never overwrite).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from typing import Iterable, Literal

import dropbox

from services.obsidian.add_readwise_buffet import (
    ARTICLE_HIGHLIGHTS_HEADER,
    _frontmatter_body_start,
    _get_dropbox_client,
    _highlight_section_end,
    _resolve_knowledge_hub_folder,
    _search_match_md_path,
    _section_bounds,
)
from services.obsidian.utils.dropbox_rev_safe import (
    download_text_with_rev,
    is_conflicted_copy_path,
    upload_if_rev_matches,
)

logger = logging.getLogger(__name__)

# Dropbox content search. Punctuation in ``###`` is optional; the heading
# text is distinctive enough and is 3+ characters (search_v2 minimum).
_SEARCH_QUERY = "Article highlights"
_SEARCH_MAX_RESULTS = 200

# First Knowledge Hub title is H1 or H2 — not ``### … highlights``.
_TITLE_ATX = re.compile(r"^#{1,2}(?:\s|$)")

Action = Literal[
    "would_move",
    "moved",
    "skipped_already_correct",
    "skipped_rev",
    "skipped_conflicted",
    "skipped_no_section",
    "skipped_no_title",
    "skipped_error",
]

REPORT_ACTIONS = (
    "would_move",
    "moved",
    "skipped_already_correct",
    "skipped_rev",
    "skipped_conflicted",
)


def _first_title_heading_index(lines: list[str], body_start: int) -> int | None:
    """Index of the first H1 / H2 in the body. Ignores ``###`` sections."""
    for index in range(body_start, len(lines)):
        if _TITLE_ATX.match(lines[index]):
            return index
    return None


def _body_header_index(lines: list[str], body_start: int) -> int | None:
    """``### Article highlights`` in the post-YAML body, or None."""
    header_idx, _ignored_end = _section_bounds(lines, ARTICLE_HIGHLIGHTS_HEADER)
    if header_idx is None or header_idx < body_start:
        return None
    return header_idx


def relocate_article_highlights_above_title(content: str) -> tuple[str, bool]:
    """Move ``### Article highlights`` from under the first title to above it.

    Only mutates notes where the section currently appears after the first
    ATX title (``#`` / ``##``) in the body (post-YAML). Already-correct
    notes (section before title), notes without the section, and notes
    without a title heading are unchanged. Book / tweet / transcript
    sections stay where they are.

    The moved block is the heading plus contiguous highlight bullets, using
    the same ``_highlight_section_end`` / ``_section_bounds`` rules as
    ``add_readwise_buffet``. YAML frontmatter lines are not rewritten.

    Returns ``(new_content, changed)``.
    """
    lines = content.split("\n")
    body_start = _frontmatter_body_start(lines)
    header_idx = _body_header_index(lines, body_start)
    if header_idx is None:
        return content, False

    title_idx = _first_title_heading_index(lines, body_start)
    if title_idx is None or header_idx < title_idx:
        return content, False

    section_end = _highlight_section_end(lines, header_idx)
    section = list(lines[header_idx:section_end])
    while section and not section[-1].strip():
        section.pop()
    if not section:
        return content, False
    section.append("")

    remaining = lines[:header_idx] + lines[section_end:]
    new_title_idx = _first_title_heading_index(remaining, body_start)
    if new_title_idx is None:
        return content, False

    prefix = remaining[:new_title_idx]
    if prefix and prefix[-1].strip():
        prefix = prefix + [""]
    updated = "\n".join(prefix + section + remaining[new_title_idx:])
    if updated == content:
        return content, False
    return updated, True


def classify_article_highlights_placement(content: str) -> str:
    """Why a note would or would not move. Used for dry-run / skip logs."""
    lines = content.split("\n")
    body_start = _frontmatter_body_start(lines)
    header_idx = _body_header_index(lines, body_start)
    if header_idx is None:
        return "no_section"
    title_idx = _first_title_heading_index(lines, body_start)
    if title_idx is None:
        return "no_title"
    if header_idx < title_idx:
        return "already_correct"
    return "needs_move"


def _path_in_hub(path: str, hub_path: str) -> bool:
    needle = path.rstrip("/").casefold()
    root = hub_path.rstrip("/").casefold()
    return needle == root or needle.startswith(root + "/")


def _search_article_highlight_paths(dbx: dropbox.Dropbox, hub_path: str) -> list[str]:
    """KH-scoped content search for notes that mention Article highlights."""
    options = dropbox.files.SearchOptions(
        path=hub_path,
        max_results=_SEARCH_MAX_RESULTS,
        filename_only=False,
        file_extensions=["md"],
    )
    paths: list[str] = []
    seen: set[str] = set()
    result = dbx.files_search_v2(_SEARCH_QUERY, options=options)
    while True:
        matches = getattr(result, "matches", None)
        if isinstance(matches, (list, tuple)):
            for match in matches:
                path = _search_match_md_path(match)
                if not path or not _path_in_hub(path, hub_path):
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
    logger.info("KH article-highlights search unique_hits=%s", len(paths))
    return paths


def _list_knowledge_hub_md_paths(dbx: dropbox.Dropbox, hub_path: str) -> list[str]:
    """Fallback: list KH ``.md`` paths when content search is unavailable."""
    result = dbx.files_list_folder(hub_path, recursive=True)
    paths: list[str] = []
    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata):
                continue
            name = getattr(entry, "name", "") or ""
            if not name.lower().endswith(".md"):
                continue
            path = getattr(entry, "path_display", None) or getattr(
                entry, "path_lower", None
            )
            if path:
                paths.append(path)
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)
    logger.info("KH article-highlights list_folder md_files=%s", len(paths))
    return paths


def discover_candidate_paths(dbx: dropbox.Dropbox, hub_path: str) -> list[str]:
    """Prefer Dropbox search; list the KH folder only if search fails."""
    try:
        return _search_article_highlight_paths(dbx, hub_path)
    except Exception:
        logger.warning(
            "KH article-highlights search failed; falling back to list_folder",
            exc_info=True,
        )
        return _list_knowledge_hub_md_paths(dbx, hub_path)


def _report(path: str, action: Action) -> None:
    line = f"{path}\t{action}"
    print(line, flush=True)
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


def process_note(
    dbx: dropbox.Dropbox,
    path: str,
    *,
    apply: bool,
) -> Action:
    """Download one note, relocate if needed, optionally rev-safe upload."""
    if is_conflicted_copy_path(path):
        return "skipped_conflicted"

    try:
        note = download_text_with_rev(dbx, path)
    except FileNotFoundError:
        logger.warning("Article highlights migrate missing path=%s", path)
        return "skipped_error"
    except ValueError:
        logger.warning(
            "Article highlights migrate no rev path=%s; skip (no overwrite)",
            path,
        )
        return "skipped_rev"

    if is_conflicted_copy_path(note.path):
        return "skipped_conflicted"

    updated, changed = relocate_article_highlights_above_title(note.content)
    if not changed:
        placement = classify_article_highlights_placement(note.content)
        if placement == "already_correct":
            return "skipped_already_correct"
        if placement == "no_title":
            return "skipped_no_title"
        return "skipped_no_section"

    if not apply:
        return "would_move"

    result = upload_if_rev_matches(
        dbx,
        note.path,
        updated.encode("utf-8"),
        note.rev,
    )
    if result.status == "updated":
        return "moved"
    return "skipped_rev"


def run_relocate_article_highlights(
    *,
    apply: bool = False,
    dbx: dropbox.Dropbox | None = None,
    paths: Iterable[str] | None = None,
) -> dict[str, int]:
    """Scan Knowledge Hub notes and relocate article highlights.

    ``apply=False`` (default) is dry-run: no uploads. ``apply=True`` writes
    only notes that still have the section after the title, using
    ``upload_if_rev_matches``. Rev conflicts are skipped, never forced.
    """
    client = dbx if dbx is not None else _get_dropbox_client()
    if paths is None:
        hub_path = _resolve_knowledge_hub_folder(client)
        candidate_paths = discover_candidate_paths(client, hub_path)
    else:
        candidate_paths = list(paths)

    counts = _empty_counts()
    mode = "apply" if apply else "dry-run"
    logger.info(
        "Article highlights migrate mode=%s candidates=%s",
        mode,
        len(candidate_paths),
    )
    print(f"mode={mode} candidates={len(candidate_paths)}", flush=True)

    for path in candidate_paths:
        try:
            action = process_note(client, path, apply=apply)
        except Exception:
            logger.exception("Article highlights migrate failed path=%s", path)
            action = "skipped_error"
        counts[action] += 1
        if action in REPORT_ACTIONS or action in {
            "skipped_no_title",
            "skipped_error",
        }:
            _report(path, action)

    _print_counts(counts)
    return dict(counts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-off: move ### Article highlights above the first Knowledge "
            "Hub title. Dry-run by default; pass --apply to write."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Upload relocated notes (rev-safe). Default is dry-run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    args = build_parser().parse_args(argv)
    run_relocate_article_highlights(apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
