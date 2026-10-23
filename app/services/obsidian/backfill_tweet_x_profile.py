"""One-off: add YAML ``X Profile`` on existing Tweets from @handle notes.

Dry-run is the default. Pass ``--apply`` to upload. Writes are rev-safe
(``WriteMode.update(rev)``, never overwrite) and take the per-note Redis
lock. Only YAML is touched — no journal buffet append, no Journal date,
no ``modified time``, and the original Dropbox ``client_modified`` is
preserved so ``update_modified_files_today`` does not treat the note as
newly edited.

Usage (from ``app/``, or via docker exec into the app container):

    uv run python -m services.obsidian.backfill_tweet_x_profile
    uv run python -m services.obsidian.backfill_tweet_x_profile --apply

    uv run python scripts/backfill_tweet_x_profile.py
    uv run python scripts/backfill_tweet_x_profile.py --apply
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal

import dropbox

from services.obsidian.add_readwise_buffet import (
    _ensure_tweet_page_x_profile,
    _get_dropbox_client,
    _handle_from_tweet_page,
    _resolve_knowledge_hub_folder,
    _tweets_from_handle_label,
)
from services.obsidian.utils.dropbox_rev_safe import (
    is_conflicted_copy_path,
    is_dropbox_write_conflict,
    write_mode_update,
)
from services.obsidian.utils.readwise_concurrency import lock_readwise_target

logger = logging.getLogger(__name__)

TWEETS_FROM_NOTE_NAME = re.compile(r"^Tweets from @.+\.md$", re.I)

Action = Literal[
    "would_add",
    "added",
    "skipped_already_present",
    "skipped_no_handle",
    "skipped_rev",
    "skipped_conflicted",
    "skipped_error",
]

REPORT_ACTIONS = (
    "would_add",
    "added",
    "skipped_already_present",
    "skipped_no_handle",
    "skipped_rev",
    "skipped_conflicted",
)


@dataclass(frozen=True)
class TweetNoteDownload:
    path: str
    content: str
    rev: str
    client_modified: datetime | None


def _is_tweets_from_note_name(name: str) -> bool:
    return bool(TWEETS_FROM_NOTE_NAME.match(name or ""))


def _naive_utc(dt: datetime | None) -> datetime | None:
    """Dropbox ``client_modified`` must be a naive UTC datetime."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _list_tweets_from_note_paths(dbx: dropbox.Dropbox, hub_path: str) -> list[str]:
    """``Tweets from @*.md`` in the Knowledge Hub folder (non-recursive)."""
    result = dbx.files_list_folder(hub_path)
    paths: list[str] = []
    while True:
        entries = getattr(result, "entries", None)
        if not isinstance(entries, (list, tuple)):
            break
        for entry in entries:
            if isinstance(entry, dropbox.files.FolderMetadata):
                continue
            name = getattr(entry, "name", "") or ""
            if not _is_tweets_from_note_name(name):
                continue
            path = getattr(entry, "path_display", None) or getattr(
                entry, "path_lower", None
            )
            if path:
                paths.append(path)
        if getattr(result, "has_more", False) is not True:
            break
        result = dbx.files_list_folder_continue(result.cursor)
    logger.info("Tweet X Profile backfill candidates=%s hub=%s", len(paths), hub_path)
    return paths


def download_tweet_note(dbx: dropbox.Dropbox, path: str) -> TweetNoteDownload:
    """Download a note and capture rev + client_modified for a muted update."""
    try:
        metadata, response = dbx.files_download(path)
    except dropbox.exceptions.ApiError as exc:
        if isinstance(exc.error, dropbox.files.DownloadError):
            raise FileNotFoundError(f"File not found: {path}") from exc
        raise
    content = response.content.decode("utf-8")
    rev = getattr(metadata, "rev", None) if metadata is not None else None
    if not isinstance(rev, str) or not rev:
        raise ValueError(f"No Dropbox rev on download for {path}")
    path_display = (
        getattr(metadata, "path_display", None) if metadata is not None else None
    )
    client_modified = (
        getattr(metadata, "client_modified", None) if metadata is not None else None
    )
    return TweetNoteDownload(
        path=path_display or path,
        content=content,
        rev=rev,
        client_modified=client_modified if isinstance(client_modified, datetime) else None,
    )


def plan_x_profile_backfill(
    content: str,
    *,
    path: str | None = None,
) -> tuple[str, str | None]:
    """Return ``(updated_or_same, handle)``. Handle is None when it cannot be read."""
    handle = _handle_from_tweet_page(content, path=path)
    if not handle:
        handle = _tweets_from_handle_label((path or "").rsplit("/", 1)[-1])
    if not handle:
        return content, None
    return _ensure_tweet_page_x_profile(content, handle), handle


def _upload_yaml_only(
    dbx: dropbox.Dropbox,
    note: TweetNoteDownload,
    updated: str,
) -> Action:
    """Rev-safe YAML-only write. Preserves client_modified; mutes hub notify."""
    mode = write_mode_update(note.rev)
    upload_kwargs: dict = {
        "mode": mode,
        "autorename": False,
        "mute": True,
    }
    client_modified = _naive_utc(note.client_modified)
    if client_modified is not None:
        upload_kwargs["client_modified"] = client_modified
    try:
        dbx.files_upload(
            updated.encode("utf-8"),
            note.path,
            **upload_kwargs,
        )
    except dropbox.exceptions.ApiError as exc:
        if is_dropbox_write_conflict(exc):
            logger.warning(
                "Tweet X Profile backfill rev conflict path=%s rev=%s; skip",
                note.path,
                note.rev,
            )
            return "skipped_rev"
        raise
    return "added"


def process_note(
    dbx: dropbox.Dropbox,
    path: str,
    *,
    apply: bool,
) -> tuple[Action, str | None]:
    """Download one Tweets-from note and add ``X Profile`` when missing."""
    if is_conflicted_copy_path(path):
        return "skipped_conflicted", None

    try:
        note = download_tweet_note(dbx, path)
    except FileNotFoundError:
        logger.warning("Tweet X Profile backfill missing path=%s", path)
        return "skipped_error", None
    except ValueError:
        logger.warning(
            "Tweet X Profile backfill no rev path=%s; skip (no overwrite)",
            path,
        )
        return "skipped_rev", None

    if is_conflicted_copy_path(note.path):
        return "skipped_conflicted", None

    updated, handle = plan_x_profile_backfill(note.content, path=note.path or path)
    if handle is None:
        return "skipped_no_handle", None
    if updated == note.content:
        return "skipped_already_present", handle
    if not apply:
        return "would_add", handle
    return _upload_yaml_only(dbx, note, updated), handle


def format_candidate_report(path: str, action: Action, handle: str | None) -> str:
    suffix = f"\thandle={handle}" if handle else ""
    return f"{path}\t{action}{suffix}"


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


def run_backfill_tweet_x_profile(
    *,
    apply: bool = False,
    dbx: dropbox.Dropbox | None = None,
    paths: Iterable[str] | None = None,
) -> dict[str, int]:
    """Scan ``Tweets from @*.md`` notes and add ``X Profile`` when missing.

    ``apply=False`` (default) is dry-run: no uploads. ``apply=True`` writes
    only notes that lack the property, using ``WriteMode.update(rev)``.
    Does not write the daily journal or update Journal / modified YAML.
    """
    client = dbx if dbx is not None else _get_dropbox_client()
    if paths is None:
        hub_path = _resolve_knowledge_hub_folder(client)
        candidate_paths = _list_tweets_from_note_paths(client, hub_path)
    else:
        candidate_paths = list(paths)

    counts = _empty_counts()
    mode = "apply" if apply else "dry-run"
    logger.info(
        "Tweet X Profile backfill mode=%s candidates=%s",
        mode,
        len(candidate_paths),
    )
    print(f"mode={mode} candidates={len(candidate_paths)}", flush=True)

    for path in candidate_paths:
        try:
            with lock_readwise_target(path):
                action, handle = process_note(client, path, apply=apply)
        except Exception:
            logger.exception("Tweet X Profile backfill failed path=%s", path)
            action, handle = "skipped_error", None
        counts[action] += 1
        if action in REPORT_ACTIONS or action == "skipped_error":
            _report(format_candidate_report(path, action, handle))

    _print_counts(counts)
    return dict(counts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-off: add YAML X Profile to existing Tweets from @handle "
            "Knowledge Hub notes. Dry-run by default; pass --apply to write."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Upload YAML updates (rev-safe). Default is dry-run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    args = build_parser().parse_args(argv)
    run_backfill_tweet_x_profile(apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
