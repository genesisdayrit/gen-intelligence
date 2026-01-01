"""API endpoint tests."""

import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set required env vars before importing app
os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


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
