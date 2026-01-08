"""
Test script to capture Linear webhook payloads for ProjectUpdate and Initiative events.

Run this script as a standalone server to capture webhook payloads:
    python -m tests.capture_linear_webhooks

Then configure Linear to send webhooks to your ngrok URL + /linear/capture

After capturing, check the output files:
- captured_webhooks/project_update_*.json
- captured_webhooks/initiative_*.json
"""

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

LINEAR_WEBHOOK_SECRET = os.getenv("LINEAR_WEBHOOK_SECRET")

# Create output directory
OUTPUT_DIR = Path(__file__).parent / "captured_webhooks"
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Linear Webhook Capture")


def verify_linear_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify Linear webhook HMAC-SHA256 signature (hex-encoded)."""
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.get("/health")
async def health():
    return {"status": "healthy", "mode": "webhook_capture"}


@app.post("/linear/capture")
async def capture_linear_webhook(
    request: Request,
    linear_signature: str | None = Header(None, alias="Linear-Signature"),
):
    """Capture and save Linear webhook payloads for analysis."""
    payload = await request.body()

    # Verify signature if secret is configured
    if LINEAR_WEBHOOK_SECRET and linear_signature:
        if not verify_linear_signature(payload, linear_signature, LINEAR_WEBHOOK_SECRET):
            logger.warning("Invalid Linear signature")
            raise HTTPException(status_code=401, detail="Invalid signature")

    # Parse payload
    try:
        data = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Extract event info
    event_type = data.get("type", "unknown")
    action = data.get("action", "unknown")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Log full payload
    logger.info("=" * 60)
    logger.info(f"CAPTURED: type={event_type} action={action}")
    logger.info("=" * 60)
    logger.info(json.dumps(data, indent=2))
    logger.info("=" * 60)

    # Save to file
    filename = f"{event_type.lower()}_{action}_{timestamp}.json"
    output_path = OUTPUT_DIR / filename
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Saved to: {output_path}")

    return JSONResponse(content={
        "status": "captured",
        "type": event_type,
        "action": action,
        "saved_to": str(output_path),
    })


if __name__ == "__main__":
    import uvicorn
    print(f"\nWebhook capture server starting...")
    print(f"Output directory: {OUTPUT_DIR}")
    print(f"\nEndpoint: POST /linear/capture")
    print(f"Configure Linear webhook URL: https://your-ngrok-url/linear/capture")
    print(f"\nMake sure to enable 'Project updates' and 'Initiatives' in Linear webhook settings\n")
    uvicorn.run(app, host="0.0.0.0", port=8001)
