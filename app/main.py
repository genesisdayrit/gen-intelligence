"""Gen Intelligence API - Personal services hub."""

import base64
import hashlib
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime

import pytz
import requests
from cryptography.hazmat.primitives import hashes as crypto_hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as crypto_padding
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.obsidian.add_telegram_log import append_telegram_log
from services.obsidian.append_completed_task import append_completed_task
from services.obsidian.upsert_linear_update import upsert_linear_update
from services.obsidian.upsert_issue_touched import upsert_issue_touched
from services.obsidian.add_manus_task import upsert_manus_task
from services.obsidian.remove_todoist_completed import remove_todoist_completed
from services.obsidian.update_telegram_log import update_telegram_log
from services.obsidian.add_shared_link import add_shared_link, get_predicted_link_path
from services.obsidian.add_youtube_link import add_youtube_link, is_valid_youtube_url
from services.todoist.client import create_completed_todoist_task

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
LINEAR_WEBHOOK_SECRET = os.getenv("LINEAR_WEBHOOK_SECRET")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")
LINK_SHARE_API_KEY = os.getenv("LINK_SHARE_API_KEY")
MANUS_API_KEY = os.getenv("MANUS_API_KEY")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "")
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
    edited_channel_post: Message | None = None


# Link sharing models
class LinkShareRequest(BaseModel):
    url: str
    title: str | None = None


class LinkShareResponse(BaseModel):
    status: str
    message: str
    file_path: str | None = None
    vault_name: str | None = None


# YouTube sharing model
class YouTubeShareRequest(BaseModel):
    url: str


# FastAPI app
@asynccontextmanager
async def lifespan(app):
    from scheduler import start_scheduler, shutdown_scheduler

    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(title="Gen Intelligence API", lifespan=lifespan)


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

    # Handle new channel posts
    if update.channel_post:
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
            append_telegram_log(log_entry, message_id=msg.message_id)
            logger.info("Written to journal")
        except Exception as e:
            logger.error("Failed to write to journal: %s", e)
            # Still return 200 - don't want Telegram to retry

        return JSONResponse(content={"status": "ok"})

    # Handle edited channel posts
    if update.edited_channel_post:
        msg = update.edited_channel_post
        new_text = msg.text or msg.caption or "(no text)"

        logger.info(
            "✏️ %s | msg_id=%s | %s",
            msg.chat.title or msg.chat.id,
            msg.message_id,
            new_text[:100],
        )

        # Update entry in Obsidian journal
        try:
            updated = update_telegram_log(msg.message_id, new_text)
            if updated:
                logger.info("Updated in journal")
            else:
                logger.info("Entry not found in journal (may be from a different day or before tracking)")
        except Exception as e:
            logger.error("Failed to update journal: %s", e)
            # Still return 200 - don't want Telegram to retry

        return JSONResponse(content={"status": "ok"})

    # Neither channel_post nor edited_channel_post
    return JSONResponse(content={"status": "ignored"})


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

        # Write to Daily Action and Weekly Cycle
        try:
            result = append_completed_task(task_content)
            if not result["daily_action_success"]:
                logger.error("Failed to write to Daily Action: %s", result["daily_action_error"])
            if not result["weekly_cycle_success"]:
                logger.error("Failed to write to Weekly Cycle: %s", result["weekly_cycle_error"])
        except Exception as e:
            logger.error("Failed to write completed task: %s", e)
            # Still return 200 - don't want Todoist to retry

    # Handle item:uncompleted events
    elif event_name == "item:uncompleted":
        task_content = event_data.get("content", "(no content)")
        task_id = event_data.get("id")
        project_id = event_data.get("project_id")

        logger.info(
            "↩️ Todoist task uncompleted | user=%s | task_id=%s | project=%s | content=%s",
            user_id,
            task_id,
            project_id,
            task_content[:100],
        )

        # Remove from Daily Action
        try:
            removed = remove_todoist_completed(task_content)
            if removed:
                logger.info("Removed from Daily Action")
            else:
                logger.info("Task not found in Daily Action (may have been completed on a different day)")
        except Exception as e:
            logger.error("Failed to remove from Daily Action: %s", e)
            # Still return 200 - don't want Todoist to retry

    else:
        logger.info("Todoist event: %s (ignored)", event_name)

    return JSONResponse(content={"status": "ok"})


# Linear webhook
def verify_linear_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify Linear webhook HMAC-SHA256 signature (hex-encoded)."""
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/linear/webhook")
async def linear_webhook(
    request: Request,
    linear_signature: str | None = Header(None, alias="Linear-Signature"),
):
    """Receive Linear webhook events."""
    # Get raw payload for signature verification
    payload = await request.body()

    # Verify signature
    if LINEAR_WEBHOOK_SECRET and linear_signature:
        if not verify_linear_signature(payload, linear_signature, LINEAR_WEBHOOK_SECRET):
            logger.warning("Invalid Linear signature")
            raise HTTPException(status_code=401, detail="Invalid signature")
    elif LINEAR_WEBHOOK_SECRET and not linear_signature:
        logger.warning("Missing Linear signature header")
        raise HTTPException(status_code=401, detail="Missing signature")

    # Parse payload
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.debug("Linear webhook received: %s", data)

    # Extract event data
    action = data.get("action")
    event_type = data.get("type")
    event_data = data.get("data", {})
    actor = data.get("actor") or {}
    actor_type = actor.get("type")

    # Only process human-triggered updates; acknowledge all others to avoid retries.
    if actor_type != "user":
        entity_name = (
            event_data.get("title")
            or event_data.get("name")
            or event_data.get("project", {}).get("name")
            or event_data.get("initiative", {}).get("name")
            or event_type
        )
        logger.info(
            "[FILTERED] Linear webhook | actor_type=%s | type=%s | action=%s | entity=%s",
            actor_type or "unknown",
            event_type,
            action,
            entity_name,
        )
        return JSONResponse(content={"status": "ok"})

    # Handle Project events (when projects are created/updated under initiatives)
    if event_type == "Project" and action in ("create", "update"):
        project_name = event_data.get("name", "(unknown project)")
        project_id = event_data.get("id")

        logger.info(
            "📁 Linear project | %s | %s",
            project_name,
            action,
        )

        # Sync the parent initiative to Obsidian _Initiatives folder
        if project_id:
            try:
                from scripts.linear.sync_single_initiative import sync_initiative_for_project
                sync_result = sync_initiative_for_project(project_id)
                if sync_result["errors"]:
                    logger.error("Initiative sync errors: %s", sync_result["errors"])
                else:
                    logger.info("Synced initiative for project: %s", project_id)
            except Exception as e:
                logger.error("Failed to sync initiative for project: %s", e)

        return JSONResponse(content={"status": "ok"})

    # Handle Document events (only sync if document has a parent initiative via project or directly)
    if event_type == "Document" and action in ("create", "update"):
        doc_title = event_data.get("title", "(unknown document)")
        project = event_data.get("project", {})
        initiative = event_data.get("initiative", {})
        project_id = project.get("id") if project else None
        initiative_id = initiative.get("id") if initiative else None

        logger.info(
            "📄 Linear document | %s | %s",
            doc_title,
            action,
        )

        # Sync initiative if document is linked to a project (which has an initiative)
        if project_id:
            try:
                from scripts.linear.sync_single_initiative import sync_initiative_for_project
                sync_result = sync_initiative_for_project(project_id)
                if sync_result["errors"]:
                    logger.error("Initiative sync errors: %s", sync_result["errors"])
                else:
                    logger.info("Synced initiative for document's project: %s", project_id)
            except Exception as e:
                logger.error("Failed to sync initiative for document's project: %s", e)
            return JSONResponse(content={"status": "ok"})

        # Sync initiative if document is directly linked to an initiative
        if initiative_id:
            try:
                from scripts.linear.sync_single_initiative import sync_initiative_by_id
                sync_result = sync_initiative_by_id(initiative_id)
                if sync_result["errors"]:
                    logger.error("Initiative sync errors: %s", sync_result["errors"])
                else:
                    logger.info("Synced initiative for document: %s", initiative_id)
            except Exception as e:
                logger.error("Failed to sync initiative for document: %s", e)
            return JSONResponse(content={"status": "ok"})

        # No parent initiative - just log and return
        logger.info("Document has no parent initiative, skipping sync")
        return JSONResponse(content={"status": "ok"})

    # Handle ProjectUpdate events (direct to Obsidian Daily Action and Weekly Cycle)
    if event_type == "ProjectUpdate" and action in ("create", "update"):
        project_name = event_data.get("project", {}).get("name", "(unknown project)")
        update_body = event_data.get("body", "(no content)")
        update_url = data.get("url", "")

        logger.info(
            "📋 Linear project update | %s | %s | %s",
            project_name,
            action,
            update_body[:100],
        )

        # Write to both Daily Action and Weekly Cycle
        result = upsert_linear_update(
            section_type="project",
            url=update_url,
            parent_name=project_name,
            content=update_body,
        )
        if result["daily_action_success"]:
            logger.info("Written to Daily Action: action=%s", result["daily_action_action"])
        else:
            logger.error("Failed to write to Daily Action: %s", result.get("daily_action_error"))
        if result["weekly_cycle_success"]:
            logger.info("Written to Weekly Cycle: action=%s", result["weekly_cycle_action"])
        else:
            logger.error("Failed to write to Weekly Cycle: %s", result.get("weekly_cycle_error"))
            # Still return 200 - don't want Linear to retry

        # Sync the parent initiative to Obsidian _Initiatives folder
        project_id = event_data.get("project", {}).get("id")
        if project_id:
            try:
                from scripts.linear.sync_single_initiative import sync_initiative_for_project
                sync_result = sync_initiative_for_project(project_id)
                if sync_result["errors"]:
                    logger.error("Initiative sync errors: %s", sync_result["errors"])
                else:
                    logger.info("Synced initiative for project: %s", project_id)
            except Exception as e:
                logger.error("Failed to sync initiative for project: %s", e)

        return JSONResponse(content={"status": "ok"})

    # Handle InitiativeUpdate events (direct to Obsidian Daily Action and Weekly Cycle)
    if event_type == "InitiativeUpdate" and action in ("create", "update"):
        initiative_name = event_data.get("initiative", {}).get("name", "(unknown initiative)")
        update_body = event_data.get("body", "(no content)")
        update_url = data.get("url", "")

        logger.info(
            "🎯 Linear initiative update | %s | %s | %s",
            initiative_name,
            action,
            update_body[:100],
        )

        # Write to both Daily Action and Weekly Cycle
        result = upsert_linear_update(
            section_type="initiative",
            url=update_url,
            parent_name=initiative_name,
            content=update_body,
        )
        if result["daily_action_success"]:
            logger.info("Written to Daily Action: action=%s", result["daily_action_action"])
        else:
            logger.error("Failed to write to Daily Action: %s", result.get("daily_action_error"))
        if result["weekly_cycle_success"]:
            logger.info("Written to Weekly Cycle: action=%s", result["weekly_cycle_action"])
        else:
            logger.error("Failed to write to Weekly Cycle: %s", result.get("weekly_cycle_error"))
            # Still return 200 - don't want Linear to retry

        # Sync the initiative to Obsidian _Initiatives folder
        initiative_id = event_data.get("initiative", {}).get("id")
        if initiative_id:
            try:
                from scripts.linear.sync_single_initiative import sync_initiative_by_id
                sync_result = sync_initiative_by_id(initiative_id)
                if sync_result["errors"]:
                    logger.error("Initiative sync errors: %s", sync_result["errors"])
                else:
                    logger.info("Synced initiative: %s", initiative_id)
            except Exception as e:
                logger.error("Failed to sync initiative: %s", e)

        return JSONResponse(content={"status": "ok"})

    # Handle Issue events (issues touched tracking + completion)
    if event_type == "Issue" and action in ("create", "update"):
        issue_title = event_data.get("title", "(no title)")
        issue_number = event_data.get("number")
        team = event_data.get("team", {})
        team_key = team.get("key", "")
        issue_identifier = f"{team_key}-{issue_number}" if team_key and issue_number else ""
        project_name = event_data.get("project", {}).get("name", "") if event_data.get("project") else ""
        status_name = event_data.get("state", {}).get("name", "") if event_data.get("state") else ""
        issue_url = event_data.get("url", "")
        updated_from = data.get("updatedFrom", {})
        status_changed = "stateId" in updated_from

        # Track issue in Linear Issues Touched section
        if issue_identifier:
            logger.info(
                "📌 Linear issue touched | %s | %s | %s",
                issue_identifier,
                status_name,
                action,
            )

            touched_result = upsert_issue_touched(
                issue_identifier=issue_identifier,
                project_name=project_name,
                issue_title=issue_title,
                status_name=status_name,
                issue_url=issue_url,
                status_changed=status_changed,
            )
            if touched_result["daily_action_success"]:
                logger.info("Issues touched written to Daily Action: action=%s", touched_result["daily_action_action"])
            else:
                logger.error("Failed to write issues touched to Daily Action: %s", touched_result.get("daily_action_error"))
            if touched_result["weekly_cycle_success"]:
                logger.info("Issues touched written to Weekly Cycle: action=%s", touched_result["weekly_cycle_action"])
            else:
                logger.error("Failed to write issues touched to Weekly Cycle: %s", touched_result.get("weekly_cycle_error"))

        # Handle issue completion (existing Todoist flow)
        if event_data.get("completedAt"):
            if team_key and issue_number:
                task_content = f"{team_key}-{issue_number}: {issue_title}"
            else:
                task_content = issue_title

            logger.info(
                "✅ Linear issue completed | %s | %s",
                task_content[:100],
                action,
            )

            # Create and complete task in Todoist
            # This will trigger Todoist's webhook which writes to Obsidian
            result = create_completed_todoist_task(task_content)
            if result["success"]:
                logger.info("Created and completed Todoist task: id=%s", result["task_id"])
            else:
                logger.error("Failed to create/complete Todoist task: %s", result.get("error"))
                # Still return 200 - don't want Linear to retry

    else:
        logger.info("Linear event: type=%s action=%s (ignored)", event_type, action)

    return JSONResponse(content={"status": "ok"})


# GitHub webhook
def verify_github_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature (hex-encoded with sha256= prefix)."""
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/github/webhook")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
    x_github_event: str | None = Header(None, alias="X-GitHub-Event"),
):
    """Receive GitHub webhook events for merged PRs and commits to main."""
    # Get raw payload for signature verification
    payload = await request.body()

    # Verify signature
    if GITHUB_WEBHOOK_SECRET and x_hub_signature_256:
        if not verify_github_signature(payload, x_hub_signature_256, GITHUB_WEBHOOK_SECRET):
            logger.warning("Invalid GitHub signature")
            raise HTTPException(status_code=401, detail="Invalid signature")
    elif GITHUB_WEBHOOK_SECRET and not x_hub_signature_256:
        logger.warning("Missing GitHub signature header")
        raise HTTPException(status_code=401, detail="Missing signature")

    # Parse payload
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.debug("GitHub webhook received: %s", data)

    # Handle push events (commits to main/master)
    if x_github_event == "push":
        ref = data.get("ref", "")

        # Only process pushes to main or master
        if ref not in ("refs/heads/main", "refs/heads/master"):
            logger.info("GitHub push to %s (not main/master), ignoring", ref)
            return JSONResponse(content={"status": "ignored"})

        # Filter by pusher
        pusher = data.get("pusher", {}).get("name")
        if GITHUB_USERNAME and pusher != GITHUB_USERNAME:
            logger.info("GitHub push by %s (not target user %s), ignoring", pusher, GITHUB_USERNAME)
            return JSONResponse(content={"status": "ignored"})

        repo = data.get("repository", {})
        repo_name = repo.get("name", "unknown-repo")
        commits = data.get("commits", [])

        for commit in commits:
            # Skip merge commits
            commit_message = commit.get("message", "(no message)")
            if commit_message.startswith("Merge pull request") or commit_message.startswith("Merge branch"):
                continue

            commit_message_first_line = commit_message.split("\n")[0]
            commit_sha = commit.get("id", "")[:7]  # Short SHA
            commit_url = commit.get("url", "")

            logger.info(
                "📝 GitHub commit | %s | %s | %s",
                repo_name,
                commit_sha,
                commit_message_first_line[:100],
            )

            # Create and complete task in Todoist
            # This will trigger Todoist's webhook which writes to Obsidian
            short_message = commit_message_first_line[:50] + "..." if len(commit_message_first_line) > 50 else commit_message_first_line
            task_content = f"{repo_name}: {short_message}"
            result = create_completed_todoist_task(task_content)
            if result["success"]:
                logger.info("Created and completed Todoist task: id=%s", result["task_id"])
            else:
                logger.error("Failed to create/complete Todoist task: %s", result.get("error"))
                # Still return 200 - don't want GitHub to retry

        return JSONResponse(content={"status": "ok"})

    logger.info("GitHub event: %s (ignored)", x_github_event)
    return JSONResponse(content={"status": "ignored"})


# Manus webhook
_manus_public_key_cache: dict = {"key": None, "fetched_at": 0}


def fetch_manus_public_key() -> str:
    """Fetch Manus webhook public key from API. Caches for 1 hour."""
    cache = _manus_public_key_cache
    now = time.time()
    if cache["key"] and (now - cache["fetched_at"]) < 3600:
        return cache["key"]

    response = requests.get(
        "https://api.manus.ai/v1/webhook/public_key",
        headers={"Authorization": f"Bearer {MANUS_API_KEY}"},
        timeout=10,
    )
    response.raise_for_status()
    public_key_pem = response.json().get("public_key", "")
    cache["key"] = public_key_pem
    cache["fetched_at"] = now
    return public_key_pem


def verify_manus_signature(
    payload: bytes,
    signature_b64: str,
    timestamp: str,
    request_url: str,
    public_key_pem: str,
) -> bool:
    """Verify Manus webhook RSA-SHA256 signature."""
    try:
        body_sha256 = hashlib.sha256(payload).hexdigest()
        signed_content = f"{timestamp}.{request_url}.{body_sha256}"

        public_key = serialization.load_pem_public_key(public_key_pem.encode())
        signature_bytes = base64.b64decode(signature_b64)

        public_key.verify(
            signature_bytes,
            signed_content.encode(),
            crypto_padding.PKCS1v15(),
            crypto_hashes.SHA256(),
        )
        return True
    except Exception:
        return False


def _is_manus_timestamp_valid(timestamp: str, max_age_seconds: int = 300) -> bool:
    """Check if the Manus webhook timestamp is within acceptable range (default 5 min)."""
    try:
        ts = int(timestamp)
        return abs(time.time() - ts) <= max_age_seconds
    except (ValueError, TypeError):
        return False


@app.post("/manus/webhook")
async def manus_webhook(
    request: Request,
    x_webhook_signature: str | None = Header(None, alias="X-Webhook-Signature"),
    x_webhook_timestamp: str | None = Header(None, alias="X-Webhook-Timestamp"),
):
    """Receive Manus AI webhook events."""
    # Allow verification pings (registration test requests without signature headers)
    if not x_webhook_signature and not x_webhook_timestamp:
        logger.info("Manus webhook verification ping received")
        return JSONResponse(content={"status": "received"})

    if not x_webhook_signature or not x_webhook_timestamp:
        logger.warning("Missing Manus webhook signature headers")
        raise HTTPException(status_code=401, detail="Missing signature headers")

    if not _is_manus_timestamp_valid(x_webhook_timestamp):
        logger.warning("Manus webhook timestamp expired or invalid: %s", x_webhook_timestamp)
        raise HTTPException(status_code=401, detail="Timestamp expired")

    payload = await request.body()

    try:
        public_key_pem = fetch_manus_public_key()
    except Exception as e:
        logger.error("Failed to fetch Manus public key: %s", e)
        raise HTTPException(status_code=500, detail="Unable to verify signature")

    # Use WEBHOOK_BASE_URL for the external URL that Manus signed against
    # str(request.url) returns the internal Docker URL which won't match
    if WEBHOOK_BASE_URL:
        request_url = WEBHOOK_BASE_URL.rstrip("/") + "/manus/webhook"
    else:
        request_url = str(request.url)

    logger.info(
        "Manus webhook debug | sig=%s... | ts=%s | url=%s | body_len=%d",
        x_webhook_signature[:20] if x_webhook_signature else "none",
        x_webhook_timestamp,
        request_url,
        len(payload),
    )

    if not verify_manus_signature(payload, x_webhook_signature, x_webhook_timestamp, request_url, public_key_pem):
        logger.warning("Invalid Manus webhook signature | internal_url=%s", str(request.url))
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = data.get("event_type", "unknown")
    task_detail = data.get("task_detail", {})
    progress_detail = data.get("progress_detail", {})

    # task_id is in task_detail for created/stopped, progress_detail for progress
    task_id = task_detail.get("task_id") or progress_detail.get("task_id", "unknown")
    task_title = task_detail.get("task_title", "")
    task_url = task_detail.get("task_url", "")

    logger.info(
        "Manus webhook | event=%s | task_id=%s | title=%s",
        event_type,
        task_id,
        task_title or "(no title)",
    )

    if event_type == "task_created":
        if not task_url:
            task_url = f"https://manus.im/app/{task_id}"

        result = upsert_manus_task(task_id, task_title or "Untitled Task", task_url)
        if result["daily_action_success"]:
            logger.info("Manus task written to Daily Action: action=%s", result["daily_action_action"])
        else:
            logger.error("Failed to write Manus task to Daily Action: %s", result.get("daily_action_error"))
        if result["weekly_cycle_success"]:
            logger.info("Manus task written to Weekly Cycle: action=%s", result["weekly_cycle_action"])
        else:
            logger.error("Failed to write Manus task to Weekly Cycle: %s", result.get("weekly_cycle_error"))
    elif event_type == "task_progress":
        logger.info("Manus task progress: %s | %s", task_id, progress_detail.get("message", ""))
    elif event_type == "task_stopped":
        stop_reason = task_detail.get("stop_reason", "")
        logger.info("Manus task stopped: %s | reason=%s", task_id, stop_reason)
        # Also write to Obsidian - task_stopped has full task_detail
        if task_title and task_id != "unknown":
            if not task_url:
                task_url = f"https://manus.im/app/{task_id}"
            result = upsert_manus_task(task_id, task_title, task_url)
            if result["daily_action_success"]:
                logger.info("Manus task written to Daily Action: action=%s", result["daily_action_action"])
            else:
                logger.error("Failed to write Manus task to Daily Action: %s", result.get("daily_action_error"))
            if result["weekly_cycle_success"]:
                logger.info("Manus task written to Weekly Cycle: action=%s", result["weekly_cycle_action"])
            else:
                logger.error("Failed to write Manus task to Weekly Cycle: %s", result.get("weekly_cycle_error"))
    else:
        logger.info("Manus event: %s (unhandled)", event_type)

    return JSONResponse(content={"status": "received"})


def _process_shared_link(url: str, title: str | None) -> None:
    """Background task to save shared link to Obsidian."""
    result = add_shared_link(url, title)
    if result["success"]:
        logger.info("Saved shared link: %s (action=%s)", url[:100], result.get("action"))
    else:
        logger.error("Failed to save link: %s - %s", url[:100], result["error"])


def _process_youtube_link(url: str) -> None:
    """Background task to save YouTube link to Obsidian."""
    result = add_youtube_link(url)
    if result["success"]:
        logger.info("Saved YouTube link: %s (action=%s)", url[:100], result.get("action"))
    else:
        logger.error("Failed to save YouTube link: %s - %s", url[:100], result["error"])


# Link sharing endpoint
@app.post("/share/link", status_code=202)
async def share_link(
    link_request: LinkShareRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str | None = Header(None),
):
    """Save a shared link to Obsidian Knowledge Hub."""
    if x_api_key != LINK_SHARE_API_KEY:
        logger.warning("Invalid API key for link share")
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Detect YouTube URLs and route to YouTube processing
    if is_valid_youtube_url(link_request.url):
        logger.info(
            "📺 Link share (YouTube) | url=%s | title=%s",
            link_request.url[:100],
            link_request.title[:50] if link_request.title else "(none)",
        )

        path_info = get_predicted_link_path(link_request.url, link_request.title)

        background_tasks.add_task(
            _process_youtube_link,
            link_request.url,
        )

        return {
            "status": "accepted",
            "message": "YouTube link queued for processing",
            "vault_name": path_info["vault_name"],
            "file_path": path_info["file_path"],
        }

    # Non-YouTube: use generic web content extraction
    logger.info(
        "🔗 Link share | url=%s | title=%s",
        link_request.url[:100],
        link_request.title[:50] if link_request.title else "(none)",
    )

    path_info = get_predicted_link_path(link_request.url, link_request.title)

    background_tasks.add_task(
        _process_shared_link,
        link_request.url,
        link_request.title,
    )

    return {
        "status": "accepted",
        "message": "Link queued for processing",
        "vault_name": path_info["vault_name"],
        "file_path": path_info["file_path"],
    }


# YouTube sharing endpoint
@app.post("/share/youtube", status_code=202)
async def share_youtube(
    youtube_request: YouTubeShareRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str | None = Header(None),
):
    """Save a YouTube video link to Obsidian Knowledge Hub."""
    if x_api_key != LINK_SHARE_API_KEY:
        logger.warning("Invalid API key for YouTube share")
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Validate YouTube URL before accepting
    if not is_valid_youtube_url(youtube_request.url):
        raise HTTPException(
            status_code=422,
            detail="Invalid YouTube URL. Supported formats: youtube.com/watch, youtu.be, youtube.com/shorts, youtube.com/embed"
        )

    logger.info(
        "📺 YouTube share | url=%s",
        youtube_request.url[:100],
    )

    background_tasks.add_task(
        _process_youtube_link,
        youtube_request.url,
    )

    return {"status": "accepted", "message": "YouTube link queued for processing"}


# Scheduler endpoints
@app.get("/scheduler/jobs")
async def list_scheduled_jobs():
    """List all scheduled jobs and their next run times."""
    from scheduler import get_jobs_status

    return {"jobs": get_jobs_status()}


@app.post("/scheduler/jobs/{job_id}/run")
async def trigger_job(job_id: str):
    """Trigger a scheduled job to run immediately."""
    from scheduler import run_job_now

    if run_job_now(job_id):
        return {"status": "triggered", "job_id": job_id}
    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
