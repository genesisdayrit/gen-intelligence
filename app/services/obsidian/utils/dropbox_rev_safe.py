"""Rev-safe Dropbox uploads that never force WriteMode.overwrite races.

``WriteMode.overwrite`` with no ``rev`` lets the hub clobber a file that
Obsidian/Dropbox desktop changed after download. Dropbox then forks a
``Name (… conflicted copy DATE).md``. ``WriteMode.update(rev)`` plus
``autorename=False`` instead fails closed: latest cloud content wins, and
the caller can retry when the rev matches again.

Other Obsidian writers can adopt ``upload_if_rev_matches`` the same way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal

import dropbox

logger = logging.getLogger(__name__)

CONFLICTED_COPY_TOKEN = "conflicted copy"

RevSafeStatus = Literal["updated", "deferred"]


@dataclass(frozen=True)
class RevSafeUploadResult:
    """Outcome of a rev-checked upload. Never implies an overwrite write."""

    status: RevSafeStatus
    path: str
    rev: str
    metadata: object | None = None


def is_conflicted_copy_path(path: str | None) -> bool:
    """True when Dropbox already forked this path as a conflicted copy."""
    if not path:
        return False
    return CONFLICTED_COPY_TOKEN in path.casefold()


def is_dropbox_write_conflict(exc: BaseException) -> bool:
    """True when Dropbox rejected an upload because the path/rev conflicted."""
    if not isinstance(exc, dropbox.exceptions.ApiError):
        return False
    error = exc.error
    try:
        if not hasattr(error, "is_path") or not error.is_path():
            return False
        write_failed = error.get_path()
        reason = getattr(write_failed, "reason", None)
        if reason is None:
            return False
        is_conflict = getattr(reason, "is_conflict", None)
        return bool(is_conflict()) if callable(is_conflict) else False
    except Exception:
        return False


def write_mode_update(rev: str) -> dropbox.files.WriteMode:
    """``WriteMode.update`` for the downloaded file rev (not overwrite)."""
    if not rev:
        raise ValueError("rev is required for WriteMode.update")
    return dropbox.files.WriteMode.update(rev)


def upload_if_rev_matches(
    dbx: dropbox.Dropbox,
    path: str,
    content: bytes,
    rev: str,
) -> RevSafeUploadResult:
    """Upload ``content`` only if Dropbox still has ``rev``.

    Uses ``WriteMode.update(rev)`` with ``autorename=False`` so a mismatch
    is an error instead of a conflicted-copy filename. On conflict, leaves
    the cloud file as Dropbox has it and returns ``deferred``. Other API
    errors are re-raised.
    """
    mode = write_mode_update(rev)
    try:
        metadata = dbx.files_upload(
            content,
            path,
            mode=mode,
            autorename=False,
        )
    except dropbox.exceptions.ApiError as exc:
        if is_dropbox_write_conflict(exc):
            logger.warning(
                "Dropbox rev conflict for %s (downloaded rev=%s); "
                "skipping upload so the hub does not overwrite or create a "
                "conflicted copy. Latest cloud content wins; retry when the "
                "rev matches again.",
                path,
                rev,
            )
            return RevSafeUploadResult(status="deferred", path=path, rev=rev)
        raise

    return RevSafeUploadResult(
        status="updated",
        path=path,
        rev=rev,
        metadata=metadata,
    )


def upload_new_file(
    dbx: dropbox.Dropbox,
    path: str,
    content: bytes,
) -> RevSafeUploadResult:
    """Create ``path`` only if it does not already exist.

    Uses ``WriteMode.add`` with ``autorename=False`` so a race that creates
    the file first is an error instead of an overwrite or a conflicted-copy
    filename. On conflict, leaves the cloud file as Dropbox has it and
    returns ``deferred``. Other API errors are re-raised.
    """
    try:
        metadata = dbx.files_upload(
            content,
            path,
            mode=dropbox.files.WriteMode.add,
            autorename=False,
        )
    except dropbox.exceptions.ApiError as exc:
        if is_dropbox_write_conflict(exc):
            logger.warning(
                "Dropbox create conflict for %s; skipping upload so the hub "
                "does not overwrite or create a conflicted copy. Latest "
                "cloud content wins; retry when the path is free.",
                path,
            )
            return RevSafeUploadResult(status="deferred", path=path, rev="")
        raise

    return RevSafeUploadResult(
        status="updated",
        path=path,
        rev=getattr(metadata, "rev", "") or "",
        metadata=metadata,
    )


def record_deferred_write(
    *,
    source: str,
    kind: str,
    payload_ref: str,
    target: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Enqueue a hub write after the immediate rev-safe retry still deferred.

    Never raises — a Redis outage must not fail the live webhook / job.
    Dedup key is ``source:kind:payload_ref:target``.
    """
    try:
        from services.obsidian.reconcile.queue import enqueue_deferred

        enqueue_deferred(
            source=source,
            kind=kind,
            payload_ref=payload_ref,
            target=target,
            payload=payload,
        )
    except Exception:
        logger.exception(
            "Failed to enqueue deferred Obsidian write source=%s kind=%s ref=%s target=%s",
            source,
            kind,
            payload_ref,
            target,
        )


@dataclass(frozen=True)
class DownloadedNote:
    """Text note plus the Dropbox rev captured at download."""

    path: str
    content: str
    rev: str


def download_text_with_rev(dbx: dropbox.Dropbox, file_path: str) -> DownloadedNote:
    """Download a note and capture the Dropbox rev for a later update() write."""
    try:
        metadata, response = dbx.files_download(file_path)
    except dropbox.exceptions.ApiError as exc:
        if isinstance(exc.error, dropbox.files.DownloadError):
            raise FileNotFoundError(f"File not found: {file_path}") from exc
        raise
    content = response.content.decode("utf-8")
    rev = getattr(metadata, "rev", None) if metadata is not None else None
    if not isinstance(rev, str) or not rev:
        raise ValueError(f"No Dropbox rev on download for {file_path}")
    path_display = (
        getattr(metadata, "path_display", None) if metadata is not None else None
    )
    return DownloadedNote(path=path_display or file_path, content=content, rev=rev)


def as_defer_specs(file_path: str, defer: object | None, *, kind: str) -> list[dict[str, Any]]:
    """Normalize a defer spec, list of specs, or path-only default."""
    if defer is None:
        return [
            {
                "source": "dropbox",
                "kind": kind,
                "payload_ref": file_path,
                "target": file_path,
            }
        ]
    if isinstance(defer, list):
        return [spec for spec in defer if isinstance(spec, dict)]
    if isinstance(defer, dict):
        return [defer]
    return []


def enqueue_after_defer(file_path: str, defer: object | None, *, kind: str) -> None:
    """Enqueue after the immediate rematch still deferred. Never raises."""
    for spec in as_defer_specs(file_path, defer, kind=kind):
        record_deferred_write(
            source=str(spec.get("source") or "dropbox"),
            kind=str(spec.get("kind") or kind),
            payload_ref=str(spec.get("payload_ref") or file_path),
            target=str(spec.get("target") or file_path),
            payload=spec.get("payload") if isinstance(spec.get("payload"), dict) else None,
        )


def update_with_retry(
    dbx: dropbox.Dropbox,
    file_path: str,
    apply_fn: Callable[[str], tuple[str | None, object]],
    *,
    downloaded: DownloadedNote | None = None,
    max_attempts: int = 2,
    defer: object | None = None,
) -> tuple[str, object, str | None]:
    """Download, merge, and upload only when the downloaded rev still matches.

    ``apply_fn(content)`` returns ``(updated_or_none, meta)``. ``None`` means
    nothing to write. On rev mismatch, re-downloads and re-applies the merge
    instead of overwriting. After the immediate retry still defers, enqueues
    for the hourly reconcile.

    Returns ``(status, meta, content)`` where status is ``updated``,
    ``skipped``, ``deferred``, ``missing``, or ``error``.
    """
    last_meta: object = None
    for attempt in range(1, max_attempts + 1):
        try:
            if downloaded is not None and attempt == 1:
                note = downloaded
            else:
                note = download_text_with_rev(dbx, file_path)
        except FileNotFoundError:
            return "missing", last_meta, None
        except ValueError:
            logger.error(
                "No Dropbox rev on download for %s; skipping upload to avoid overwrite.",
                file_path,
            )
            return "error", last_meta, None

        updated, last_meta = apply_fn(note.content)
        if updated is None or updated == note.content:
            return "skipped", last_meta, note.content

        result = upload_if_rev_matches(
            dbx,
            note.path,
            updated.encode("utf-8"),
            note.rev,
        )
        if result.status == "updated":
            return "updated", last_meta, updated
        if attempt < max_attempts:
            logger.info(
                "Rev conflict on %s; re-downloading to merge into latest content.",
                file_path,
            )
            continue
        logger.warning(
            "Deferring write for %s; cloud file left unchanged "
            "(no overwrite / no conflicted copy).",
            file_path,
        )
        enqueue_after_defer(file_path, defer, kind="rev_safe_update")
        return "deferred", last_meta, note.content
    enqueue_after_defer(file_path, defer, kind="rev_safe_update")
    return "deferred", last_meta, None


def create_or_defer(
    dbx: dropbox.Dropbox,
    file_path: str,
    content: str,
    *,
    defer: object | None = None,
) -> bool:
    """Create a new note without overwrite. False if the path already exists."""
    result = upload_new_file(dbx, file_path, content.encode("utf-8"))
    if result.status == "deferred":
        logger.warning(
            "Deferring create for %s; cloud file left unchanged "
            "(no overwrite / no conflicted copy).",
            file_path,
        )
        enqueue_after_defer(file_path, defer, kind="rev_safe_create")
        return False
    return True
