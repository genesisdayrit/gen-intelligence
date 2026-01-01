"""Gen Intelligence API - Personal services hub."""

import base64
import hashlib
import hmac
import logging
import os
from datetime import datetime

import pytz
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.obsidian.add_telegram_log import append_telegram_log
from services.obsidian.add_todoist_completed import append_todoist_completed

load_dotenv()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Config
TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET")
TODOIST_CLIENT_SECRET = os.getenv("TODOIST_CLIENT_SECRET")
SYSTEM_TIMEZONE = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")


# Telegram models (minimal)
class Chat(BaseModel):
    id: int
    title: str | None = None


class Message(BaseModel):
    message_id: int
    chat: Chat
    date: int
    text: str | None = None
    caption: str | None = None


class TelegramUpdate(BaseModel):
    update_id: int
    channel_post: Message | None = None


# FastAPI app
app = FastAPI(title="Gen Intelligence API")


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(None),
):
    """Receive Telegram webhook updates."""
    # Validate secret
    if x_telegram_bot_api_secret_token != TG_WEBHOOK_SECRET:
        logger.warning("Invalid secret token")
        raise HTTPException(status_code=401, detail="Invalid secret token")

    # Parse payload
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.debug("Received: %s", payload)

    # Parse update
    try:
        update = TelegramUpdate(**payload)
    except Exception as e:
        logger.warning("Failed to parse update: %s", e)
        raise HTTPException(status_code=400, detail="Invalid update format")

    # Only handle channel posts
    if not update.channel_post:
        return JSONResponse(content={"status": "ignored"})

    msg = update.channel_post
    text = msg.text or msg.caption or "(no text)"
    tz = pytz.timezone(SYSTEM_TIMEZONE)
    timestamp = datetime.fromtimestamp(msg.date, tz).strftime("%H:%M %p")

    logger.info(
        "📨 %s | %s | %s",
        msg.chat.title or msg.chat.id,
        timestamp,
        text[:100],
    )

    # Write to Obsidian journal
    try:
        log_entry = f"[{timestamp}] {text}"
        append_telegram_log(log_entry)
        logger.info("Written to journal")
    except Exception as e:
        logger.error("Failed to write to journal: %s", e)
        # Still return 200 - don't want Telegram to retry

    return JSONResponse(content={"status": "ok"})


# Todoist webhook
def verify_todoist_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify Todoist webhook HMAC-SHA256 signature."""
    expected = base64.b64encode(
        hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


@app.post("/todoist/webhook")
async def todoist_webhook(
    request: Request,
    x_todoist_hmac_sha256: str | None = Header(None),
):
    """Receive Todoist webhook events."""
    # Get raw payload for signature verification
    payload = await request.body()

    # Verify signature
    if TODOIST_CLIENT_SECRET and x_todoist_hmac_sha256:
        if not verify_todoist_signature(payload, x_todoist_hmac_sha256, TODOIST_CLIENT_SECRET):
            logger.warning("Invalid Todoist signature")
            raise HTTPException(status_code=401, detail="Invalid signature")
    elif TODOIST_CLIENT_SECRET and not x_todoist_hmac_sha256:
        logger.warning("Missing Todoist signature header")
        raise HTTPException(status_code=401, detail="Missing signature")

    # Parse payload
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.debug("Todoist webhook received: %s", data)

    event_name = data.get("event_name", "unknown")
    event_data = data.get("event_data", {})
    user_id = data.get("user_id")

    # Handle item:completed events
    if event_name == "item:completed":
        task_content = event_data.get("content", "(no content)")
        task_id = event_data.get("id")
        project_id = event_data.get("project_id")

        logger.info(
            "✅ Todoist task completed | user=%s | task_id=%s | project=%s | content=%s",
            user_id,
            task_id,
            project_id,
            task_content[:100],
        )

        # Write to Daily Action
        try:
            append_todoist_completed(task_content)
            logger.info("Written to Daily Action")
        except Exception as e:
            logger.error("Failed to write to Daily Action: %s", e)
            # Still return 200 - don't want Todoist to retry
    else:
        logger.info("Todoist event: %s (ignored)", event_name)

    return JSONResponse(content={"status": "ok"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
