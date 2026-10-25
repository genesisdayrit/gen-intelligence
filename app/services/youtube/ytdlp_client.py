"""YouTube metadata + transcripts via yt-dlp, routed through the proxy pool.

A bot-walled Oxylabs port often exits successfully with title/channel only
(no formats, no duration) and a ``Sign in to confirm you're not a bot``
warning. That is a failure: bench the port and fail over.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
import yt_dlp

from services.youtube.proxy_pool import (
    ProxyEndpoint,
    ProxyPool,
    classify_error,
    default_pool,
)

logger = logging.getLogger(__name__)

_EXTRACT_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 16

_BOT_WALL_RE = re.compile(r"sign in to confirm|not a bot", re.IGNORECASE)
_CAPTION_EXTS = ("json3", "vtt", "srv1")
_CAPTION_HEADERS = {"User-Agent": "Mozilla/5.0"}
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


class YtdlpError(Exception):
    """yt-dlp extract or caption fetch failed."""

    def __init__(self, message: str, error_class: str | None = None) -> None:
        super().__init__(message)
        self.error_class = error_class or classify_error(message)


@dataclass(frozen=True)
class CaptionTrack:
    source: str
    lang: str
    ext: str
    url: str


@dataclass(frozen=True)
class VideoPayload:
    video_id: str
    metadata: dict
    transcript: str | None
    proxy_url: str | None = None


_CACHE: OrderedDict[str, VideoPayload] = OrderedDict()


class _CollectingLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, msg: object) -> None:
        self.messages.append(str(msg))

    def info(self, msg: object) -> None:
        self.messages.append(str(msg))

    def warning(self, msg: object) -> None:
        self.messages.append(str(msg))

    def error(self, msg: object) -> None:
        self.messages.append(str(msg))


def reset_video_cache() -> None:
    """Drop the in-process video cache (tests)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def is_bot_walled(info: dict | None, messages: Sequence[str] | None = None) -> bool:
    """True when yt-dlp returned the soft bot-wall (or the warning)."""
    for message in messages or ():
        if _BOT_WALL_RE.search(str(message)):
            return True
    if not info:
        return False
    formats = info.get("formats")
    return (not formats) and info.get("duration") is None


def map_metadata(info: dict) -> dict:
    """Hub fields (title / author / description) plus useful extras."""
    return {
        "title": info.get("title"),
        "author_name": info.get("channel") or info.get("uploader"),
        "description": info.get("description"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "upload_date": info.get("upload_date"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "chapters": info.get("chapters"),
    }


def _track_format(tracks: Sequence[dict] | None) -> tuple[str, str] | None:
    by_ext = {
        item.get("ext"): item.get("url")
        for item in tracks or ()
        if item.get("ext") and item.get("url")
    }
    for ext in _CAPTION_EXTS:
        url = by_ext.get(ext)
        if url:
            return ext, url
    return None


def pick_caption_track(info: dict) -> CaptionTrack | None:
    """Manual English, then auto ``en-orig``, then ``en`` / other ``en-*``."""
    subtitles = info.get("subtitles") or {}
    manual_keys = [key for key in subtitles if key == "en" or key.startswith("en-")]
    manual_keys.sort(key=lambda key: (key != "en", key))
    for lang in manual_keys:
        found = _track_format(subtitles.get(lang))
        if found:
            return CaptionTrack("manual", lang, found[0], found[1])

    automatic = info.get("automatic_captions") or {}
    auto_keys: list[str] = []
    for preferred in ("en-orig", "en"):
        if preferred in automatic:
            auto_keys.append(preferred)
    auto_keys.extend(
        sorted(
            key
            for key in automatic
            if (key == "en" or key.startswith("en-")) and key not in auto_keys
        )
    )
    for lang in auto_keys:
        found = _track_format(automatic.get(lang))
        if found:
            return CaptionTrack("auto", lang, found[0], found[1])
    return None


def flatten_transcript(body: str, ext: str) -> str:
    """json3 / vtt / srv1 caption payloads → a single plain-text string."""
    if ext == "json3":
        data = json.loads(body)
        pieces: list[str] = []
        for event in data.get("events") or []:
            text = "".join(seg.get("utf8", "") for seg in event.get("segs") or [])
            if text.strip():
                pieces.append(text.strip())
        return re.sub(r"\s+", " ", " ".join(pieces)).strip()

    lines: list[str] = []
    for raw in body.splitlines():
        if "-->" in raw:
            continue
        if raw.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if not raw.strip() or raw.strip().isdigit():
            continue
        cleaned = re.sub(r"<[^>]+>", "", raw).strip()
        if cleaned and (not lines or lines[-1] != cleaned):
            lines.append(cleaned)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def extract_info(video_id: str, proxy: str | None) -> tuple[dict | None, list[str]]:
    """One yt-dlp extract (``skip_download``, equivalent to ``-J``). Serialized."""
    url = WATCH_URL.format(video_id=video_id)
    collector = _CollectingLogger()
    opts: dict = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": False,
        "ignore_no_formats_error": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "cachedir": False,
        "noprogress": True,
        "logger": collector,
    }
    if proxy:
        opts["proxy"] = proxy
    with _EXTRACT_LOCK:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    return info, collector.messages


def fetch_caption_body(url: str, proxy: str | None, timeout: float = 30.0) -> str:
    client_kwargs: dict = {"timeout": timeout, "follow_redirects": True}
    if proxy:
        client_kwargs["proxy"] = proxy
    with httpx.Client(**client_kwargs) as client:
        response = client.get(url, headers=_CAPTION_HEADERS)
        response.raise_for_status()
        return response.text


def _log_try(
    pool: ProxyPool,
    label: str,
    ok: bool,
    latency: float,
    error: object | None = None,
) -> str | None:
    error_class = None
    redacted = None
    if error is not None:
        redacted = pool.redact(error)
        error_class = getattr(error, "error_class", None) or classify_error(redacted)
    logger.info(
        "yt-dlp try port=%s outcome=%s latency=%.2fs error_class=%s",
        label,
        "ok" if ok else "fail",
        latency,
        error_class or "-",
    )
    if redacted and not ok:
        logger.info("yt-dlp try port=%s error=%s", label, redacted)
    return error_class


def _raise_if_bot_walled(info: dict | None, messages: Sequence[str]) -> dict:
    joined = "\n".join(str(m) for m in messages)
    if info is None:
        raise YtdlpError(joined or "yt-dlp returned no info")
    if is_bot_walled(info, messages):
        raise YtdlpError(
            "Sign in to confirm you're not a bot (no formats and no duration)",
            error_class="bot_wall",
        )
    return info


def _extract_via_pool(
    video_id: str,
    pool: ProxyPool,
    rng: random.Random | None = None,
) -> tuple[dict, str | None, str]:
    last_error: Exception | None = None
    for endpoint in pool.iter_attempts(rng=rng):
        proxy = endpoint.url if endpoint else None
        label = endpoint.label if endpoint else "direct"
        started = time.monotonic()
        try:
            info, messages = extract_info(video_id, proxy)
            info = _raise_if_bot_walled(info, messages)
        except Exception as exc:
            last_error = exc
            _log_try(pool, label, False, time.monotonic() - started, exc)
            if endpoint is not None:
                pool.bench(endpoint)
            continue
        _log_try(pool, label, True, time.monotonic() - started)
        return info, proxy, label
    if last_error:
        raise last_error
    raise YtdlpError("proxy pool exhausted")


def _caption_endpoints(
    pool: ProxyPool,
    preferred: str | None,
    rng: random.Random | None = None,
) -> list[ProxyEndpoint | None]:
    if not pool.endpoints:
        return [None]
    chooser = rng if rng is not None else random
    preferred_ep = pool.endpoint_for_url(preferred)
    rest = [ep for ep in pool.endpoints if ep.url != preferred]
    now = time.monotonic()
    available = [ep for ep in rest if not pool.is_benched(ep, now)]
    benched = [ep for ep in rest if pool.is_benched(ep, now)]
    chooser.shuffle(available)
    order: list[ProxyEndpoint | None] = []
    if preferred_ep is not None:
        order.append(preferred_ep)
    elif preferred is None:
        pass
    order.extend(available)
    if not available:
        chooser.shuffle(benched)
        order.extend(benched)
    return order


def _fetch_transcript_text(
    track: CaptionTrack,
    pool: ProxyPool,
    preferred_proxy: str | None,
    rng: random.Random | None = None,
) -> str | None:
    last_error: Exception | None = None
    for endpoint in _caption_endpoints(pool, preferred_proxy, rng=rng):
        proxy = endpoint.url if endpoint else None
        label = endpoint.label if endpoint else "direct"
        started = time.monotonic()
        try:
            body = fetch_caption_body(track.url, proxy)
            text = flatten_transcript(body, track.ext)
            if not text:
                raise YtdlpError(f"empty {track.ext} caption body")
        except Exception as exc:
            last_error = exc
            _log_try(pool, label, False, time.monotonic() - started, exc)
            if endpoint is not None:
                pool.bench(endpoint)
            continue
        _log_try(pool, label, True, time.monotonic() - started)
        logger.info(
            "yt-dlp caption track=%s:%s:%s port=%s",
            track.source,
            track.lang,
            track.ext,
            label,
        )
        return text
    if last_error:
        logger.info(
            "yt-dlp caption unavailable after pool failover: %s",
            pool.redact(last_error),
        )
    return None


def _cache_get(video_id: str) -> VideoPayload | None:
    with _CACHE_LOCK:
        payload = _CACHE.get(video_id)
        if payload is not None:
            _CACHE.move_to_end(video_id)
        return payload


def _cache_put(video_id: str, payload: VideoPayload) -> None:
    with _CACHE_LOCK:
        _CACHE[video_id] = payload
        _CACHE.move_to_end(video_id)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


def fetch_video(
    video_id: str,
    *,
    pool: ProxyPool | None = None,
    rng: random.Random | None = None,
    use_cache: bool = True,
) -> VideoPayload | None:
    """Metadata + transcript from one yt-dlp extract on one successful endpoint."""
    if use_cache:
        cached = _cache_get(video_id)
        if cached is not None:
            return cached
    pool = pool if pool is not None else default_pool()
    try:
        info, proxy, _label = _extract_via_pool(video_id, pool, rng=rng)
    except Exception as exc:
        logger.info("yt-dlp metadata failed for %s: %s", video_id, pool.redact(exc))
        return None
    metadata = map_metadata(info)
    transcript = None
    track = pick_caption_track(info)
    if track:
        transcript = _fetch_transcript_text(track, pool, proxy, rng=rng)
    else:
        logger.info("yt-dlp no English caption track for %s", video_id)
    payload = VideoPayload(
        video_id=video_id,
        metadata=metadata,
        transcript=transcript,
        proxy_url=proxy,
    )
    if use_cache:
        _cache_put(video_id, payload)
    return payload


def fetch_metadata(
    video_id: str,
    *,
    pool: ProxyPool | None = None,
    rng: random.Random | None = None,
) -> dict | None:
    payload = fetch_video(video_id, pool=pool, rng=rng)
    if payload is None:
        return None
    return payload.metadata


def fetch_transcript(
    video_id: str,
    *,
    pool: ProxyPool | None = None,
    rng: random.Random | None = None,
) -> str | None:
    payload = fetch_video(video_id, pool=pool, rng=rng)
    if payload is None:
        return None
    transcript = payload.transcript
    if transcript is None:
        return None
    transcript = str(transcript).strip()
    return transcript or None
