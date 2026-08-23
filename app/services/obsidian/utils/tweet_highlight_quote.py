"""Strip image embeds from Readwise tweet highlight quotes."""

import re

# Live Readwise tweet highlights (Aug 23, 2026 journal): one or more
# space-separated empty-alt media images, usually after a t.co URL.
# Keep the t.co link; only the ![]() markup goes.
_EMPTY_ALT_TWIMG_MEDIA = re.compile(
    r"(?:!\[\]\(\s*https?://pbs\.twimg\.com/media/[^)\s]+\s*\)(?:\s+)?)+",
    re.IGNORECASE,
)
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_HTML_IMG_CLOSE = re.compile(r"</img\s*>", re.IGNORECASE)
_TWEET_MEDIA_URL = re.compile(
    r"(?:https?://)?(?:pbs\.twimg\.com|pic\.twitter\.com|video\.twimg\.com)"
    r"/[^\s<>)\]'\"]+",
    re.IGNORECASE,
)


def tweet_highlight_quote(text: object) -> str | None:
    """Tweet highlight text → quote without markdown/HTML/twimg image embeds.

    Import this from ``services.obsidian.utils.tweet_highlight_quote``. The
    journal Content Buffet tweet line uses it; Tweets-from-@handle notes
    should call the same helper.

    Primary live pattern: one or more space-separated empty-alt
    ``![](https://pbs.twimg.com/media/....jpg)`` after a ``https://t.co/...``
    URL. Also strips named-alt ``![foo](url)``, HTML ``<img>``, and bare
    ``pbs.twimg.com`` / ``pic.twitter.com`` / ``video.twimg.com`` URLs.
    Keeps t.co links. Collapses leftover whitespace. Returns None when
    nothing usable remains (empty input or image-only).
    """
    if text is None:
        return None
    raw = str(text)
    if not raw.strip():
        return None
    cleaned = _EMPTY_ALT_TWIMG_MEDIA.sub("", raw)
    cleaned = _MARKDOWN_IMAGE.sub("", cleaned)
    cleaned = _HTML_IMG_TAG.sub("", cleaned)
    cleaned = _HTML_IMG_CLOSE.sub("", cleaned)
    cleaned = _TWEET_MEDIA_URL.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None
