"""Gen Intelligence API - Personal services hub."""

import logging
import os
from datetime import datetime

import pytz
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.obsidian.add_telegram_log import append_telegram_log

load_dotenv()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Config
TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET")
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
