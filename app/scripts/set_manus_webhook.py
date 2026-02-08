#!/usr/bin/env python3
"""Register/manage webhook with Manus API.

Usage:
    python scripts/set_manus_webhook.py set      # Register webhook
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

MANUS_API_KEY = os.environ.get("MANUS_API_KEY")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL")

# Endpoint path (matches FastAPI route in main.py)
MANUS_WEBHOOK_PATH = "/manus/webhook"

MANUS_API_BASE = "https://api.manus.ai/v1"

if not MANUS_API_KEY:
    print("Error: MANUS_API_KEY not set in environment")
    sys.exit(1)


def set_webhook():
    """Register webhook with Manus."""
    if not WEBHOOK_BASE_URL:
        print("Error: WEBHOOK_BASE_URL not set in environment")
        sys.exit(1)

    webhook_url = f"{WEBHOOK_BASE_URL}{MANUS_WEBHOOK_PATH}"
    print(f"Setting Manus webhook to: {webhook_url}")

    response = httpx.post(
        f"{MANUS_API_BASE}/webhooks",
        headers={"API_KEY": MANUS_API_KEY},
        json={"webhook": {"url": webhook_url}},
        timeout=10,
    )

    result = response.json()
    if response.status_code == 200:
        webhook_id = result.get("webhook_id", "(unknown)")
        print(f"Webhook registered successfully! ID: {webhook_id}")
    else:
        print(f"Failed to register webhook: {response.status_code}")

    print(f"\nResponse: {result}")


def main():
    commands = {
        "set": set_webhook,
    }

    cmd = sys.argv[1] if len(sys.argv) > 1 else "set"

    if cmd not in commands:
        print(f"Unknown command: {cmd}")
        print(f"Available commands: {', '.join(commands.keys())}")
        sys.exit(1)

    commands[cmd]()


if __name__ == "__main__":
    main()
