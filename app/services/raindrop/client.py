"""Raindrop.io REST API client for bookmark operations."""

import logging
import os
from typing import TypedDict
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from dotenv import load_dotenv

from config import redis_client

load_dotenv()

logger = logging.getLogger(__name__)

RAINDROP_API_BASE = "https://api.raindrop.io/rest/v1"
UNSORTED_COLLECTION_ID = -1
ALL_COLLECTIONS_ID = 0
BOOKMARK_DEDUP_KEY_PREFIX = "raindrop:bookmark:"
BOOKMARK_DEDUP_TTL_SECONDS = 90 * 24 * 60 * 60
_TRACKING_QUERY_KEYS = frozenset({"si", "is"})


class RaindropBookmarkResult(TypedDict):
    """Result of a Raindrop.io bookmark operation."""

    success: bool
    bookmark_id: str | None
    error: str | None


def _get_access_token() -> str | None:
    """Get Raindrop.io access token from environment."""
    return os.getenv("RAINDROP_IO_TEST_TOKEN")


def _get_headers() -> dict[str, str]:
    """Get authorization headers for Raindrop.io API."""
    token = _get_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _ok(bookmark_id: str | None) -> RaindropBookmarkResult:
    return RaindropBookmarkResult(success=True, bookmark_id=bookmark_id, error=None)


def _err(error: str) -> RaindropBookmarkResult:
    return RaindropBookmarkResult(success=False, bookmark_id=None, error=error)


def bookmark_dedup_key(normalized_url: str) -> str:
    return f"{BOOKMARK_DEDUP_KEY_PREFIX}{normalized_url}"


def normalize_bookmark_url(url: str) -> str:
    """Stable identity for a Raindrop bookmark.

    YouTube watch / youtu.be / shorts / m. collapse to
    ``https://www.youtube.com/watch?v=<id>``. Tracking params (``si``,
    ``is``, ``utm_*``), fragments, and a trailing slash are stripped;
    scheme and host are lowercased.
    """
    text = (url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in _TRACKING_QUERY_KEYS
        and not key.casefold().startswith("utm_")
    ]
    cleaned = urlunparse(parsed._replace(query=urlencode(kept, doseq=True), fragment=""))
    try:
        from services.obsidian.add_youtube_link import _extract_video_id, is_valid_youtube_url

        if is_valid_youtube_url(cleaned):
            video_id = _extract_video_id(cleaned)
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"
    except Exception:
        logger.debug("YouTube bookmark normalize skipped for %s", cleaned[:80], exc_info=True)

    parsed = urlparse(cleaned)
    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()
    path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def _youtube_video_id(url: str) -> str | None:
    try:
        from services.obsidian.add_youtube_link import _extract_video_id, is_valid_youtube_url

        text = (url or "").strip()
        if text and is_valid_youtube_url(text):
            return _extract_video_id(text)
    except Exception:
        logger.debug("YouTube video id skipped for %s", (url or "")[:80], exc_info=True)
    return None


def _search_terms(url: str, normalized: str) -> list[str]:
    """One or two Raindrop ``link:`` queries covering stored URL variants."""
    terms: list[str] = []
    video_id = _youtube_video_id(normalized) or _youtube_video_id(url)
    if video_id:
        terms.append(video_id)
    for candidate in (normalized, (url or "").strip()):
        if candidate and candidate not in terms:
            terms.append(candidate)
    return terms[:2]


def _stored_id(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or text == "1":
        return None
    return text


def _claim_bookmark_slot(normalized: str, redis_conn) -> bool:
    """SET NX the 90-day key. True = this call may search/POST. Redis errors fail open."""
    try:
        return bool(
            redis_conn.set(
                bookmark_dedup_key(normalized),
                "1",
                nx=True,
                ex=BOOKMARK_DEDUP_TTL_SECONDS,
            )
        )
    except Exception:
        logger.exception(
            "Raindrop dedupe claim failed url=%s; continuing to search/POST",
            normalized[:120],
        )
        return True


def _remember_bookmark(normalized: str, bookmark_id: str | None, redis_conn) -> None:
    value = (bookmark_id or "").strip() or "1"
    try:
        redis_conn.set(
            bookmark_dedup_key(normalized),
            value,
            ex=BOOKMARK_DEDUP_TTL_SECONDS,
        )
    except Exception:
        logger.exception(
            "Raindrop dedupe store failed url=%s",
            normalized[:120],
        )


def _release_bookmark_slot(normalized: str, redis_conn) -> None:
    try:
        redis_conn.delete(bookmark_dedup_key(normalized))
    except Exception:
        logger.exception(
            "Raindrop dedupe release failed url=%s",
            normalized[:120],
        )


def _item_matches(item: dict, url: str, normalized: str) -> bool:
    link = (item or {}).get("link") or ""
    if not link:
        return False
    item_norm = normalize_bookmark_url(link)
    if item_norm and item_norm == normalized:
        return True
    want_id = _youtube_video_id(normalized) or _youtube_video_id(url)
    have_id = _youtube_video_id(link)
    return bool(want_id and have_id and want_id == have_id)


def find_bookmark_by_link(
    url: str,
    normalized: str | None = None,
    *,
    client: httpx.Client | None = None,
) -> dict | None:
    """Return an existing Raindrop item whose link matches ``url``, or None."""
    normalized = normalized or normalize_bookmark_url(url)
    if not normalized:
        return None
    owns_client = client is None
    http = client or httpx.Client(timeout=10.0)
    try:
        for term in _search_terms(url, normalized):
            response = http.get(
                f"{RAINDROP_API_BASE}/raindrops/{ALL_COLLECTIONS_ID}",
                headers=_get_headers(),
                params={"search": f'link:"{term}"', "perpage": 5},
            )
            if response.status_code != 200:
                logger.info(
                    "Raindrop search returned %s for %s",
                    response.status_code,
                    term[:80],
                )
                continue
            for item in response.json().get("items") or []:
                if _item_matches(item, url, normalized):
                    return item
        return None
    finally:
        if owns_client:
            http.close()


def create_bookmark(
    url: str,
    title: str | None = None,
    excerpt: str | None = None,
    *,
    redis_conn=None,
) -> RaindropBookmarkResult:
    """Save a URL to Raindrop.io unsorted collection.

    Dedupes by normalized URL: Redis SET NX (90 days) before POST, then a
    Raindrop ``link:`` search if this process has no key (covers bookmarks
    made before the cache existed). ``/share/link``, ``/share/youtube``,
    and the Reader webhook all call this function.

    Args:
        url: The URL to bookmark
        title: Optional title for the bookmark. Raindrop.io will fetch one if omitted.
        excerpt: Optional description/excerpt for the bookmark.

    Returns:
        RaindropBookmarkResult with success status, bookmark_id, and error message
    """
    token = _get_access_token()
    if not token:
        return _err("RAINDROP_IO_TEST_TOKEN not set in environment")

    normalized = normalize_bookmark_url(url) or (url or "").strip()
    if not normalized:
        return _err("url is empty")

    redis_conn = redis_client if redis_conn is None else redis_conn
    if not _claim_bookmark_slot(normalized, redis_conn):
        existing_id = None
        try:
            existing_id = _stored_id(redis_conn.get(bookmark_dedup_key(normalized)))
        except Exception:
            logger.exception(
                "Raindrop dedupe read failed url=%s",
                normalized[:120],
            )
        logger.info("Raindrop skip (already claimed): %s", normalized[:100])
        return _ok(existing_id)

    body: dict = {
        "link": url,
        "collectionId": UNSORTED_COLLECTION_ID,
        "tags": ["obsidian-share"],
    }
    if title:
        body["title"] = title
    if excerpt:
        body["excerpt"] = excerpt

    try:
        with httpx.Client(timeout=10.0) as client:
            try:
                existing = find_bookmark_by_link(url, normalized, client=client)
            except httpx.RequestError as exc:
                logger.info("Raindrop search failed; continuing to POST: %s", exc)
                existing = None
            if existing is not None:
                bookmark_id = str(existing.get("_id") or "") or None
                _remember_bookmark(normalized, bookmark_id, redis_conn)
                logger.info(
                    "Raindrop skip (already exists): id=%s url=%s",
                    bookmark_id,
                    normalized[:100],
                )
                return _ok(bookmark_id)

            response = client.post(
                f"{RAINDROP_API_BASE}/raindrop",
                headers=_get_headers(),
                json=body,
            )

            if response.status_code != 200:
                error_text = response.text
                logger.error(
                    "Failed to create Raindrop.io bookmark: %s %s",
                    response.status_code,
                    error_text,
                )
                _release_bookmark_slot(normalized, redis_conn)
                return _err(
                    f"Raindrop.io API returned status {response.status_code}: {error_text}"
                )

            data = response.json()
            bookmark_id = str(data.get("item", {}).get("_id", ""))
            _remember_bookmark(normalized, bookmark_id, redis_conn)
            logger.info(
                "Created Raindrop.io bookmark: id=%s url=%s", bookmark_id, url[:100]
            )
            return _ok(bookmark_id)

    except httpx.RequestError as e:
        _release_bookmark_slot(normalized, redis_conn)
        logger.error("Failed to connect to Raindrop.io: %s", e)
        return _err(f"Failed to connect to Raindrop.io: {e}")
    except Exception:
        _release_bookmark_slot(normalized, redis_conn)
        raise


def mirror_to_raindrop(url: str, title: str | None, excerpt: str | None = None) -> None:
    """Save a bookmark to Raindrop.io unsorted. Logs errors but never raises.

    Used by ``/share/link``, ``/share/youtube``, and Reader document KH writes.
    Missing ``RAINDROP_IO_TEST_TOKEN`` and duplicate-URL rejections are logged
    and skipped.
    """
    try:
        raindrop_result = create_bookmark(url, title, excerpt)
        if raindrop_result["success"]:
            logger.info(
                "Mirrored to Raindrop.io: %s (id=%s)", url[:100], raindrop_result["bookmark_id"]
            )
        else:
            logger.error(
                "Failed to mirror to Raindrop.io: %s - %s", url[:100], raindrop_result["error"]
            )
    except Exception as e:
        logger.error("Unexpected error mirroring to Raindrop.io: %s - %s", url[:100], e)
