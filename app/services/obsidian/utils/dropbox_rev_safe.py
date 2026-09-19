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
from typing import Literal

import dropbox

logger = logging.getLogger(__name__)

RevSafeStatus = Literal["updated", "deferred"]


@dataclass(frozen=True)
class RevSafeUploadResult:
    """Outcome of a rev-checked upload. Never implies an overwrite write."""

    status: RevSafeStatus
    path: str
    rev: str
    metadata: object | None = None


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
