"""One-off migration: move ``### Article highlights`` above the KH title.

PR #213 writes new article-page highlights above the first H1 / H2. Older
Knowledge Hub notes still have that section under the title (or at the
bottom after scraped body). This module relocates those existing sections
without touching book / tweet / transcript headings.

The page title is **not** “the first ATX heading.” Scraped article bodies
often contain their own ``#`` / ``##``. A heading is the title only when
it is H1/H2 **and** its trimmed text exact- or casefold-matches the YAML
``title``, the filename stem, or the hub ``Title by Author`` stem for
that note. When the note has ``readwise_id`` or ``readwise_url`` in YAML,
a heading that casefold-equals a word-boundary prefix of an expected
stem (or whose stem is a word-boundary prefix of the heading) also
counts, if the shorter side is at least 12 characters. No match → skip
(``skipped_no_verified_title``); never guess “any heading above
Article highlights.”

Dry-run is the default. Pass ``--apply`` to upload. Writes use the shared
rev-safe Dropbox path (``WriteMode.update(rev)``, never overwrite).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Literal

import dropbox

from services.obsidian.add_readwise_buffet import (
    ARTICLE_HIGHLIGHTS_HEADER,
    _frontmatter_body_start,
    _get_dropbox_client,
    _highlight_section_end,
    _hub_filename_stem,
    _resolve_knowledge_hub_folder,
    _search_match_md_path,
    _section_bounds,
    knowledge_hub_note_stem,
    reader_knowledge_hub_note_stem,
)
from services.obsidian.add_shared_link import _extract_frontmatter
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

# Candidate page titles are H1 or H2 only — not ``###``–``######``.
_TITLE_ATX = re.compile(r"^#{1,2}(?:\s|$)")
_HEADING_TEXT = re.compile(r"^#{1,2}\s*(.*)$")
_RAW_YAML_TITLE = re.compile(r"^title:\s*(.*)$")

# Prefix gate for short Genesis title-case / truncated headings such as
# ``## Nobel Lecture`` vs expected ``Nobel Lecture by Muhammad Yunus``.
# Only used when YAML already identifies the note as Readwise-backed.
_MIN_TITLE_PREFIX_LEN = 12

Action = Literal[
    "would_move",
    "moved",
    "skipped_already_correct",
    "skipped_rev",
    "skipped_conflicted",
    "skipped_no_section",
    "skipped_no_verified_title",
    "skipped_error",
]

Placement = Literal[
    "needs_move",
    "already_correct",
    "no_section",
    "no_verified_title",
]

REPORT_ACTIONS = (
    "would_move",
    "moved",
    "skipped_already_correct",
    "skipped_rev",
    "skipped_conflicted",
    "skipped_no_verified_title",
)


@dataclass(frozen=True)
class RelocationAnalysis:
    """Pure-markdown plan: whether/where to move article highlights."""

    placement: Placement
    updated_content: str
    changed: bool
    matched_title_line: str | None
    yaml_title: str | None
    filename_stem: str | None
    title_idx: int | None
    header_idx: int | None
    before_sketch: tuple[str, ...]
    after_sketch: tuple[str, ...]


def _strip_surrounding_quotes(text: str) -> str:
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1].strip()
    return stripped


def _as_title_text(value: object) -> str | None:
    if value is None or isinstance(value, (list, dict, bool)):
        return None
    text = _strip_surrounding_quotes(str(value))
    return text or None


def yaml_title_field(content: str) -> str | None:
    """YAML ``title`` with surrounding quotes stripped. None if missing."""
    frontmatter, _body = _extract_frontmatter(content)
    parsed = _as_title_text(frontmatter.get("title"))
    if parsed:
        return parsed
    lines = content.split("\n")
    body_start = _frontmatter_body_start(lines)
    if body_start <= 0:
        return None
    for line in lines[1:body_start]:
        match = _RAW_YAML_TITLE.match(line.strip())
        if match:
            return _as_title_text(match.group(1))
    return None


def filename_stem_from_path(path: str | None) -> str | None:
    if not path:
        return None
    stem = _hub_filename_stem(path).strip()
    return stem or None


def expected_title_texts(
    content: str,
    path: str | None = None,
    *,
    filename_stem: str | None = None,
) -> list[str]:
    """YAML title, filename stem, and hub ``Title by Author`` stem forms."""
    expected: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        text = (value or "").strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        expected.append(text)

    yaml_title = yaml_title_field(content)
    add(yaml_title)
    add(filename_stem.strip() if filename_stem else None)
    add(filename_stem_from_path(path))

    frontmatter, _body = _extract_frontmatter(content)
    author = frontmatter.get("author")
    if yaml_title:
        add(knowledge_hub_note_stem(yaml_title))
        add(reader_knowledge_hub_note_stem(yaml_title, author))
    stem = filename_stem_from_path(path) or (filename_stem.strip() if filename_stem else None)
    if stem:
        add(knowledge_hub_note_stem(stem))
        add(reader_knowledge_hub_note_stem(yaml_title or stem, author))

    return expected


def _heading_text(line: str) -> str | None:
    if not _TITLE_ATX.match(line):
        return None
    match = _HEADING_TEXT.match(line)
    if not match:
        return None
    return match.group(1).strip()


def yaml_has_readwise_identity(content: str) -> bool:
    """True when YAML has a non-empty ``readwise_id`` or ``readwise_url``."""
    frontmatter, _body = _extract_frontmatter(content)
    for key in ("readwise_id", "readwise_url"):
        value = frontmatter.get(key)
        if value is None or isinstance(value, (list, dict, bool)):
            continue
        if str(value).strip():
            return True
    return False


def _is_word_boundary_prefix(shorter: str, longer: str) -> bool:
    """True when *shorter* is a prefix of *longer* and does not split a word."""
    if not shorter or not longer.startswith(shorter):
        return False
    if len(shorter) == len(longer):
        return True
    return not longer[len(shorter)].isalnum()


def heading_matches_expected_title(
    heading: str,
    expected: Iterable[str],
    *,
    allow_prefix: bool = False,
    min_prefix_len: int = _MIN_TITLE_PREFIX_LEN,
) -> bool:
    """Exact, casefold, or (optionally) long word-boundary prefix match."""
    text = heading.strip()
    if not text:
        return False
    folded = text.casefold()
    for raw in expected:
        candidate = (raw or "").strip()
        if not candidate:
            continue
        if text == candidate or folded == candidate.casefold():
            return True
        if not allow_prefix:
            continue
        other = candidate.casefold()
        if len(folded) <= len(other):
            shorter, longer = folded, other
        else:
            shorter, longer = other, folded
        if len(shorter) >= min_prefix_len and _is_word_boundary_prefix(
            shorter, longer
        ):
            return True
    return False


def verified_title_heading_index(
    lines: list[str],
    body_start: int,
    expected: Iterable[str],
    *,
    allow_prefix: bool = False,
    min_prefix_len: int = _MIN_TITLE_PREFIX_LEN,
) -> int | None:
    """First H1/H2 whose text exact- or casefold-matches an expected title.

    When ``allow_prefix`` is true (Readwise YAML present), also accept a
    heading that casefold-equals a word-boundary prefix of an expected
    stem, or whose stem is a word-boundary prefix of the heading, if the
    shorter side is at least ``min_prefix_len`` characters. Never treats
    “the last ``#`` / ``##`` above Article highlights” as a title.
    """
    wanted = [text.strip() for text in expected if text and text.strip()]
    if not wanted:
        return None
    for index in range(body_start, len(lines)):
        text = _heading_text(lines[index])
        if text is not None and heading_matches_expected_title(
            text,
            wanted,
            allow_prefix=allow_prefix,
            min_prefix_len=min_prefix_len,
        ):
            return index
    return None


def _body_header_index(lines: list[str], body_start: int) -> int | None:
    """``### Article highlights`` in the post-YAML body, or None."""
    header_idx, _ignored_end = _section_bounds(lines, ARTICLE_HIGHLIGHTS_HEADER)
    if header_idx is None or header_idx < body_start:
        return None
    return header_idx


def _sketch_lines(lines: list[str], *centers: int | None, width: int = 5) -> tuple[str, ...]:
    """3–5 lines around the first valid center index."""
    valid = [index for index in centers if index is not None and 0 <= index < len(lines)]
    if not valid or not lines:
        return tuple()
    center = min(valid)
    half = width // 2
    start = max(0, center - half)
    end = min(len(lines), start + width)
    start = max(0, end - width)
    return tuple(lines[start:end])


def _move_section_above_title(
    lines: list[str],
    header_idx: int,
    expected: Iterable[str],
    body_start: int,
    *,
    allow_prefix: bool = False,
) -> list[str] | None:
    section_end = _highlight_section_end(lines, header_idx)
    section = list(lines[header_idx:section_end])
    while section and not section[-1].strip():
        section.pop()
    if not section:
        return None
    section.append("")

    remaining = lines[:header_idx] + lines[section_end:]
    new_title_idx = verified_title_heading_index(
        remaining, body_start, expected, allow_prefix=allow_prefix
    )
    if new_title_idx is None:
        return None

    prefix = remaining[:new_title_idx]
    if prefix and prefix[-1].strip():
        prefix = prefix + [""]
    return prefix + section + remaining[new_title_idx:]


def analyze_article_highlights_relocation(
    content: str,
    path: str | None = None,
    *,
    filename_stem: str | None = None,
) -> RelocationAnalysis:
    """Decide whether ``### Article highlights`` should move above a verified title."""
    stem = filename_stem.strip() if filename_stem else filename_stem_from_path(path)
    yaml_title = yaml_title_field(content)
    lines = content.split("\n")
    body_start = _frontmatter_body_start(lines)
    header_idx = _body_header_index(lines, body_start)
    expected = expected_title_texts(content, path, filename_stem=filename_stem)
    allow_prefix = yaml_has_readwise_identity(content)
    title_idx = verified_title_heading_index(
        lines, body_start, expected, allow_prefix=allow_prefix
    )
    matched = lines[title_idx] if title_idx is not None else None

    empty = RelocationAnalysis(
        placement="no_section",
        updated_content=content,
        changed=False,
        matched_title_line=matched,
        yaml_title=yaml_title,
        filename_stem=stem,
        title_idx=title_idx,
        header_idx=header_idx,
        before_sketch=(),
        after_sketch=(),
    )
    if header_idx is None:
        return empty
    if title_idx is None:
        return RelocationAnalysis(
            placement="no_verified_title",
            updated_content=content,
            changed=False,
            matched_title_line=None,
            yaml_title=yaml_title,
            filename_stem=stem,
            title_idx=None,
            header_idx=header_idx,
            before_sketch=_sketch_lines(lines, header_idx),
            after_sketch=(),
        )
    if header_idx < title_idx:
        window = _sketch_lines(lines, header_idx, title_idx)
        return RelocationAnalysis(
            placement="already_correct",
            updated_content=content,
            changed=False,
            matched_title_line=matched,
            yaml_title=yaml_title,
            filename_stem=stem,
            title_idx=title_idx,
            header_idx=header_idx,
            before_sketch=window,
            after_sketch=window,
        )

    updated_lines = _move_section_above_title(
        lines, header_idx, expected, body_start, allow_prefix=allow_prefix
    )
    if updated_lines is None:
        return RelocationAnalysis(
            placement="no_verified_title",
            updated_content=content,
            changed=False,
            matched_title_line=matched,
            yaml_title=yaml_title,
            filename_stem=stem,
            title_idx=title_idx,
            header_idx=header_idx,
            before_sketch=_sketch_lines(lines, title_idx, header_idx),
            after_sketch=(),
        )
    updated = "\n".join(updated_lines)
    if updated == content:
        window = _sketch_lines(lines, header_idx, title_idx)
        return RelocationAnalysis(
            placement="already_correct",
            updated_content=content,
            changed=False,
            matched_title_line=matched,
            yaml_title=yaml_title,
            filename_stem=stem,
            title_idx=title_idx,
            header_idx=header_idx,
            before_sketch=window,
            after_sketch=window,
        )
    new_title_idx = verified_title_heading_index(
        updated_lines, body_start, expected, allow_prefix=allow_prefix
    )
    new_header_idx = _body_header_index(updated_lines, body_start)
    return RelocationAnalysis(
        placement="needs_move",
        updated_content=updated,
        changed=True,
        matched_title_line=matched,
        yaml_title=yaml_title,
        filename_stem=stem,
        title_idx=title_idx,
        header_idx=header_idx,
        before_sketch=_sketch_lines(lines, title_idx, header_idx),
        after_sketch=_sketch_lines(updated_lines, new_header_idx, new_title_idx),
    )


def relocate_article_highlights_above_title(
    content: str,
    path: str | None = None,
    *,
    filename_stem: str | None = None,
) -> tuple[str, bool]:
    """Move ``### Article highlights`` from under the verified title to above it.

    Only mutates notes where the section currently appears after a verified
    H1/H2 title in the body (post-YAML). A heading is verified when its
    text exact- or casefold-matches YAML ``title``, the filename stem, or
    the hub ``Title by Author`` stem. Notes with ``readwise_id`` /
    ``readwise_url`` also accept a long (≥12 char) word-boundary prefix
    of those expected texts. Already-correct notes, notes without the
    section, and notes with no verified title are unchanged. Book / tweet
    / transcript sections stay where they are.

    The moved block is the heading plus contiguous highlight bullets, using
    the same ``_highlight_section_end`` / ``_section_bounds`` rules as
    ``add_readwise_buffet``. YAML frontmatter lines are not rewritten.

    Returns ``(new_content, changed)``.
    """
    analysis = analyze_article_highlights_relocation(
        content, path, filename_stem=filename_stem
    )
    return analysis.updated_content, analysis.changed


def classify_article_highlights_placement(
    content: str,
    path: str | None = None,
    *,
    filename_stem: str | None = None,
) -> str:
    """Why a note would or would not move. Used for dry-run / skip logs."""
    return analyze_article_highlights_relocation(
        content, path, filename_stem=filename_stem
    ).placement


def format_candidate_report(
    path: str,
    action: Action,
    analysis: RelocationAnalysis | None,
) -> str:
    """Dry-run / apply line: path, titles, and a short before→after sketch."""
    lines = [f"{path}\t{action}"]
    if analysis is None:
        return lines[0]
    lines.append(f"  matched_title: {analysis.matched_title_line or ''}")
    lines.append(f"  yaml_title: {analysis.yaml_title or ''}")
    lines.append(f"  filename_stem: {analysis.filename_stem or ''}")
    if analysis.before_sketch or analysis.after_sketch:
        lines.append("  before:")
        for row in analysis.before_sketch:
            lines.append(f"    | {row}")
        if analysis.changed or analysis.placement == "needs_move":
            lines.append("  after:")
            for row in analysis.after_sketch:
                lines.append(f"    | {row}")
        elif analysis.placement == "already_correct":
            lines.append("  after: (unchanged; section already above verified title)")
        elif analysis.placement == "no_verified_title":
            lines.append("  after: (skipped; no verified #/## title)")
    return "\n".join(lines)


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


def process_note(
    dbx: dropbox.Dropbox,
    path: str,
    *,
    apply: bool,
) -> tuple[Action, RelocationAnalysis | None]:
    """Download one note, relocate if needed, optionally rev-safe upload."""
    if is_conflicted_copy_path(path):
        return "skipped_conflicted", None

    try:
        note = download_text_with_rev(dbx, path)
    except FileNotFoundError:
        logger.warning("Article highlights migrate missing path=%s", path)
        return "skipped_error", None
    except ValueError:
        logger.warning(
            "Article highlights migrate no rev path=%s; skip (no overwrite)",
            path,
        )
        return "skipped_rev", None

    if is_conflicted_copy_path(note.path):
        return "skipped_conflicted", None

    analysis = analyze_article_highlights_relocation(note.content, note.path or path)
    if not analysis.changed:
        if analysis.placement == "already_correct":
            return "skipped_already_correct", analysis
        if analysis.placement == "no_verified_title":
            return "skipped_no_verified_title", analysis
        return "skipped_no_section", analysis

    if not apply:
        return "would_move", analysis

    result = upload_if_rev_matches(
        dbx,
        note.path,
        analysis.updated_content.encode("utf-8"),
        note.rev,
    )
    if result.status == "updated":
        return "moved", analysis
    return "skipped_rev", analysis


def run_relocate_article_highlights(
    *,
    apply: bool = False,
    dbx: dropbox.Dropbox | None = None,
    paths: Iterable[str] | None = None,
) -> dict[str, int]:
    """Scan Knowledge Hub notes and relocate article highlights.

    ``apply=False`` (default) is dry-run: no uploads. ``apply=True`` writes
    only notes that still have the section after a *verified* title, using
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
            action, analysis = process_note(client, path, apply=apply)
        except Exception:
            logger.exception("Article highlights migrate failed path=%s", path)
            action, analysis = "skipped_error", None
        counts[action] += 1
        if action in REPORT_ACTIONS or action in {"skipped_error"}:
            _report(format_candidate_report(path, action, analysis))

    _print_counts(counts)
    return dict(counts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-off: move ### Article highlights above the verified Knowledge "
            "Hub title (YAML title / filename stem / Title by Author; exact or "
            "casefold, plus a long prefix when Readwise YAML is present). "
            "Dry-run by default; pass --apply to write."
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
