"""yt-dlp client: bot-wall, captions, flattening, oEmbed fallback."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.obsidian.add_youtube_link import fetch_youtube_metadata
from services.youtube.proxy_pool import ProxyEndpoint, ProxyPool, build_proxy_url
from services.youtube.ytdlp_client import (
    flatten_transcript,
    fetch_video,
    is_bot_walled,
    map_metadata,
    pick_caption_track,
    reset_video_cache,
)


class FirstChoice:
    def choice(self, seq):
        return seq[0]

    def shuffle(self, seq):
        return None


def _pool(*ports: int, password: str = "s3cret") -> ProxyPool:
    endpoints = [
        ProxyEndpoint(
            url=build_proxy_url("isp.oxylabs.io", "user", password, port),
            label=str(port),
        )
        for port in ports
    ]
    return ProxyPool(endpoints, secrets=["user", password, "s3cret"])


def _ok_info(**overrides) -> dict:
    info = {
        "title": "Cool Video",
        "channel": "A Channel",
        "uploader": "Uploader",
        "description": "A description",
        "duration": 125,
        "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
        "upload_date": "20260119",
        "channel_url": "https://www.youtube.com/@achannel",
        "formats": [{"url": "https://example.com/media"}],
        "subtitles": {},
        "automatic_captions": {},
    }
    info.update(overrides)
    return info


def setup_function(_fn):
    reset_video_cache()


def test_is_bot_walled_warning_or_title_only():
    assert is_bot_walled(
        {"title": "T", "channel": "C", "formats": [], "duration": None},
        ["WARNING: [youtube] Sign in to confirm you're not a bot"],
    )
    assert is_bot_walled({"title": "T", "formats": [], "duration": None}, [])
    assert is_bot_walled(
        {"title": "T", "formats": [{"url": "x"}], "duration": 10},
        ["Sign in to confirm you're not a bot"],
    )
    assert not is_bot_walled(_ok_info(), [])
    assert not is_bot_walled({"title": "T", "formats": [], "duration": 10}, [])


def test_map_metadata_uses_channel_then_uploader():
    mapped = map_metadata(_ok_info())
    assert mapped["title"] == "Cool Video"
    assert mapped["author_name"] == "A Channel"
    assert mapped["description"] == "A description"
    assert mapped["duration"] == 125
    assert mapped["thumbnail"]
    assert mapped["upload_date"] == "20260119"
    assert mapped["channel_url"] == "https://www.youtube.com/@achannel"

    mapped = map_metadata({"title": "T", "uploader": "Only Uploader"})
    assert mapped["author_name"] == "Only Uploader"


def test_pick_caption_track_prefers_manual_then_en_orig():
    manual = pick_caption_track(
        {
            "subtitles": {
                "en": [{"ext": "vtt", "url": "https://manual.vtt"}],
                "en-US": [{"ext": "json3", "url": "https://manual-us.json3"}],
            },
            "automatic_captions": {
                "en-orig": [{"ext": "json3", "url": "https://auto-orig.json3"}],
                "en": [{"ext": "json3", "url": "https://auto-en.json3"}],
            },
        }
    )
    assert manual is not None
    assert (manual.source, manual.lang, manual.ext) == ("manual", "en", "vtt")

    auto_orig = pick_caption_track(
        {
            "subtitles": {"de": [{"ext": "vtt", "url": "https://de.vtt"}]},
            "automatic_captions": {
                "en": [{"ext": "json3", "url": "https://auto-en.json3"}],
                "en-orig": [{"ext": "json3", "url": "https://auto-orig.json3"}],
                "en-US": [{"ext": "json3", "url": "https://auto-us.json3"}],
            },
        }
    )
    assert auto_orig is not None
    assert (auto_orig.source, auto_orig.lang) == ("auto", "en-orig")
    assert auto_orig.ext == "json3"

    auto_en = pick_caption_track(
        {
            "automatic_captions": {
                "en": [{"ext": "srv1", "url": "https://auto-en.srv1"}],
                "en-GB": [{"ext": "json3", "url": "https://auto-gb.json3"}],
            }
        }
    )
    assert auto_en is not None
    assert auto_en.lang == "en"

    assert pick_caption_track({"subtitles": {}, "automatic_captions": {}}) is None


def test_flatten_json3_and_vtt():
    json3 = json.dumps(
        {
            "events": [
                {"segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
                {"tStartMs": 1},
                {"segs": [{"utf8": "\nNext  line"}]},
            ]
        }
    )
    assert flatten_transcript(json3, "json3") == "Hello world Next line"

    vtt = "\n".join(
        [
            "WEBVTT",
            "Kind: captions",
            "Language: en",
            "",
            "1",
            "00:00:00.000 --> 00:00:01.000",
            "Hello <c>world</c>",
            "",
            "2",
            "00:00:01.000 --> 00:00:02.000",
            "Hello <c>world</c>",
            "Next",
        ]
    )
    assert flatten_transcript(vtt, "vtt") == "Hello world Next"


def test_fetch_video_benches_bot_wall_and_fails_over():
    pool = _pool(8001, 8002)
    calls: list[str | None] = []

    def fake_extract(video_id, proxy):
        calls.append(proxy)
        if proxy and proxy.endswith(":8001"):
            return (
                {"title": "T", "channel": "C", "formats": [], "duration": None},
                ["WARNING: Sign in to confirm you're not a bot"],
            )
        return _ok_info(), []

    with patch("services.youtube.ytdlp_client.extract_info", side_effect=fake_extract):
        payload = fetch_video("abcdefghijk", pool=pool, rng=FirstChoice(), use_cache=False)

    assert payload is not None
    assert payload.metadata["title"] == "Cool Video"
    assert payload.metadata["author_name"] == "A Channel"
    assert len(calls) == 2
    assert calls[0].endswith(":8001")
    assert calls[1].endswith(":8002")
    assert pool.is_benched(pool.endpoints[0])
    assert not pool.is_benched(pool.endpoints[1])


def test_fetch_video_empty_pool_fetches_direct():
    pool = ProxyPool([])
    with patch(
        "services.youtube.ytdlp_client.extract_info",
        return_value=(_ok_info(), []),
    ) as mock_extract:
        payload = fetch_video("abcdefghijk", pool=pool, use_cache=False)
    assert payload is not None
    mock_extract.assert_called_once_with("abcdefghijk", None)


def test_fetch_video_same_endpoint_fetches_captions():
    pool = _pool(8001)
    info = _ok_info(
        automatic_captions={
            "en-orig": [{"ext": "json3", "url": "https://captions.example/en-orig.json3"}],
        }
    )
    json3 = json.dumps({"events": [{"segs": [{"utf8": "  spoken words  "}]}]})

    with patch("services.youtube.ytdlp_client.extract_info", return_value=(info, [])), patch(
        "services.youtube.ytdlp_client.fetch_caption_body", return_value=json3
    ) as mock_caps:
        payload = fetch_video("abcdefghijk", pool=pool, rng=FirstChoice(), use_cache=False)

    assert payload is not None
    assert payload.transcript == "spoken words"
    mock_caps.assert_called_once()
    assert mock_caps.call_args.args[0] == "https://captions.example/en-orig.json3"
    assert mock_caps.call_args.args[1] == pool.endpoints[0].url


def test_caption_429_on_metadata_port_fails_over():
    pool = _pool(8001, 8002)
    info = _ok_info(
        automatic_captions={
            "en-orig": [{"ext": "json3", "url": "https://captions.example/en-orig.json3"}],
        }
    )
    json3 = json.dumps({"events": [{"segs": [{"utf8": "recovered"}]}]})
    proxies: list[str | None] = []

    def fake_caps(url, proxy, timeout=30.0):
        proxies.append(proxy)
        if proxy and proxy.endswith(":8001"):
            raise RuntimeError("HTTP 429 Too Many Requests")
        return json3

    with patch("services.youtube.ytdlp_client.extract_info", return_value=(info, [])), patch(
        "services.youtube.ytdlp_client.fetch_caption_body", side_effect=fake_caps
    ):
        payload = fetch_video("abcdefghijk", pool=pool, rng=FirstChoice(), use_cache=False)

    assert payload is not None
    assert payload.transcript == "recovered"
    assert proxies[0].endswith(":8001")
    assert proxies[1].endswith(":8002")
    assert pool.is_benched(pool.endpoints[0])


def test_fetch_video_no_track_returns_metadata_without_transcript():
    pool = _pool(8001)
    with patch(
        "services.youtube.ytdlp_client.extract_info",
        return_value=(_ok_info(), []),
    ), patch("services.youtube.ytdlp_client.fetch_caption_body") as mock_caps:
        payload = fetch_video("abcdefghijk", pool=pool, use_cache=False)
    assert payload is not None
    assert payload.transcript is None
    mock_caps.assert_not_called()


def test_fetch_video_all_ports_fail_returns_none():
    pool = _pool(8001, 8002)

    def boom(_video_id, _proxy):
        raise RuntimeError("http://user:s3cret@isp.oxylabs.io:8001 timed out")

    with patch("services.youtube.ytdlp_client.extract_info", side_effect=boom):
        payload = fetch_video("abcdefghijk", pool=pool, rng=FirstChoice(), use_cache=False)
    assert payload is None
    assert pool.is_benched(pool.endpoints[0])
    assert pool.is_benched(pool.endpoints[1])


def test_try_logs_never_include_credentials(caplog):
    pool = _pool(8001)
    with caplog.at_level("INFO", logger="services.youtube.ytdlp_client"), patch(
        "services.youtube.ytdlp_client.extract_info",
        side_effect=RuntimeError("via http://user:s3cret@isp.oxylabs.io:8001"),
    ):
        fetch_video("abcdefghijk", pool=pool, use_cache=False)
    text = caplog.text
    assert "s3cret" not in text
    assert "user:s3cret" not in text
    assert "port=8001" in text
    assert "error_class=other" in text


def test_oembed_fallback_when_ytdlp_fails():
    oembed = MagicMock()
    oembed.status_code = 200
    oembed.json.return_value = {
        "title": "OEmbed Title",
        "author_name": "OEmbed Author",
    }
    client = MagicMock()
    client.get.return_value = oembed
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    with patch(
        "services.obsidian.add_youtube_link._fetch_video_metadata_ytdlp",
        return_value=None,
    ), patch(
        "services.obsidian.add_youtube_link.httpx.Client",
        return_value=client,
    ), patch(
        "services.obsidian.add_youtube_link._fetch_youtube_description",
        return_value="scraped desc",
    ):
        result = fetch_youtube_metadata("https://www.youtube.com/watch?v=abcdefghijk")

    assert result["title"] == "OEmbed Title"
    assert result["author_name"] == "OEmbed Author"
    assert result["description"] == "scraped desc"


def test_ytdlp_success_skips_oembed():
    with patch(
        "services.obsidian.add_youtube_link._fetch_video_metadata_ytdlp",
        return_value={
            "title": "YT Title",
            "author_name": "YT Channel",
            "description": "from ytdlp",
        },
    ) as mock_ytdlp, patch(
        "services.obsidian.add_youtube_link.httpx.Client"
    ) as mock_client:
        result = fetch_youtube_metadata("https://www.youtube.com/watch?v=abcdefghijk")

    assert result["title"] == "YT Title"
    assert result["author_name"] == "YT Channel"
    mock_ytdlp.assert_called_once_with("abcdefghijk")
    mock_client.assert_called()
    mock_client.return_value.__enter__.return_value.get.assert_not_called()


def test_channel_and_playlist_skip_ytdlp():
    with patch(
        "services.obsidian.add_youtube_link._fetch_video_metadata_ytdlp"
    ) as mock_ytdlp, patch(
        "services.obsidian.add_youtube_link._fetch_channel_metadata",
        return_value={"title": "A Channel", "description": "bio"},
    ):
        channel = fetch_youtube_metadata("https://www.youtube.com/@somechannel")
    assert channel["title"] == "A Channel"
    mock_ytdlp.assert_not_called()
