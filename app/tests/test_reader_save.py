"""Readwise Reader document CREATE (save_document) and YouTube share wiring."""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ.setdefault("READWISE_TOKEN", "test-readwise-token")

from services.readwise.reader import (  # noqa: E402
    SAVE_URL,
    reader_permalink,
    save_document,
)


def test_reader_permalink_prefers_stable_read_path():
    assert reader_permalink("01abc") == "https://read.readwise.io/read/01abc"
    assert (
        reader_permalink(
            "01abc",
            "https://read.readwise.io/new/read/01abc",
        )
        == "https://read.readwise.io/read/01abc"
    )


def test_reader_permalink_rewrites_new_read_when_id_missing():
    assert (
        reader_permalink(None, "https://read.readwise.io/new/read/01abc")
        == "https://read.readwise.io/read/01abc"
    )


@patch.dict(os.environ, {"READWISE_TOKEN": "test-readwise-token"})
def test_save_document_posts_video_category_and_title():
    response = MagicMock()
    response.status_code = 201
    response.content = b'{"id":"01doc","url":"https://read.readwise.io/new/read/01doc"}'
    response.json.return_value = {
        "id": "01doc",
        "url": "https://read.readwise.io/new/read/01doc",
    }

    with patch("services.readwise.reader.requests.post", return_value=response) as mock_post:
        result = save_document(
            "https://www.youtube.com/watch?v=abcdefghijk",
            category="video",
            title="Cool Video",
            saved_using="gen-intelligence",
        )

    assert result["success"] is True
    assert result["id"] == "01doc"
    assert result["status_code"] == 201
    mock_post.assert_called_once()
    assert mock_post.call_args.args[0] == SAVE_URL
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Token test-readwise-token"
    assert mock_post.call_args.kwargs["json"] == {
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "category": "video",
        "title": "Cool Video",
        "saved_using": "gen-intelligence",
    }


@patch.dict(os.environ, {"READWISE_TOKEN": "test-readwise-token"})
def test_save_document_200_already_exists_is_success():
    response = MagicMock()
    response.status_code = 200
    response.content = b'{"id":"01doc","url":"https://read.readwise.io/new/read/01doc"}'
    response.json.return_value = {
        "id": "01doc",
        "url": "https://read.readwise.io/new/read/01doc",
    }

    with patch("services.readwise.reader.requests.post", return_value=response):
        result = save_document(
            "https://www.youtube.com/watch?v=abcdefghijk",
            category="video",
            title="Cool Video",
        )

    assert result["success"] is True
    assert result["status_code"] == 200
    assert result["id"] == "01doc"


@patch.dict(os.environ, {"READWISE_TOKEN": "test-readwise-token"})
def test_save_document_http_error_is_failure():
    response = MagicMock()
    response.status_code = 400
    response.content = b'{"error":"bad"}'
    response.text = '{"error":"bad"}'

    with patch("services.readwise.reader.requests.post", return_value=response):
        result = save_document("https://www.youtube.com/watch?v=abcdefghijk")

    assert result["success"] is False
    assert result["status_code"] == 400
    assert result["id"] is None


def test_save_document_missing_token_does_not_post():
    with patch.dict(os.environ, {"READWISE_TOKEN": ""}, clear=False), patch(
        "services.readwise.reader.requests.post"
    ) as mock_post:
        # Force _headers to see a missing token even if the process env has one.
        with patch("services.readwise.reader.os.getenv", return_value=None):
            result = save_document("https://www.youtube.com/watch?v=abcdefghijk")

    assert result["success"] is False
    assert "READWISE_TOKEN" in (result["error"] or "")
    mock_post.assert_not_called()


def test_process_youtube_link_calls_save_with_obsidian_stem():
    from main import _process_youtube_link

    kh = {
        "success": True,
        "action": "created",
        "title": 'Watch this: "AI" / part 1?',
        "stem": "Watch this_ AI _ part 1?",
        "file_path": "/vault/_knowledge-hub/Watch this_ AI _ part 1?.md",
        "description": None,
        "error": None,
    }
    save = {
        "success": True,
        "id": "01readerdoc",
        "url": "https://read.readwise.io/new/read/01readerdoc",
        "error": None,
        "status_code": 201,
    }

    with patch("main.add_youtube_link", return_value=kh), patch(
        "main.save_document", return_value=save
    ) as mock_save, patch(
        "main.apply_youtube_extra_frontmatter",
        return_value={"success": True, "action": "updated", "error": None},
    ) as mock_apply, patch("main._mirror_to_raindrop"):
        _process_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    mock_save.assert_called_once_with(
        "https://www.youtube.com/watch?v=abcdefghijk",
        category="video",
        title="Watch this_ AI _ part 1?",
        saved_using="gen-intelligence",
    )
    extras = mock_apply.call_args.args[1]
    assert extras["readwise_id"] == "01readerdoc"
    assert extras["readwise_url"] == "https://read.readwise.io/read/01readerdoc"


def test_process_youtube_link_200_still_writes_yaml():
    from main import _process_youtube_link

    kh = {
        "success": True,
        "action": "skipped",
        "title": "Cool Video",
        "stem": "Cool Video",
        "file_path": "/vault/_knowledge-hub/Cool Video.md",
        "description": None,
        "error": None,
    }
    save = {
        "success": True,
        "id": "01readerdoc",
        "url": "https://read.readwise.io/new/read/01readerdoc",
        "error": None,
        "status_code": 200,
    }

    with patch("main.add_youtube_link", return_value=kh), patch(
        "main.save_document", return_value=save
    ), patch(
        "main.apply_youtube_extra_frontmatter",
        return_value={"success": True, "action": "updated", "error": None},
    ) as mock_apply, patch("main._mirror_to_raindrop") as mock_raindrop:
        _process_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    mock_raindrop.assert_not_called()
    mock_apply.assert_called_once()
    assert mock_apply.call_args.args[1]["readwise_url"] == (
        "https://read.readwise.io/read/01readerdoc"
    )


def test_process_youtube_link_save_failure_still_reports_kh_success():
    from main import _process_youtube_link

    kh = {
        "success": True,
        "action": "created",
        "title": "Cool Video",
        "stem": "Cool Video",
        "file_path": "/vault/_knowledge-hub/Cool Video.md",
        "description": None,
        "error": None,
    }

    with patch("main.add_youtube_link", return_value=kh) as mock_kh, patch(
        "main.save_document",
        return_value={
            "success": False,
            "id": None,
            "url": None,
            "error": "Reader down",
            "status_code": 503,
        },
    ), patch("main.apply_youtube_extra_frontmatter") as mock_apply, patch(
        "main._mirror_to_raindrop"
    ) as mock_raindrop:
        _process_youtube_link("https://www.youtube.com/watch?v=abcdefghijk")

    mock_kh.assert_called_once()
    mock_raindrop.assert_called_once()
    mock_apply.assert_not_called()


def test_process_shared_link_does_not_save_to_reader():
    from main import _process_shared_link

    with patch(
        "main.add_shared_link",
        return_value={"success": True, "action": "created", "error": None},
    ), patch("main.save_document") as mock_save, patch("main._mirror_to_raindrop"):
        _process_shared_link("https://example.com/article", "My Article")

    mock_save.assert_not_called()
