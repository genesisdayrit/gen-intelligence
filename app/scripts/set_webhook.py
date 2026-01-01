#!/usr/bin/env python3
"""Register/manage webhook with Telegram Bot API.

Usage:
    python scripts/set_webhook.py set      # Register webhook
    python scripts/set_webhook.py info     # Get current webhook info
    python scripts/set_webhook.py delete   # Delete webhook
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL")
SECRET_TOKEN = os.environ.get("TG_WEBHOOK_SECRET")

# Endpoint path (matches FastAPI route in main.py)
TELEGRAM_WEBHOOK_PATH = "/telegram/webhook"

if not BOT_TOKEN:
    print("Error: BOT_TOKEN not set in environment")
    sys.exit(1)

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def set_webhook():
    """Register webhook with Telegram."""
    if not WEBHOOK_BASE_URL:
        print("Error: WEBHOOK_BASE_URL not set in environment")
        sys.exit(1)
    if not SECRET_TOKEN:
        print("Error: TG_WEBHOOK_SECRET not set in environment")
        sys.exit(1)

    webhook_url = f"{WEBHOOK_BASE_URL}{TELEGRAM_WEBHOOK_PATH}"
    print(f"Setting webhook to: {webhook_url}")

    response = httpx.post(
        f"{API_BASE}/setWebhook",
        json={
            "url": webhook_url,
            "secret_token": SECRET_TOKEN,
            "allowed_updates": ["channel_post"],
            "drop_pending_updates": True,
        },
    )
    
    result = response.json()
    if result.get("ok"):
        print("✅ Webhook set successfully!")
    else:
        print(f"❌ Failed to set webhook: {result}")
    
    print(f"\nResponse: {result}")


def get_webhook_info():
    """Get current webhook info from Telegram."""
    print("Getting webhook info...")
    
    response = httpx.get(f"{API_BASE}/getWebhookInfo")
    result = response.json()
    
    if result.get("ok"):
        info = result.get("result", {})
        print(f"\n📌 Webhook URL: {info.get('url') or '(not set)'}")
        print(f"   Pending updates: {info.get('pending_update_count', 0)}")
        print(f"   Last error: {info.get('last_error_message') or '(none)'}")
        print(f"   Allowed updates: {info.get('allowed_updates') or '(all)'}")
    else:
        print(f"❌ Failed to get webhook info: {result}")
    
    print(f"\nFull response: {result}")


def delete_webhook():
    """Delete webhook from Telegram."""
    print("Deleting webhook...")
    
    response = httpx.post(
        f"{API_BASE}/deleteWebhook",
        json={"drop_pending_updates": True},
    )
    
    result = response.json()
    if result.get("ok"):
        print("✅ Webhook deleted successfully!")
    else:
        print(f"❌ Failed to delete webhook: {result}")
    
    print(f"\nResponse: {result}")


def main():
    commands = {
        "set": set_webhook,
        "info": get_webhook_info,
        "delete": delete_webhook,
    }
    
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    
    if cmd not in commands:
        print(f"Unknown command: {cmd}")
        print(f"Available commands: {', '.join(commands.keys())}")
        sys.exit(1)
    
    commands[cmd]()


if __name__ == "__main__":
    main()

