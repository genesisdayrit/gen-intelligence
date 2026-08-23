"""Unit tests for the reusable tweet highlight quote cleaner."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.obsidian.add_readwise_buffet import tweet_highlight_quote as reexported
from services.obsidian.utils.tweet_highlight_quote import tweet_highlight_quote


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
