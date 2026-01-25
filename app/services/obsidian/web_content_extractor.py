"""Web content extraction for shared links using trafilatura."""

import logging

import httpx
import trafilatura

logger = logging.getLogger(__name__)

MAX_CONTENT_LENGTH = 50000  # 50K character limit for extracted text
DEFAULT_TIMEOUT = 15.0


def fetch_web_content(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Fetch and extract article content from a web page.

    Uses trafilatura for high-quality article extraction with boilerplate removal.

    Args:
        url: The URL to fetch and extract content from.
        timeout: HTTP request timeout in seconds.

    Returns:
        dict with keys:
            - title: str | None
            - author: str | None
            - date: str | None
            - body_text: str | None
    """
    result = {"title": None, "author": None, "date": None, "body_text": None}

    try:
        # Fetch HTML using httpx (consistent with existing patterns)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )

            if response.status_code != 200:
                logger.warning(
                    "Page returned %s for %s", response.status_code, url[:100]
                )
                return result

            html = response.text

        # Extract main content using trafilatura
        extracted = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            favor_recall=True,
            output_format="txt",
        )

        if extracted:
            # Truncate if too long
            if len(extracted) > MAX_CONTENT_LENGTH:
                extracted = extracted[:MAX_CONTENT_LENGTH] + "\n\n[Content truncated...]"
            result["body_text"] = extracted

        # Extract metadata separately
        metadata = trafilatura.extract_metadata(html, default_url=url)
        if metadata:
            result["title"] = metadata.title
            result["author"] = metadata.author
            result["date"] = metadata.date

    except httpx.RequestError as e:
        logger.warning("Failed to fetch content from %s: %s", url[:100], e)
    except Exception as e:
        logger.warning("Unexpected error extracting content from %s: %s", url[:100], e)

    return result
