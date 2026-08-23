"""Inbound Raindrop webhook tests. Dropbox / share helpers are mocked."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("RAINDROP_WEBHOOK_SECRET", "test-raindrop-secret")
os.environ.setdefault("READWISE_WEBHOOK_SECRET", "test-readwise-secret")
os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")

from fastapi.testclient import TestClient

from main import app
from services.raindrop.webhook import (
    extract_raindrop_bookmark,
    is_created_raindrop_event,
    process_created_raindrop,
)

client = TestClient(app)
SECRET = "test-raindrop-secret"
ARTICLE_URL = "https://example.com/article"
ARTICLE_TITLE = "My Article"
YOUTUBE_URL = "https://www.youtube.com/watch?v=abcdefghijk"


def _payload(**overrides):
    data = {
        "title": ARTICLE_TITLE,
        "url": ARTICLE_URL,
        "secret": SECRET,
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"title": ARTICLE_TITLE, "url": ARTICLE_URL}, {"url": ARTICLE_URL, "title": ARTICLE_TITLE}),
        ({"title": ARTICLE_TITLE, "link": ARTICLE_URL}, {"url": ARTICLE_URL, "title": ARTICLE_TITLE}),
        (
            {"raindrop": {"title": ARTICLE_TITLE, "link": ARTICLE_URL}},
            {"url": ARTICLE_URL, "title": ARTICLE_TITLE},
        ),
        (
            {"item": {"title": ARTICLE_TITLE, "link": ARTICLE_URL}},
            {"url": ARTICLE_URL, "title": ARTICLE_TITLE},
        ),
        (
            {"Title": ARTICLE_TITLE, "Url": ARTICLE_URL},
            {"url": ARTICLE_URL, "title": ARTICLE_TITLE},
        ),
        (
            {"items": [{"title": ARTICLE_TITLE, "link": ARTICLE_URL}]},
            {"url": ARTICLE_URL, "title": ARTICLE_TITLE},
        ),
        ({"url": ARTICLE_URL}, {"url": ARTICLE_URL, "title": None}),
        ({"title": ARTICLE_TITLE}, None),
        ({"text": "a quote", "raindropRef": 99}, None),
    ],
)
def test_extract_raindrop_bookmark_shapes(payload, expected):
    assert extract_raindrop_bookmark(payload) == expected


@pytest.mark.parametrize(
    "payload, expected",
    [
        (_payload(), True),
        ({"title": ARTICLE_TITLE, "url": ARTICLE_URL, "event": "raindrop.created"}, True),
        ({"item": {"title": ARTICLE_TITLE, "link": ARTICLE_URL}, "action": "created"}, True),
        ({"title": ARTICLE_TITLE, "url": ARTICLE_URL, "event_type": "raindrop.updated"}, False),
        ({"title": ARTICLE_TITLE, "url": ARTICLE_URL, "action": "deleted"}, False),
        ({"title": ARTICLE_TITLE, "url": ARTICLE_URL, "removed": True}, False),
        ({"item": {"title": ARTICLE_TITLE, "link": ARTICLE_URL, "removed": True}}, False),
        ({"text": "quoted passage", "raindropRef": 123}, False),
        ({"secret": SECRET}, False),
    ],
)
def test_is_created_raindrop_event(payload, expected):
    assert is_created_raindrop_event(payload) is expected


# ---------------------------------------------------------------------------
# Webhook auth / ping
# ---------------------------------------------------------------------------


def test_raindrop_webhook_rejects_missing_secret():
    response = client.post("/raindrop/webhook", json=_payload(secret=None))
    assert response.status_code == 401


def test_raindrop_webhook_rejects_wrong_secret():
    response = client.post("/raindrop/webhook", json=_payload(secret="nope"))
    assert response.status_code == 401


def test_raindrop_webhook_empty_test_ping():
    with patch("services.raindrop.webhook.add_shared_link") as mock_share:
        response = client.post("/raindrop/webhook", content=b"")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_share.assert_not_called()


def test_raindrop_webhook_whitespace_ping_is_noop():
    with patch("services.raindrop.webhook.add_shared_link") as mock_share:
        response = client.post("/raindrop/webhook", content=b"  \n")
    assert response.status_code == 200
    mock_share.assert_not_called()


@pytest.mark.parametrize(
    "header_name",
    ["X-Raindrop-Webhook-Secret", "X-Webhook-Secret"],
)
@patch("services.raindrop.webhook.add_shared_link", return_value={"success": True, "action": "created"})
def test_raindrop_webhook_accepts_header_secret(mock_share, header_name):
    response = client.post(
        "/raindrop/webhook",
        json={"title": ARTICLE_TITLE, "url": ARTICLE_URL},
        headers={header_name: SECRET},
    )
    assert response.status_code == 202
    mock_share.assert_called_once_with(ARTICLE_URL, title=ARTICLE_TITLE)


@patch("services.raindrop.webhook.add_shared_link", return_value={"success": True, "action": "created"})
def test_raindrop_webhook_accepts_body_secret_when_header_wrong(mock_share):
    response = client.post(
        "/raindrop/webhook",
        json=_payload(),
        headers={"X-Raindrop-Webhook-Secret": "wrong"},
    )
    assert response.status_code == 202
    mock_share.assert_called_once()


# ---------------------------------------------------------------------------
# Create / YouTube / skip / missing journal
# ---------------------------------------------------------------------------


@patch("services.raindrop.webhook.add_shared_link", return_value={"success": True, "action": "created"})
def test_raindrop_webhook_creates_shared_link(mock_share):
    response = client.post("/raindrop/webhook", json=_payload())
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    mock_share.assert_called_once_with(ARTICLE_URL, title=ARTICLE_TITLE)


@patch("services.raindrop.webhook.add_shared_link", return_value={"success": True, "action": "created"})
def test_raindrop_webhook_nested_item_create(mock_share):
    response = client.post(
        "/raindrop/webhook",
        json={
            "secret": SECRET,
            "item": {"title": ARTICLE_TITLE, "link": ARTICLE_URL},
            "event": "raindrop.created",
        },
    )
    assert response.status_code == 202
    mock_share.assert_called_once_with(ARTICLE_URL, title=ARTICLE_TITLE)


@patch("services.raindrop.webhook.add_youtube_link", return_value={"success": True, "action": "created"})
@patch("services.raindrop.webhook.add_shared_link")
def test_raindrop_webhook_youtube_uses_youtube_helper(mock_share, mock_youtube):
    response = client.post("/raindrop/webhook", json=_payload(url=YOUTUBE_URL, title="Cool Video"))
    assert response.status_code == 202
    mock_youtube.assert_called_once_with(YOUTUBE_URL)
    mock_share.assert_not_called()


@patch("main.create_bookmark")
@patch("services.obsidian.add_readwise_buffet.append_wikilink_to_journal_buffet")
@patch("services.raindrop.webhook.add_shared_link", return_value={"success": True, "action": "skipped"})
def test_raindrop_webhook_same_day_skip_prevents_share_mirror_loop(mock_share, mock_buffet, mock_create):
    """If /share/link already mirrored this URL today, inbound must not remirror or double buffet."""
    response = client.post("/raindrop/webhook", json=_payload())
    assert response.status_code == 202
    mock_share.assert_called_once_with(ARTICLE_URL, title=ARTICLE_TITLE)
    mock_create.assert_not_called()
    mock_buffet.assert_not_called()


@patch("services.raindrop.webhook.add_shared_link", return_value={"success": True, "action": "created"})
def test_raindrop_webhook_missing_journal_does_not_fail(mock_share):
    """KH save still succeeds when the journal is missing; buffet skip lives in add_shared_link."""
    response = client.post("/raindrop/webhook", json=_payload())
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    mock_share.assert_called_once_with(ARTICLE_URL, title=ARTICLE_TITLE)


@patch("services.raindrop.webhook.add_shared_link")
def test_raindrop_webhook_ignores_update_and_delete(mock_share):
    for event in ("raindrop.updated", "raindrop.deleted"):
        response = client.post("/raindrop/webhook", json=_payload(event=event))
        assert response.status_code == 202
    mock_share.assert_not_called()


@patch("services.raindrop.webhook.add_shared_link")
def test_raindrop_webhook_does_not_pull_highlights(mock_share):
    response = client.post(
        "/raindrop/webhook",
        json={"secret": SECRET, "text": "a highlight", "raindropRef": 42},
    )
    assert response.status_code == 202
    mock_share.assert_not_called()


@patch("services.raindrop.webhook.add_shared_link", side_effect=RuntimeError("dropbox down"))
def test_raindrop_webhook_acks_even_if_dropbox_fails(mock_share):
    response = client.post("/raindrop/webhook", json=_payload())
    assert response.status_code == 202
    mock_share.assert_called_once()


# ---------------------------------------------------------------------------
# Processor routing (mocked helpers)
# ---------------------------------------------------------------------------


@patch("services.raindrop.webhook.add_shared_link", return_value={"success": True, "action": "skipped"})
def test_process_created_raindrop_same_day_skip_returns_skipped(mock_share):
    result = process_created_raindrop(_payload())
    assert result["action"] == "skipped"
    mock_share.assert_called_once_with(ARTICLE_URL, title=ARTICLE_TITLE)


@patch("services.raindrop.webhook.add_shared_link", return_value={"success": True, "action": "created"})
def test_process_created_raindrop_missing_journal_still_succeeds(mock_share):
    result = process_created_raindrop(_payload())
    assert result["success"] is True
    assert result["action"] == "created"
    mock_share.assert_called_once()
