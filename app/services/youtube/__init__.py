"""YouTube metadata and transcripts via yt-dlp + a rotatable proxy pool."""

from services.youtube.proxy_pool import (
    ProxyEndpoint,
    ProxyPool,
    classify_error,
    parse_ports,
    redact_secrets,
)
from services.youtube.ytdlp_client import (
    fetch_metadata,
    fetch_transcript,
    fetch_video,
)

__all__ = [
    "ProxyEndpoint",
    "ProxyPool",
    "classify_error",
    "fetch_metadata",
    "fetch_transcript",
    "fetch_video",
    "parse_ports",
    "redact_secrets",
]
