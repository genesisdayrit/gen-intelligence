"""Granola webhook helpers: Standard Webhooks verify, event_id dedup, journal write.

Docs: https://docs.granola.ai/webhooks
Payloads carry no note body. After verify, GET /v1/notes/{id} and reuse the
incremental journal writers (``format_granola_block``, ``### Transcript Notes``,
3am PT rollover, ``<!-- granola:not_… -->`` dedup).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

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


def process_granola_webhook_event(data: dict) -> dict | None:
    """Fetch the referenced note and append it under ``### Transcript Notes``.

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
        result = write_notes_by_journal([note])
    except Exception:
        logger.exception("Failed to write Granola note to journal")
        return None

    logger.info(
        "Granola webhook write inserted=%s skipped=%s "
        "skipped_missing_journal=%s files_written=%s errors=%s",
        result.get("inserted"),
        result.get("skipped"),
        result.get("skipped_missing_journal"),
        result.get("files_written"),
        len(result.get("errors") or []),
    )
    return result
