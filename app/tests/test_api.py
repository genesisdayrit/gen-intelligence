"""API endpoint tests."""

import os
import sys
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set required env vars before importing app
os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


# Mock responses for share services to prevent creating actual files
def mock_add_shared_link(url, title=None):
    """Mock that returns success without creating files."""
    return {"success": True, "action": "created", "error": None, "file_path": "test.md", "vault_name": "test"}


def mock_add_youtube_link(url):
    """Mock that returns success without creating files."""
    return {"success": True, "action": "created", "error": None}


def test_health_endpoint():
    """Health check returns healthy status."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_webhook_requires_auth():
    """Webhook endpoint rejects requests without valid secret."""
    response = client.post("/telegram/webhook", json={"update_id": 123})
    assert response.status_code == 401


def test_webhook_with_valid_secret():
    """Webhook accepts requests with valid secret and ignores non-channel posts."""
    response = client.post(
        "/telegram/webhook",
        json={"update_id": 123},
        headers={"X-Telegram-Bot-API-Secret-Token": "test-secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


# Link sharing endpoint tests
def test_share_link_requires_api_key():
    """Share link endpoint rejects requests without API key."""
    response = client.post(
        "/share/link",
        json={"url": "https://example.com"},
    )
    assert response.status_code == 401


def test_share_link_rejects_invalid_key():
    """Share link endpoint rejects requests with invalid API key."""
    response = client.post(
        "/share/link",
        json={"url": "https://example.com"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert response.status_code == 401


@patch("main.add_shared_link", mock_add_shared_link)
def test_share_link_accepts_valid_request():
    """Share link endpoint accepts request with valid API key and returns 202."""
    response = client.post(
        "/share/link",
        json={"url": "https://example.com", "title": "Example"},
        headers={"X-API-Key": "test-link-api-key"},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"


@patch("main.add_shared_link", mock_add_shared_link)
def test_share_link_accepts_without_title():
    """Share link endpoint accepts request without title."""
    response = client.post(
        "/share/link",
        json={"url": "https://example.com/page"},
        headers={"X-API-Key": "test-link-api-key"},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"


def test_share_link_requires_url():
    """Share link endpoint requires url field."""
    response = client.post(
        "/share/link",
        json={"title": "Missing URL"},
        headers={"X-API-Key": "test-link-api-key"},
    )
    assert response.status_code == 422


# YouTube sharing endpoint tests
def test_share_youtube_requires_api_key():
    """Share YouTube endpoint rejects requests without API key."""
    response = client.post(
        "/share/youtube",
        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert response.status_code == 401


def test_share_youtube_rejects_invalid_key():
    """Share YouTube endpoint rejects requests with invalid API key."""
    response = client.post(
        "/share/youtube",
        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert response.status_code == 401


@patch("main.add_youtube_link", mock_add_youtube_link)
def test_share_youtube_accepts_valid_request():
    """Share YouTube endpoint accepts request with valid API key and returns 202."""
    response = client.post(
        "/share/youtube",
        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        headers={"X-API-Key": "test-link-api-key"},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"


def test_share_youtube_rejects_non_youtube_url():
    """Share YouTube endpoint rejects non-YouTube URLs with 422."""
    response = client.post(
        "/share/youtube",
        json={"url": "https://example.com/video"},
        headers={"X-API-Key": "test-link-api-key"},
    )
    assert response.status_code == 422


@patch("main.add_youtube_link", mock_add_youtube_link)
def test_share_youtube_accepts_short_url():
    """Share YouTube endpoint accepts youtu.be short URLs."""
    response = client.post(
        "/share/youtube",
        json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        headers={"X-API-Key": "test-link-api-key"},
    )
    assert response.status_code == 202


@patch("main.add_youtube_link", mock_add_youtube_link)
def test_share_youtube_accepts_shorts_url():
    """Share YouTube endpoint accepts YouTube Shorts URLs."""
    response = client.post(
        "/share/youtube",
        json={"url": "https://www.youtube.com/shorts/dQw4w9WgXcQ"},
        headers={"X-API-Key": "test-link-api-key"},
    )
    assert response.status_code == 202


@patch("main.add_youtube_link", mock_add_youtube_link)
def test_share_youtube_accepts_mobile_url():
    """Share YouTube endpoint accepts mobile YouTube URLs."""
    response = client.post(
        "/share/youtube",
        json={"url": "https://m.youtube.com/watch?v=dQw4w9WgXcQ"},
        headers={"X-API-Key": "test-link-api-key"},
    )
    assert response.status_code == 202


@patch("main.add_youtube_link", mock_add_youtube_link)
def test_share_youtube_accepts_embed_url():
    """Share YouTube endpoint accepts embed URLs."""
    response = client.post(
        "/share/youtube",
        json={"url": "https://www.youtube.com/embed/dQw4w9WgXcQ"},
        headers={"X-API-Key": "test-link-api-key"},
    )
    assert response.status_code == 202


def test_share_youtube_requires_url():
    """Share YouTube endpoint requires url field."""
    response = client.post(
        "/share/youtube",
        json={},
        headers={"X-API-Key": "test-link-api-key"},
    )
    assert response.status_code == 422
