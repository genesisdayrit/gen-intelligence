"""APScheduler configuration for scheduled jobs.

All scheduled jobs are defined here. To add a new job:
1. Import the callable function (not the CLI main())
2. Add a wrapper and an entry to SCHEDULED_JOBS
3. The scheduler starts/stops via FastAPI lifespan in main.py

Jobs run inside the FastAPI process using BackgroundScheduler (thread pool).
"""

import logging
import os
from datetime import datetime

import pytz
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job wrappers (lazy imports to avoid import-time side effects)
# ---------------------------------------------------------------------------

def _send_cycle_summary():
    from scripts.send_cycle_summary_email import run_cycle_summary_email

    return run_cycle_summary_email(current=False, all_initiatives=False)


def _send_arxiv_email():
    from scripts.send_arxiv_email import run_arxiv_email

    return run_arxiv_email()


def _send_linear_digest_email():
    from scripts.send_linear_digest_email import run_linear_digest_email

    return run_linear_digest_email()


def _fetch_manus_tasks():
    from services.manus.fetch_manus_tasks import fetch_and_upsert_manus_tasks

    return fetch_and_upsert_manus_tasks()


# ---------------------------------------------------------------------------
# Job registry
# ---------------------------------------------------------------------------

SCHEDULED_JOBS = [
    {
        "id": "send_cycle_summary_email_tue",
        "name": "Weekly Cycle Summary Email (Tuesday)",
        "func": _send_cycle_summary,
        "trigger": CronTrigger(
            day_of_week="tue",
            hour=10,
            minute=0,
            timezone=os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles"),
        ),
    },
    {
        "id": "send_cycle_summary_email",
        "name": "Weekly Cycle Summary Email",
        "func": _send_cycle_summary,
        "trigger": CronTrigger(
            day_of_week="wed",
            hour=3,
            minute=30,
            timezone=os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles"),
        ),
    },
    {
        "id": "send_arxiv_email",
        "name": "Daily ArXiv Articles Email",
        "func": _send_arxiv_email,
        "trigger": CronTrigger(
            hour=4,
            minute=0,
            timezone=os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles"),
        ),
    },
    {
        "id": "send_linear_digest_email",
        "name": "Daily Linear Issues Digest Email",
        "func": _send_linear_digest_email,
        "trigger": CronTrigger(
            hour=19,
            minute=0,
            timezone="America/Los_Angeles",
        ),
    },
    {
        "id": "fetch_manus_tasks",
        "name": "Fetch Manus Tasks",
        "func": _fetch_manus_tasks,
        "trigger": CronTrigger(
            minute="*/30",
            timezone=os.getenv("SYSTEM_TIMEZONE", "America/Los_Angeles"),
        ),
    },
]


# ---------------------------------------------------------------------------
# Event listener
# ---------------------------------------------------------------------------

def _job_listener(event):
    job_id = event.job_id
    if hasattr(event, "exception") and event.exception:
        logger.error("Scheduled job FAILED: %s | exception=%s", job_id, event.exception)
    elif event.code == EVENT_JOB_MISSED:
        logger.warning("Scheduled job MISSED: %s", job_id)
    else:
        logger.info("Scheduled job completed: %s | return=%s", job_id, event.retval)


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler()


def start_scheduler():
    """Register all jobs and start the scheduler."""
    scheduler.add_listener(_job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED)

    for job_def in SCHEDULED_JOBS:
        scheduler.add_job(
            func=job_def["func"],
            trigger=job_def["trigger"],
            id=job_def["id"],
            name=job_def["name"],
            replace_existing=True,
            misfire_grace_time=3600,
        )

    scheduler.start()

    for job in scheduler.get_jobs():
        logger.info("Registered job: %s | next_run=%s", job.id, job.next_run_time)
    logger.info("Scheduler started with %d job(s)", len(SCHEDULED_JOBS))


def shutdown_scheduler():
    """Gracefully shut down the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=True)
        logger.info("Scheduler shut down")


# ---------------------------------------------------------------------------
# Helpers for API endpoints
# ---------------------------------------------------------------------------

def get_jobs_status():
    """Return current status of all scheduled jobs."""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
        })
    return jobs


def run_job_now(job_id):
    """Trigger a scheduled job to run immediately. Returns True if found."""
    job = scheduler.get_job(job_id)
    if job is None:
        return False
    scheduler.modify_job(job_id, next_run_time=datetime.now(pytz.utc))
    return True
