"""API endpoint tests."""

import base64
import hashlib
import os
import sys
import time
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set required env vars before importing app
os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")

from cryptography.hazmat.primitives import hashes as crypto_hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as crypto_padding
from cryptography.hazmat.primitives.asymmetric import rsa as rsa_gen
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

# Generate a test RSA key pair for Manus webhook tests
_test_private_key = rsa_gen.generate_private_key(public_exponent=65537, key_size=2048)
_test_public_key_pem = _test_private_key.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()


def _sign_manus_payload(payload: bytes, url: str, timestamp: str) -> str:
    """Create a valid Manus RSA-SHA256 signature for testing."""
    body_sha256 = hashlib.sha256(payload).hexdigest()
    signed_content = f"{timestamp}.{url}.{body_sha256}"
    signature = _test_private_key.sign(
        signed_content.encode(),
        crypto_padding.PKCS1v15(),
        crypto_hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


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


# Manus webhook tests
def test_manus_webhook_verification_ping():
    """Manus webhook accepts verification pings (no signature headers)."""
    response = client.post("/manus/webhook", json={"event_type": "task_created"})
    assert response.status_code == 200
    assert response.json() == {"status": "received"}


def test_manus_webhook_partial_headers():
    """Manus webhook rejects requests with only one signature header."""
    response = client.post(
        "/manus/webhook",
        json={"event_type": "task_created"},
        headers={"X-Webhook-Signature": "some-sig"},
    )
    assert response.status_code == 401


@patch("main.fetch_manus_public_key", return_value=_test_public_key_pem)
def test_manus_webhook_expired_timestamp(mock_fetch):
    """Manus webhook rejects requests with expired timestamp."""
    payload = b'{"event_type": "task_created", "task_id": "123"}'
    old_timestamp = str(int(time.time()) - 600)  # 10 minutes ago
    url = "http://testserver/manus/webhook"
    sig = _sign_manus_payload(payload, url, old_timestamp)

    response = client.post(
        "/manus/webhook",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig,
            "X-Webhook-Timestamp": old_timestamp,
        },
    )
    assert response.status_code == 401
    assert "Timestamp expired" in response.json()["detail"]


@patch("main.fetch_manus_public_key", return_value=_test_public_key_pem)
def test_manus_webhook_invalid_signature(mock_fetch):
    """Manus webhook rejects requests with invalid signature."""
    payload = b'{"event_type": "task_created", "task_id": "123"}'
    timestamp = str(int(time.time()))

    response = client.post(
        "/manus/webhook",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": "aW52YWxpZHNpZ25hdHVyZQ==",
            "X-Webhook-Timestamp": timestamp,
        },
    )
    assert response.status_code == 401
    assert "Invalid signature" in response.json()["detail"]


@patch("main.fetch_manus_public_key", return_value=_test_public_key_pem)
def test_manus_webhook_valid_task_created(mock_fetch):
    """Manus webhook accepts valid task_created event."""
    payload = b'{"event_type": "task_created", "task_id": "abc-123"}'
    timestamp = str(int(time.time()))
    url = "http://testserver/manus/webhook"
    sig = _sign_manus_payload(payload, url, timestamp)

    response = client.post(
        "/manus/webhook",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig,
            "X-Webhook-Timestamp": timestamp,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "received"}


@patch("main.fetch_manus_public_key", return_value=_test_public_key_pem)
def test_manus_webhook_valid_task_progress(mock_fetch):
    """Manus webhook accepts valid task_progress event."""
    payload = b'{"event_type": "task_progress", "task_id": "abc-123", "progress": {"step": 3, "total": 10}}'
    timestamp = str(int(time.time()))
    url = "http://testserver/manus/webhook"
    sig = _sign_manus_payload(payload, url, timestamp)

    response = client.post(
        "/manus/webhook",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig,
            "X-Webhook-Timestamp": timestamp,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "received"}


@patch("main.fetch_manus_public_key", return_value=_test_public_key_pem)
def test_manus_webhook_valid_task_stopped(mock_fetch):
    """Manus webhook accepts valid task_stopped event."""
    payload = b'{"event_type": "task_stopped", "task_id": "abc-123", "status": "completed"}'
    timestamp = str(int(time.time()))
    url = "http://testserver/manus/webhook"
    sig = _sign_manus_payload(payload, url, timestamp)

    response = client.post(
        "/manus/webhook",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig,
            "X-Webhook-Timestamp": timestamp,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "received"}


@patch("main.fetch_manus_public_key", side_effect=Exception("Network error"))
def test_manus_webhook_key_fetch_failure(mock_fetch):
    """Manus webhook returns 500 when public key fetch fails."""
    payload = b'{"event_type": "task_created", "task_id": "123"}'
    timestamp = str(int(time.time()))

    response = client.post(
        "/manus/webhook",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": "dW51c2Vk",
            "X-Webhook-Timestamp": timestamp,
        },
    )
    assert response.status_code == 500


@patch("main.verify_manus_signature", return_value=True)
@patch("main.fetch_manus_public_key", return_value="unused")
@patch("main._is_manus_timestamp_valid", return_value=True)
def test_manus_webhook_invalid_json(mock_ts, mock_fetch, mock_verify):
    """Manus webhook returns 400 for invalid JSON body."""
    response = client.post(
        "/manus/webhook",
        content=b"not json",
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": "dW51c2Vk",
            "X-Webhook-Timestamp": str(int(time.time())),
        },
    )
    assert response.status_code == 400
