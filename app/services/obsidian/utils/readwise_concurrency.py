"""Readwise KH / journal concurrency: URL identity, target lock, Reader debounce.

Two independent Redis guards, both using the shared ``config.redis_client``
(no broker / worker process):

1. **Per-target lock** (``readwise:target:lock:<identity>``)
   Serializes Dropbox mutates for the same Knowledge Hub note or journal
   file. Identity prefers a normalized page URL (tracking query stripped);
   otherwise the Dropbox target path. Unrelated identities stay parallel.
   TTL 60s, blocking wait 45s, always released in ``finally``.

2. **Reader document single-flight** (``readwise:reader:singleflight:<url>``)
   ``SET NX`` with a 45s TTL, checked at the start of
   ``reader.*_document.created`` processing. The first arrival does the
   real work (KH write + Raindrop). Later arrivals in the window **no-op
   immediately** — they do not wait for the winner. A failed / deferred
   winner deletes the claim so a retry can run before TTL. Redis errors
   fail open (process the event).
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from config import redis_client
from services.obsidian.utils.redis_lock import redis_lock

logger = logging.getLogger(__name__)

TARGET_LOCK_KEY_PREFIX = "readwise:target:lock:"
READER_SINGLEFLIGHT_KEY_PREFIX = "readwise:reader:singleflight:"
# Permanent book_id → Knowledge Hub path_display (seeded from YAML or
# written after a successful document/highlight resolve).
BOOK_PATH_KEY_PREFIX = "readwise:kh:book:"
# Window long enough to cover the 2026-09-22 simultaneous Reader pair
# (~same second) and the ~28s highlight double-fire, short enough that a
# later legitimate retry / reconcile still writes.
READER_SINGLEFLIGHT_TTL_SECONDS = 45

_TRACKING_QUERY_KEYS = frozenset({"si", "is"})


def strip_tracking_query(url: str) -> str:
    """Drop share/tracking query keys such as ``si=`` / ``is=``. Keep the rest."""
    text = (url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.query:
        return text
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    return urlunparse(parsed._replace(query=urlencode(kept, doseq=True)))


def normalize_knowledge_hub_url(url: str) -> str:
    """Stable KH identity for a page URL.

    Strips ``si`` / ``is`` tracking params, trailing slash, URL fragment,
    and lowercases scheme/host. YouTube URLs collapse to
    ``https://www.youtube.com/watch?v=<id>`` so share links and Reader
    saves lock/debounce as one note.
    """
    text = strip_tracking_query(url)
    if not text:
        return ""
    try:
        from services.obsidian.add_youtube_link import _extract_video_id, is_valid_youtube_url

        if is_valid_youtube_url(text):
            video_id = _extract_video_id(text)
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"
    except Exception:
        logger.debug("YouTube URL normalize skipped for %s", text[:80], exc_info=True)

    parsed = urlparse(text)
    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()
    path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def readwise_target_lock_key(identity: str) -> str:
    """Redis key for one KH / journal mutate target."""
    return f"{TARGET_LOCK_KEY_PREFIX}{identity}"


def reader_singleflight_key(normalized_url: str) -> str:
    return f"{READER_SINGLEFLIGHT_KEY_PREFIX}{normalized_url}"


def readwise_book_path_key(book_id: str) -> str:
    """Redis key for the KH ``path_display`` of one Readwise book."""
    return f"{BOOK_PATH_KEY_PREFIX}{book_id}"


def get_hub_path_for_book_id(book_id: str, *, client=None) -> str | None:
    """Return the cached KH ``path_display`` for ``book_id``, or None.

    Redis errors fail open (treat as a miss so Dropbox search can run).
    """
    text = (book_id or "").strip()
    if not text:
        return None
    redis = client if client is not None else redis_client
    try:
        value = redis.get(readwise_book_path_key(text))
    except Exception:
        logger.exception(
            "Readwise book path lookup failed book_id=%s; falling back to search",
            text,
        )
        return None
    if value is None:
        return None
    path = str(value).strip()
    return path or None


def set_hub_path_for_book_id(book_id: str, path_display: str, *, client=None) -> None:
    """Remember ``book_id → path_display``. Redis errors are logged, not raised."""
    key = (book_id or "").strip()
    path = (path_display or "").strip()
    if not key or not path:
        return
    redis = client if client is not None else redis_client
    try:
        redis.set(readwise_book_path_key(key), path)
    except Exception:
        logger.exception(
            "Readwise book path store failed book_id=%s path=%s",
            key,
            path,
        )


def lock_readwise_target(identity: str, **kwargs):
    """Lock one Readwise Dropbox target (URL or path). See ``redis_lock``."""
    return redis_lock(readwise_target_lock_key(identity), **kwargs)


def claim_reader_document_singleflight(
    normalized_url: str,
    *,
    client=None,
    ttl_seconds: int = READER_SINGLEFLIGHT_TTL_SECONDS,
) -> bool:
    """Claim the Reader debounce window. True if this run should do the work.

    Later arrivals with the same normalized URL within ``ttl_seconds``
    get False and should no-op (no KH write, no Raindrop). Empty URL
    skips the debounce (returns True). Redis errors fail open.
    """
    text = (normalized_url or "").strip()
    if not text:
        return True
    redis = client if client is not None else redis_client
    try:
        return bool(
            redis.set(
                reader_singleflight_key(text),
                "1",
                nx=True,
                ex=int(ttl_seconds),
            )
        )
    except Exception:
        logger.exception(
            "Reader document single-flight claim failed url=%s; processing anyway",
            text[:120],
        )
        return True


def release_reader_document_singleflight(
    normalized_url: str,
    *,
    client=None,
) -> None:
    """Drop a claim so a failed / deferred run can retry before TTL."""
    text = (normalized_url or "").strip()
    if not text:
        return
    redis = client if client is not None else redis_client
    try:
        redis.delete(reader_singleflight_key(text))
    except Exception:
        logger.exception(
            "Reader document single-flight release failed url=%s",
            text[:120],
        )
