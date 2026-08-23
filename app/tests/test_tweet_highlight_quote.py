"""Unit tests for the reusable tweet highlight quote cleaner."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.obsidian.add_readwise_buffet import tweet_highlight_quote as reexported
from services.obsidian.utils.tweet_highlight_quote import tweet_highlight_quote

# Aug 23, 2026 journal — two empty-alt twimg images after a t.co URL.
INTERNET_HOF_TEXT = (
    "New York is now the #1 market for tech talent, dethroning San Francisco's "
    "13-year reign (via: CNBC) https://t.co/L3eHRbgQyG "
    "![](https://pbs.twimg.com/media/HQYkftsXEAAsKdm.jpg) "
    "![](https://pbs.twimg.com/media/HQYkgifXEAAfKbH.jpg)"
)
FLOWER_ALICEE_TEXT = (
    "...but then when https://t.co/p3GyToJO6M "
    "![](https://pbs.twimg.com/media/HQWvI2ZbIAA4WBd.jpg) "
    "![](https://pbs.twimg.com/media/HQWvI2ZaoAAdqfb.jpg)"
)


def test_live_interneth0f_keeps_tco_strips_two_empty_alt_images():
    assert tweet_highlight_quote(INTERNET_HOF_TEXT) == (
        "New York is now the #1 market for tech talent, dethroning San Francisco's "
        "13-year reign (via: CNBC) https://t.co/L3eHRbgQyG"
    )


def test_live_flower_alicee_keeps_tco_strips_two_empty_alt_images():
    assert tweet_highlight_quote(FLOWER_ALICEE_TEXT) == (
        "...but then when https://t.co/p3GyToJO6M"
    )


def test_strips_markdown_image():
    assert (
        tweet_highlight_quote("Quote text ![](https://pbs.twimg.com/media/foo.jpg)")
        == "Quote text"
    )


def test_strips_html_img():
    assert (
        tweet_highlight_quote(
            'Quote text <img src="https://pbs.twimg.com/media/foo.jpg" alt="pic">'
        )
        == "Quote text"
    )


def test_strips_bare_twimg_and_pic_twitter_urls():
    assert (
        tweet_highlight_quote(
            "Quote text https://pbs.twimg.com/media/foo.jpg pic.twitter.com/abc123"
        )
        == "Quote text"
    )


def test_strips_video_twimg_url():
    assert (
        tweet_highlight_quote(
            "Quote text https://video.twimg.com/ext_tw_video/1/pu/vid/foo.mp4"
        )
        == "Quote text"
    )


def test_strips_markdown_image_on_own_line():
    assert (
        tweet_highlight_quote("Quote text\n\n![alt](https://pbs.twimg.com/media/foo.jpg)")
        == "Quote text"
    )


def test_empty_after_strip_is_none():
    assert tweet_highlight_quote("![](https://pbs.twimg.com/media/foo.jpg)") is None
    assert tweet_highlight_quote("   ") is None
    assert tweet_highlight_quote(None) is None


def test_unrelated_url_is_kept():
    text = "See https://example.com/diagram.png for the chart"
    assert tweet_highlight_quote(text) == text


def test_reexported_from_add_readwise_buffet():
    """Journal writer re-exports the helper so either import path works."""
    assert reexported is tweet_highlight_quote
