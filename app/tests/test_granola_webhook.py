"""Granola webhook: Standard Webhooks signature, dedup, journal write path."""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_KEY_BYTES = b"granola-test-signing-key!!"
GRANOLA_WEBHOOK_SECRET = "whsec_" + base64.b64encode(_TEST_KEY_BYTES).decode()

os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
os.environ.setdefault("GRANOLA_API_KEY", "test-granola-key")
os.environ["GRANOLA_WEBHOOK_SECRET"] = GRANOLA_WEBHOOK_SECRET
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"
os.environ.setdefault("DROPBOX_OBSIDIAN_VAULT_PATH", "/obsidian/personal")
os.environ.setdefault("DROPBOX_ACCESS_KEY", "test-key")
os.environ.setdefault("DROPBOX_ACCESS_SECRET", "test-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "test-refresh")


@pytest.fixture(autouse=True)
def _force_secrets(monkeypatch):
    monkeypatch.setenv("GRANOLA_WEBHOOK_SECRET", GRANOLA_WEBHOOK_SECRET)
    monkeypatch.setenv("GRANOLA_API_KEY", "test-granola-key")
    monkeypatch.setenv("SYSTEM_TIMEZONE", "America/Los_Angeles")


from fastapi.testclient import TestClient

from main import app
from services.granola.webhook import (
    EVENT_DEDUP_TTL_SECONDS,
    WEBHOOK_TOLERANCE_SECONDS,
    claim_granola_event_id,
    event_dedup_key,
    is_granola_timestamp_valid,
    process_granola_webhook_event,
    verify_granola_signature,
)

client = TestClient(app)

SAMPLE_EVENT = {
    "event_id": "8f1c2a4e-6b3d-4e8f-9a2b-1c5d7e9f0a3b",
    "event_type": "note.generated",
    "note_id": "not_1d3tmYTlCICgjy",
    "occurred_at": "2026-01-27T15:30:00Z",
}

SAMPLE_NOTE = {
    "id": "not_1d3tmYTlCICgjy",
    "title": "Quarterly yoghurt budget review",
    "web_url": "https://notes.granola.ai/d/f3e45e0f-24cc-480b-9a6c-8b1f5e3d7a2c",
    "summary_markdown": "## Takeaways\n- Buy more yoghurt",
    "created_at": "2026-01-27T15:00:00Z",
}


def _sign(
    body: bytes,
    webhook_id: str,
    timestamp: str,
    secret: str = GRANOLA_WEBHOOK_SECRET,
) -> str:
    raw = secret[len("whsec_") :] if secret.startswith("whsec_") else secret
    key = base64.b64decode(raw)
    digest = hmac.new(
        key,
        f"{webhook_id}.{timestamp}.".encode("utf-8") + body,
        hashlib.sha256,
    ).digest()
    return "v1," + base64.b64encode(digest).decode()


def _signed_headers(body: bytes, event_id: str | None = None, timestamp: str | None = None):
    webhook_id = event_id or SAMPLE_EVENT["event_id"]
    ts = timestamp or str(int(time.time()))
    return {
        "Content-Type": "application/json",
        "webhook-id": webhook_id,
        "webhook-timestamp": ts,
        "webhook-signature": _sign(body, webhook_id, ts),
    }, ts


def _post_event(event: dict | None = None, **header_overrides):
    payload = json.dumps(event or SAMPLE_EVENT).encode("utf-8")
    headers, _ = _signed_headers(payload, event_id=(event or SAMPLE_EVENT).get("event_id"))
    headers.update(header_overrides)
    return client.post("/granola/webhook", content=payload, headers=headers), payload


# ---------------------------------------------------------------------------
# Signature helpers
# ---------------------------------------------------------------------------


def test_verify_granola_signature_accepts_valid_v1():
    body = json.dumps(SAMPLE_EVENT).encode("utf-8")
    webhook_id = SAMPLE_EVENT["event_id"]
    timestamp = str(int(time.time()))
    signature = _sign(body, webhook_id, timestamp)
    assert verify_granola_signature(
        body, webhook_id, timestamp, signature, GRANOLA_WEBHOOK_SECRET
    )


def test_verify_granola_signature_accepts_one_valid_among_multiple():
    body = json.dumps(SAMPLE_EVENT).encode("utf-8")
    webhook_id = SAMPLE_EVENT["event_id"]
    timestamp = str(int(time.time()))
    good = _sign(body, webhook_id, timestamp)
    header = f"v1,dG90YWxseWZha2VzaWduYXR1cmU= {good}"
    assert verify_granola_signature(
        body, webhook_id, timestamp, header, GRANOLA_WEBHOOK_SECRET
    )


def test_verify_granola_signature_rejects_bad_sig():
    body = json.dumps(SAMPLE_EVENT).encode("utf-8")
    assert not verify_granola_signature(
        body,
        SAMPLE_EVENT["event_id"],
        str(int(time.time())),
        "v1,dG90YWxseWZha2VzaWduYXR1cmU=",
        GRANOLA_WEBHOOK_SECRET,
    )


def test_verify_granola_signature_uses_raw_body():
    body = json.dumps(SAMPLE_EVENT, separators=(",", ":")).encode("utf-8")
    pretty = json.dumps(SAMPLE_EVENT, indent=2).encode("utf-8")
    webhook_id = SAMPLE_EVENT["event_id"]
    timestamp = str(int(time.time()))
    signature = _sign(body, webhook_id, timestamp)
    assert verify_granola_signature(
        body, webhook_id, timestamp, signature, GRANOLA_WEBHOOK_SECRET
    )
    assert not verify_granola_signature(
        pretty, webhook_id, timestamp, signature, GRANOLA_WEBHOOK_SECRET
    )


def test_is_granola_timestamp_valid_rejects_stale_and_garbage():
    now = 1_700_000_000.0
    assert is_granola_timestamp_valid(str(int(now)), now=now)
    assert is_granola_timestamp_valid(
        str(int(now - WEBHOOK_TOLERANCE_SECONDS)), now=now
    )
    assert not is_granola_timestamp_valid(
        str(int(now - WEBHOOK_TOLERANCE_SECONDS - 1)), now=now
    )
    assert not is_granola_timestamp_valid("not-a-timestamp", now=now)
    assert not is_granola_timestamp_valid(None, now=now)


# ---------------------------------------------------------------------------
# HTTP: signature / timestamp
# ---------------------------------------------------------------------------


def test_webhook_rejects_bad_signature():
    payload = json.dumps(SAMPLE_EVENT).encode("utf-8")
    headers, _ = _signed_headers(payload)
    headers["webhook-signature"] = "v1,dG90YWxseWZha2VzaWduYXR1cmU="
    with patch("main._process_granola_event") as mock_process:
        response = client.post("/granola/webhook", content=payload, headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid signature"
    mock_process.assert_not_called()


def test_webhook_accepts_valid_signature_and_acks():
    with patch("main.claim_granola_event_id", return_value=True), patch(
        "main._process_granola_event"
    ) as mock_process:
        response, _ = _post_event()
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    mock_process.assert_called_once()
    posted = mock_process.call_args[0][0]
    assert posted["event_id"] == SAMPLE_EVENT["event_id"]
    assert posted["note_id"] == SAMPLE_EVENT["note_id"]
    assert posted["event_type"] == "note.generated"
    assert "summary_markdown" not in posted


def test_webhook_rejects_stale_timestamp():
    payload = json.dumps(SAMPLE_EVENT).encode("utf-8")
    old_ts = str(int(time.time()) - WEBHOOK_TOLERANCE_SECONDS - 30)
    headers, _ = _signed_headers(payload, timestamp=old_ts)
    with patch("main._process_granola_event") as mock_process:
        response = client.post("/granola/webhook", content=payload, headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "Timestamp expired"
    mock_process.assert_not_called()


def test_webhook_rejects_missing_headers():
    response = client.post("/granola/webhook", json=SAMPLE_EVENT)
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing signature headers"


def test_webhook_rejects_when_secret_missing(monkeypatch):
    monkeypatch.delenv("GRANOLA_WEBHOOK_SECRET", raising=False)
    with patch("main.GRANOLA_WEBHOOK_SECRET", None), patch(
        "main._granola_webhook_secret", return_value=None
    ):
        response, _ = _post_event()
    assert response.status_code == 401
    assert response.json()["detail"] == "Webhook secret not configured"


def test_webhook_rejects_invalid_json():
    payload = b"not json"
    headers, _ = _signed_headers(payload)
    response = client.post("/granola/webhook", content=payload, headers=headers)
    assert response.status_code == 400


@pytest.mark.parametrize(
    "event_type",
    ["note.generated", "note.edited", "note.access_granted"],
)
def test_webhook_handles_documented_event_types(event_type):
    event = {**SAMPLE_EVENT, "event_type": event_type, "event_id": f"evt-{event_type}"}
    if event_type == "note.edited":
        event["data"] = {"changed_fields": ["summary"]}
    with patch("main.claim_granola_event_id", return_value=True), patch(
        "main._process_granola_event"
    ) as mock_process:
        response, _ = _post_event(event)
    assert response.status_code == 202
    mock_process.assert_called_once()
    assert mock_process.call_args[0][0]["event_type"] == event_type


def test_webhook_ignores_unknown_event_type():
    event = {**SAMPLE_EVENT, "event_type": "note.deleted", "event_id": "evt-unknown"}
    with patch("main.claim_granola_event_id") as mock_claim, patch(
        "main._process_granola_event"
    ) as mock_process:
        response, _ = _post_event(event)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_claim.assert_not_called()
    mock_process.assert_not_called()


# ---------------------------------------------------------------------------
# Redis event_id dedup
# ---------------------------------------------------------------------------


def test_claim_granola_event_id_setnx():
    mock_redis = MagicMock()
    mock_redis.set.return_value = True
    with patch("services.granola.webhook.redis_client", mock_redis):
        assert claim_granola_event_id("evt-1") is True
    mock_redis.set.assert_called_once_with(
        event_dedup_key("evt-1"),
        "1",
        nx=True,
        ex=EVENT_DEDUP_TTL_SECONDS,
    )


def test_webhook_dedups_retries_on_event_id():
    mock_redis = MagicMock()
    mock_redis.set.side_effect = [True, False]
    with patch("services.granola.webhook.redis_client", mock_redis), patch(
        "main._process_granola_event"
    ) as mock_process:
        first, _ = _post_event()
        second, _ = _post_event()
    assert first.status_code == 202
    assert first.json() == {"status": "accepted"}
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    mock_process.assert_called_once()
    assert mock_redis.set.call_count == 2


# ---------------------------------------------------------------------------
# Write path (mocked fetch + journal writers)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_type",
    ["note.generated", "note.edited", "note.access_granted"],
)
def test_process_fetches_note_and_writes_journal(event_type):
    write_summary = {
        "selected": 1,
        "inserted": 0 if event_type == "note.edited" else 1,
        "replaced": 1 if event_type == "note.edited" else 0,
        "skipped": 0,
        "skipped_missing_journal": 0,
        "files_written": 1,
        "errors": [],
        "paths": ["/journal/Jan 27, 2026.md"],
    }
    event = {**SAMPLE_EVENT, "event_type": event_type}
    if event_type == "note.edited":
        event["data"] = {"changed_fields": ["summary"]}
    with patch(
        "services.granola.webhook.get_note", return_value=SAMPLE_NOTE
    ) as mock_get, patch(
        "services.granola.webhook.write_notes_by_journal", return_value=write_summary
    ) as mock_write:
        result = process_granola_webhook_event(event)
    mock_get.assert_called_once_with("not_1d3tmYTlCICgjy")
    if event_type == "note.edited":
        mock_write.assert_called_once_with([SAMPLE_NOTE], replace_existing=True)
        assert result["replaced"] == 1
    else:
        mock_write.assert_called_once_with([SAMPLE_NOTE], replace_existing=False)
        assert result["inserted"] == 1


@pytest.mark.parametrize(
    "event_type",
    ["note.generated", "note.edited", "note.access_granted"],
)
def test_webhook_write_path_mocked_end_to_end(event_type):
    write_summary = {
        "selected": 1,
        "inserted": 1,
        "replaced": 0,
        "skipped": 0,
        "skipped_missing_journal": 0,
        "files_written": 1,
        "errors": [],
        "paths": [],
    }
    event = {**SAMPLE_EVENT, "event_type": event_type, "event_id": f"evt-{event_type}"}
    if event_type == "note.edited":
        event["data"] = {"changed_fields": ["summary"]}
    with patch("main.claim_granola_event_id", return_value=True), patch(
        "services.granola.webhook.get_note", return_value=SAMPLE_NOTE
    ) as mock_get, patch(
        "services.granola.webhook.write_notes_by_journal", return_value=write_summary
    ) as mock_write:
        response, _ = _post_event(event)
    assert response.status_code == 202
    mock_get.assert_called_once_with("not_1d3tmYTlCICgjy")
    mock_write.assert_called_once_with(
        [SAMPLE_NOTE],
        replace_existing=(event_type == "note.edited"),
    )


def test_process_skips_404_note():
    from services.granola.client import GranolaNoteNotFound

    with patch(
        "services.granola.webhook.get_note",
        side_effect=GranolaNoteNotFound("not_missing"),
    ), patch("services.granola.webhook.write_notes_by_journal") as mock_write:
        assert process_granola_webhook_event(SAMPLE_EVENT) is None
    mock_write.assert_not_called()


def test_process_does_not_raise_on_write_failure():
    with patch(
        "services.granola.webhook.get_note", return_value=SAMPLE_NOTE
    ), patch(
        "services.granola.webhook.write_notes_by_journal",
        side_effect=RuntimeError("dropbox down"),
    ):
        assert process_granola_webhook_event(SAMPLE_EVENT) is None


# ---------------------------------------------------------------------------
# Scheduler: no */15 poll
# ---------------------------------------------------------------------------


def test_scheduler_no_longer_polls_granola_every_15_minutes():
    from scheduler import SCHEDULED_JOBS

    job = next(j for j in SCHEDULED_JOBS if j["id"] == "sync_granola_notes")
    trigger = str(job["trigger"])
    assert "*/15" not in trigger
    assert "2099" in trigger
