"""Granola webhook helpers: Standard Webhooks verify, event_id dedup, journal write.

Docs: https://docs.granola.ai/webhooks
Payloads carry no note body. After verify, GET /v1/notes/{id} and reuse the
incremental journal writers (``format_granola_block``, ``### Transcript Notes``,
3am PT rollover). ``note.generated`` / ``note.access_granted`` skip an existing
``<!-- granola:not_… -->`` block. ``note.edited`` replaces that block in place
(or inserts if the original write was missed).

When ``GRANOLA_FOLLOWUP_WEBHOOK_URL`` and ``GRANOLA_FOLLOWUP_WEBHOOK_KEY`` are
both set, the same processed events are also POSTed JSON to that URL
(fire-and-forget; journal write and the HTTP 2xx are unaffected).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import threading
import time
from urllib.parse import urlsplit

import requests

from config import redis_client
from services.granola.client import GranolaAPIError, GranolaNoteNotFound, get_note
from services.granola.sync import write_notes_by_journal

logger = logging.getLogger(__name__)

# Granola event types from https://docs.granola.ai/webhooks
GRANOLA_NOTE_EVENTS = frozenset(
    {
        "note.generated",
        "note.edited",
        "note.access_granted",
    }
)

WHSEC_PREFIX = "whsec_"
EVENT_DEDUP_KEY_PREFIX = "granola:webhook:event:"
# Granola retries failed deliveries for four days; keep a little extra slack.
EVENT_DEDUP_TTL_SECONDS = 7 * 24 * 60 * 60
# Standard Webhooks replay window ("a few minutes").
WEBHOOK_TOLERANCE_SECONDS = 300
# Optional outbound follow-up POST. Short so a hung peer cannot pin RAM/CPU.
FOLLOWUP_TIMEOUT_SECONDS = 5
FOLLOWUP_URL_ENV = "GRANOLA_FOLLOWUP_WEBHOOK_URL"
FOLLOWUP_KEY_ENV = "GRANOLA_FOLLOWUP_WEBHOOK_KEY"


def granola_signing_key(secret: str) -> bytes:
    """Base64-decode the signing secret after the ``whsec_`` prefix."""
    raw = (secret or "").strip()
    if raw.startswith(WHSEC_PREFIX):
        raw = raw[len(WHSEC_PREFIX) :]
    return base64.b64decode(raw)


def verify_granola_signature(
    payload: bytes,
    webhook_id: str,
    webhook_timestamp: str,
    webhook_signature: str,
    secret: str,
) -> bool:
    """Verify a Standard Webhooks ``v1`` HMAC-SHA256 signature.

    Signed content is ``{webhook-id}.{webhook-timestamp}.{raw_body}``.
    ``webhook-signature`` may contain space-separated ``v1,<base64>`` values.
    Never logs the secret.
    """
    if not webhook_id or not webhook_timestamp or not webhook_signature or not secret:
        return False
    if payload is None:
        return False
    try:
        key = granola_signing_key(secret)
        expected = hmac.new(
            key,
            f"{webhook_id}.{webhook_timestamp}.".encode("utf-8") + payload,
            hashlib.sha256,
        ).digest()
    except Exception:
        return False

    for versioned in webhook_signature.split():
        version, separator, signature = versioned.partition(",")
        if separator != "," or version != "v1" or not signature:
            continue
        try:
            provided = base64.b64decode(signature)
        except Exception:
            continue
        if len(provided) == len(expected) and hmac.compare_digest(provided, expected):
            return True
    return False


def is_granola_timestamp_valid(
    timestamp: str,
    max_age_seconds: int = WEBHOOK_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Reject replayed deliveries whose ``webhook-timestamp`` is too old."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    return abs(current - ts) <= max_age_seconds


def event_dedup_key(event_id: str) -> str:
    return f"{EVENT_DEDUP_KEY_PREFIX}{event_id}"


def claim_granola_event_id(event_id: str) -> bool:
    """Atomically claim ``event_id`` in Redis. True if this is the first delivery.

    Retries reuse ``event_id``. Redis errors fail open so a brief outage does
    not drop a note (journal ``<!-- granola:not_… -->`` still dedups writes).
    """
    text = (event_id or "").strip()
    if not text:
        return True
    try:
        return bool(
            redis_client.set(
                event_dedup_key(text),
                "1",
                nx=True,
                ex=EVENT_DEDUP_TTL_SECONDS,
            )
        )
    except Exception:
        logger.exception("Granola webhook event_id dedup failed; processing anyway")
        return True


def _followup_config() -> tuple[str, str] | None:
    """Return ``(url, key)`` when both env vars are non-empty; else None.

    Never logs the key or the URL query string.
    """
    url = (os.getenv(FOLLOWUP_URL_ENV) or "").strip()
    key = (os.getenv(FOLLOWUP_KEY_ENV) or "").strip()
    if not url or not key:
        return None
    return url, key


def _followup_target_for_log(url: str) -> str:
    """Host + path only — drop userinfo, query, and fragment."""
    parts = urlsplit(url)
    host = parts.hostname or "unknown"
    path = parts.path or "/"
    return f"{host}{path}"


def _nonempty_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_granola_followup_payload(event: dict) -> dict:
    """JSON body for the follow-up webhook: incoming event basics only.

    Always includes ``event_type`` and ``note_id``. Adds ``event_id`` and
    ``occurred_at`` / ``timestamp`` when the inbound webhook carried them.
    Does not fetch or attach note content.
    """
    payload: dict = {
        "event_type": event.get("event_type"),
        "note_id": event.get("note_id"),
    }
    for key in ("event_id", "occurred_at", "timestamp"):
        text = _nonempty_text(event.get(key))
        if text is not None:
            payload[key] = text
    return payload


def forward_granola_followup(event: dict) -> None:
    """POST the follow-up payload. Never raises. Never logs the key or URL query."""
    config = _followup_config()
    if not config:
        logger.debug("Granola follow-up webhook not configured; skipping")
        return
    url, key = config
    payload = build_granola_followup_payload(event)
    target = _followup_target_for_log(url)
    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=FOLLOWUP_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            logger.warning(
                "Granola follow-up webhook HTTP %s target=%s event=%s note_id=%s",
                response.status_code,
                target,
                payload.get("event_type"),
                payload.get("note_id"),
            )
        else:
            logger.info(
                "Granola follow-up webhook posted target=%s event=%s note_id=%s",
                target,
                payload.get("event_type"),
                payload.get("note_id"),
            )
    except Exception:
        logger.exception(
            "Granola follow-up webhook failed target=%s event=%s note_id=%s",
            target,
            payload.get("event_type"),
            payload.get("note_id"),
        )


def schedule_granola_followup(event: dict) -> None:
    """Fire-and-forget follow-up POST. Never raises; never blocks the caller."""
    try:
        if not _followup_config():
            logger.debug("Granola follow-up webhook not configured; skipping")
            return
        thread = threading.Thread(
            target=forward_granola_followup,
            args=(event,),
            daemon=True,
            name="granola-followup",
        )
        thread.start()
    except Exception:
        logger.exception("Granola follow-up webhook failed to schedule")


def process_granola_webhook_event(data: dict) -> dict | None:
    """Fetch the referenced note and write it under ``### Transcript Notes``.

    ``note.edited`` replaces an existing block for that granola id (or
    inserts if missing). Generated / access_granted still skip duplicates.
    The same processed events are forwarded to the optional follow-up
    webhook from the incoming event (no note body) without blocking this write.

    Logs errors; never raises. Returns the write summary, or None when skipped.
    """
    event_type = data.get("event_type", "unknown")
    note_id = data.get("note_id")
    event_id = data.get("event_id")
    logger.info(
        "Granola webhook | event=%s | note_id=%s | event_id=%s",
        event_type,
        note_id,
        event_id,
    )

    if event_type not in GRANOLA_NOTE_EVENTS:
        logger.info("Granola event ignored: %s", event_type)
        return None

    if not note_id:
        logger.warning("Granola webhook missing note_id; skipping")
        return None

    # Incoming event only; do not wait on GET /v1/notes/{id} or the journal write.
    schedule_granola_followup(data)

    try:
        note = get_note(str(note_id))
    except GranolaNoteNotFound:
        logger.warning("Granola note %s 404; skipping (not inventing)", note_id)
        return None
    except (GranolaAPIError, EnvironmentError, Exception):
        logger.exception("Granola webhook get note failed")
        return None

    if not isinstance(note, dict):
        logger.warning("Granola note %s was not an object; skipping", note_id)
        return None

    try:
        result = write_notes_by_journal(
            [note],
            replace_existing=(event_type == "note.edited"),
        )
    except Exception:
        logger.exception("Failed to write Granola note to journal")
        return None

    logger.info(
        "Granola webhook write inserted=%s replaced=%s skipped=%s "
        "skipped_missing_journal=%s files_written=%s errors=%s",
        result.get("inserted"),
        result.get("replaced"),
        result.get("skipped"),
        result.get("skipped_missing_journal"),
        result.get("files_written"),
        len(result.get("errors") or []),
    )
    return result
