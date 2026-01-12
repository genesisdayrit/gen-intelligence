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
from services.obsidian.append_completed_task import append_completed_task
from services.obsidian.upsert_linear_update import upsert_linear_update
from services.obsidian.remove_todoist_completed import remove_todoist_completed
from services.obsidian.update_telegram_log import update_telegram_log
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
                from app.scripts.linear.sync_single_initiative import sync_initiative_for_project
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
                from app.scripts.linear.sync_single_initiative import sync_initiative_for_project
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
                from app.scripts.linear.sync_single_initiative import sync_initiative_by_id
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
                from app.scripts.linear.sync_single_initiative import sync_initiative_for_project
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
                from app.scripts.linear.sync_single_initiative import sync_initiative_by_id
                sync_result = sync_initiative_by_id(initiative_id)
                if sync_result["errors"]:
                    logger.error("Initiative sync errors: %s", sync_result["errors"])
                else:
                    logger.info("Synced initiative: %s", initiative_id)
            except Exception as e:
                logger.error("Failed to sync initiative: %s", e)

        return JSONResponse(content={"status": "ok"})

    # Handle issue completion
    # An issue is completed when completedAt is set
    if event_type == "Issue" and event_data.get("completedAt"):
        issue_title = event_data.get("title", "(no title)")
        issue_number = event_data.get("number")
        team = event_data.get("team", {})
        team_key = team.get("key", "")

        # Format: ENG-123: Issue title
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
