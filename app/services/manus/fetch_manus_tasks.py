"""Fetch today's Manus tasks and upsert them into Obsidian notes."""

import logging
import os
from datetime import datetime

import httpx
import pytz
from dotenv import load_dotenv

from services.obsidian.add_manus_task_touched import upsert_manus_task_touched
from services.obsidian.utils.date_helpers import get_effective_date

load_dotenv()

logger = logging.getLogger(__name__)

MANUS_API_BASE = "https://api.manus.ai/v1"
MANUS_API_KEY = os.getenv("MANUS_API_KEY")
SYSTEM_TIMEZONE = os.getenv("SYSTEM_TIMEZONE", "US/Eastern")


def fetch_and_upsert_manus_tasks() -> dict:
    """Fetch today's Manus tasks and upsert each into Daily Action and Weekly Cycle.

    Returns:
        dict with keys: tasks_found, tasks_upserted, errors
    """
    if not MANUS_API_KEY:
        logger.warning("MANUS_API_KEY not set, skipping Manus task fetch")
        return {"tasks_found": 0, "tasks_upserted": 0, "errors": ["MANUS_API_KEY not set"]}

    tz = pytz.timezone(SYSTEM_TIMEZONE)
    now = datetime.now(tz)
    effective_date = get_effective_date(now).date()

    tasks_found = 0
    tasks_upserted = 0
    errors = []

    try:
        has_more = True
        last_id = None

        while has_more:
            params = {"limit": 20}
            if last_id:
                params["last_id"] = last_id

            response = httpx.get(
                f"{MANUS_API_BASE}/tasks",
                headers={"API_KEY": MANUS_API_KEY},
                params=params,
                timeout=15,
            )

            if response.status_code != 200:
                errors.append(f"API error: {response.status_code} - {response.text}")
                break

            data = response.json()
            tasks = data.get("data", [])
            has_more = data.get("has_more", False)
            last_id = data.get("last_id")

            if not tasks:
                break

            for task in tasks:
                created_at = int(task.get("created_at", 0))
                task_date = datetime.fromtimestamp(created_at, tz=tz)
                task_effective_date = get_effective_date(task_date).date()

                if task_effective_date < effective_date:
                    # Tasks are ordered newest first; once we hit older tasks, stop
                    has_more = False
                    break

                if task_effective_date == effective_date:
                    tasks_found += 1
                    task_id = task.get("id", "unknown")
                    metadata = task.get("metadata", {})
                    task_title = metadata.get("task_title", "Untitled Task")
                    task_url = metadata.get("task_url", f"https://manus.im/app/{task_id}")

                    result = upsert_manus_task_touched(task_id, task_title, task_url)

                    if result["daily_action_success"] or result["weekly_cycle_success"]:
                        tasks_upserted += 1
                    if not result["daily_action_success"]:
                        errors.append(f"DA fail for {task_id}: {result.get('daily_action_error')}")
                    if not result["weekly_cycle_success"]:
                        errors.append(f"WC fail for {task_id}: {result.get('weekly_cycle_error')}")

    except Exception as e:
        errors.append(str(e))
        logger.error("Error fetching Manus tasks: %s", e)

    logger.info(
        "Manus task fetch complete: found=%d, upserted=%d, errors=%d",
        tasks_found, tasks_upserted, len(errors),
    )

    return {"tasks_found": tasks_found, "tasks_upserted": tasks_upserted, "errors": errors}
