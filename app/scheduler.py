"""APScheduler configuration for scheduled jobs.

All scheduled jobs are defined here. To add a new job:
1. Import the callable function (not the CLI main())
2. Add a wrapper and an entry to SCHEDULED_JOBS
3. The scheduler starts/stops via FastAPI lifespan in main.py

Jobs run inside the FastAPI process using BackgroundScheduler (thread pool).
"""

import logging
from datetime import datetime

import pytz
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import SYSTEM_TZ

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job wrappers (lazy imports to avoid import-time side effects)
# ---------------------------------------------------------------------------

def _send_cycle_summary_current():
    from scripts.send_cycle_summary_email import run_cycle_summary_email

    return run_cycle_summary_email(current=True, all_initiatives=False)


def _send_cycle_summary_previous():
    from scripts.send_cycle_summary_email import run_cycle_summary_email

    return run_cycle_summary_email(current=False, all_initiatives=False)


def _send_arxiv_email():
    from scripts.send_arxiv_email import run_arxiv_email

    return run_arxiv_email()


def _send_linear_digest_email():
    from scripts.send_linear_digest_email import run_linear_digest_email

    return run_linear_digest_email()


def _send_plato_email():
    from scripts.send_plato_email import run_plato_email

    return run_plato_email()


def _send_essay_ideas_from_journal():
    from scripts.send_essay_ideas_from_journal import run_essay_ideas_from_journal

    return run_essay_ideas_from_journal()


def _fetch_manus_tasks():
    from services.manus.fetch_manus_tasks import fetch_and_upsert_manus_tasks

    return fetch_and_upsert_manus_tasks()


def _send_daily_initiative_update():
    from scripts.send_daily_initiative_update import run_daily_initiative_update

    return run_daily_initiative_update()


def _send_main_thread_rollup():
    from scripts.send_main_thread_rollup import run_main_thread_rollup

    # Runs every 6 hours; use a 6-hour look-back so windows are contiguous.
    return run_main_thread_rollup(hours=6)


def _backfill_readwise_highlights(since=None, updated_after=None, lookback_days=None):
    from services.readwise.backfill import backfill_readwise_highlights

    return backfill_readwise_highlights(
        since=since,
        updated_after=updated_after,
        lookback_days=lookback_days,
    )


def _send_sunday_wrap_up_email():
    from scripts.send_sunday_wrap_up_email import run_sunday_wrap_up_email

    return run_sunday_wrap_up_email()


def _backfill_knowledge_hub_buffet(since=None):
    from services.obsidian.backfill_knowledge_hub_buffet import backfill_knowledge_hub_buffet

    return backfill_knowledge_hub_buffet(since=since)


def _sync_granola_notes(updated_after=None):
    from services.granola.sync import sync_granola_notes

    return sync_granola_notes(updated_after=updated_after)


def _backfill_granola_notes(updated_after=None, lookback_days=None, since=None):
    from services.granola.backfill import backfill_granola_notes

    return backfill_granola_notes(
        updated_after=updated_after,
        lookback_days=lookback_days,
        since=since,
    )


def _create_daily_journal(use_today=False):
    from scripts.obsidian.workflows.file_creation.create_daily_journal import (
        create_daily_journal,
    )

    return create_daily_journal(use_today=use_today)


def _create_daily_action(use_today=False):
    from scripts.obsidian.workflows.file_creation.create_daily_action import (
        create_daily_action,
    )

    return create_daily_action(use_today=use_today)


def _update_daily_journal_properties(use_today=False):
    from scripts.obsidian.workflows.file_updates.update_daily_journal_properties import (
        update_daily_journal_properties,
    )

    return update_daily_journal_properties(use_today=use_today)


def _add_daily_review_section():
    from scripts.obsidian.workflows.file_updates.add_daily_review_section import (
        add_daily_review_section,
    )

    return add_daily_review_section()


def _update_modified_files_today():
    from scripts.obsidian.workflows.file_updates.update_modified_files_today import (
        update_modified_files_today,
    )

    return update_modified_files_today()


def _create_weeks():
    from scripts.obsidian.workflows.file_creation.create_weeks import create_weeks

    return create_weeks()


def _create_newsletter_page():
    from scripts.obsidian.workflows.file_creation.create_newsletter_page import (
        create_newsletter_page,
    )

    return create_newsletter_page()


def _create_new_cycle_page():
    from scripts.obsidian.workflows.file_creation.create_new_cycle_page import (
        create_new_cycle_page,
    )

    return create_new_cycle_page()


def _create_weekly_health_review_page():
    from scripts.obsidian.workflows.file_creation.create_weekly_health_review_page import (
        create_weekly_health_review_page,
    )

    return create_weekly_health_review_page()


def _create_weekly_map():
    from scripts.obsidian.workflows.file_creation.create_weekly_map import (
        create_weekly_map,
    )

    return create_weekly_map()


def _create_cycle_and_cooling_period_pages():
    from scripts.obsidian.workflows.file_creation.create_cycle_and_cooling_period_pages import (
        create_cycle_and_cooling_period_pages,
    )

    return create_cycle_and_cooling_period_pages()


def _spotify_sync_shazam_to_library():
    from services.spotify.sync import sync_shazam_to_library

    return sync_shazam_to_library()


def _spotify_sync_saved_today_to_half_year():
    from services.spotify.sync import sync_saved_today_to_half_year

    return sync_saved_today_to_half_year()


def _spotify_create_half_year_playlist():
    from services.spotify.sync import create_half_year_playlist

    return create_half_year_playlist()


def _spotify_music_of_the_day(date=None):
    from services.spotify.music_of_the_day import write_music_of_the_day

    return write_music_of_the_day(date=date)


# Job ids that accept ``use_today`` on POST /scheduler/jobs/{id}/run.
# Default False creates tomorrow's files (evening-before cadence).
DAILY_CREATION_JOB_IDS = frozenset({
    "create_daily_journal",
    "create_daily_action",
    "update_daily_journal_properties",
})

# Remaining gd-second-brain-os crontab jobs migrated onto APScheduler
# after the evening-before daily-creation cluster (PR 198).
# daily_prep and daily_reflection are intentionally omitted (scripts still
# in-repo; re-register if the AM/PM Vision check-ins should run again).
OBSIDIAN_CRON_MIGRATION_JOB_IDS = frozenset({
    "add_daily_review_section",
    "update_modified_files_today",
    "create_weeks",
    "create_newsletter_page",
    "create_new_cycle_page",
    "create_weekly_health_review_page",
    "create_weekly_map",
    "create_cycle_and_cooling_period_pages",
})

# personal-ec2 spotify-api crontab jobs. No token-refresh job — access
# tokens refresh on demand from SPOTIFY_REFRESH_TOKEN.
SPOTIFY_SCHEDULED_JOB_IDS = frozenset({
    "spotify_sync_shazam_to_library",
    "spotify_sync_saved_today_to_half_year",
    "spotify_create_half_year_playlist",
    "spotify_music_of_the_day",
})


# ---------------------------------------------------------------------------
# Job registry
# ---------------------------------------------------------------------------

SCHEDULED_JOBS = [
    {
        "id": "send_cycle_summary_email_tue",
        "name": "Weekly Cycle Summary Email (Tuesday)",
        "func": _send_cycle_summary_current,
        "trigger": CronTrigger(
            day_of_week="tue",
            hour=4,
            minute=30,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "send_cycle_summary_email",
        "name": "Weekly Cycle Summary Email",
        "func": _send_cycle_summary_previous,
        "trigger": CronTrigger(
            day_of_week="wed",
            hour=3,
            minute=30,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "send_arxiv_email",
        "name": "Daily ArXiv Articles Email",
        "func": _send_arxiv_email,
        "trigger": CronTrigger(
            hour=4,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "send_linear_digest_email",
        "name": "Daily Linear Issues Digest Email",
        "func": _send_linear_digest_email,
        "trigger": CronTrigger(
            hour=19,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "send_plato_email",
        "name": "Daily Plato Random Entry Email",
        "func": _send_plato_email,
        "trigger": CronTrigger(
            hour=17,
            minute=30,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "send_essay_ideas_from_journal",
        "name": "Daily Essay Ideas From Journal Email",
        "func": _send_essay_ideas_from_journal,
        "trigger": CronTrigger(
            hour=4,
            minute=30,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "fetch_manus_tasks",
        "name": "Fetch Manus Tasks",
        "func": _fetch_manus_tasks,
        "trigger": CronTrigger(
            minute="*/30",
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "send_daily_initiative_update",
        "name": "Daily Initiative Update on Active Linear Initiative",
        "func": _send_daily_initiative_update,
        "trigger": CronTrigger(
            hour=4,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "send_main_thread_rollup",
        "name": "Main-Thread Rollup Initiative Update (every 6h)",
        "func": _send_main_thread_rollup,
        # 05:30, 11:30, 17:30, 23:30 Pacific.
        "trigger": CronTrigger(
            hour="5,11,17,23",
            minute=30,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "backfill_readwise_highlights",
        "name": "Backfill Readwise Highlights (manual)",
        "func": _backfill_readwise_highlights,
        # Not on a cadence. Year 2099 keeps the job registered so
        # POST /scheduler/jobs/backfill_readwise_highlights/run can fire it
        # without a DateTrigger disappearing after one run.
        "trigger": CronTrigger(
            year=2099,
            month=1,
            day=1,
            hour=0,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "send_sunday_wrap_up_email",
        "name": "Sunday Wrap-up Email",
        "func": _send_sunday_wrap_up_email,
        "trigger": CronTrigger(
            day_of_week="sun",
            hour=6,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "backfill_knowledge_hub_buffet",
        "name": "Backfill Knowledge Hub Content Buffet (manual)",
        "func": _backfill_knowledge_hub_buffet,
        # Not on a cadence. Year 2099 keeps the job registered so
        # POST /scheduler/jobs/backfill_knowledge_hub_buffet/run can fire it
        # without a DateTrigger disappearing after one run.
        "trigger": CronTrigger(
            year=2099,
            month=1,
            day=1,
            hour=0,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "sync_granola_notes",
        "name": "Sync Granola Notes to Daily Journal (manual)",
        "func": _sync_granola_notes,
        # Live notes arrive via POST /granola/webhook. Year 2099 keeps this
        # incremental pull registered as a manual safety net so
        # POST /scheduler/jobs/sync_granola_notes/run can still fire it.
        "trigger": CronTrigger(
            year=2099,
            month=1,
            day=1,
            hour=0,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "backfill_granola_notes",
        "name": "Backfill Granola Notes (manual)",
        "func": _backfill_granola_notes,
        # Not on a cadence. Year 2099 keeps the job registered so
        # POST /scheduler/jobs/backfill_granola_notes/run can fire it
        # without a DateTrigger disappearing after one run.
        "trigger": CronTrigger(
            year=2099,
            month=1,
            day=1,
            hour=0,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "create_daily_journal",
        "name": "Create Daily Journal (tomorrow)",
        "func": _create_daily_journal,
        # Evening-before: 6:00pm system tz is 9:00pm ET year-round, matching
        # the old crontab comment for 01:00 UTC during EDT.
        "trigger": CronTrigger(
            hour=18,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "create_daily_action",
        "name": "Create Daily Action (tomorrow)",
        "func": _create_daily_action,
        "trigger": CronTrigger(
            hour=18,
            minute=5,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "update_daily_journal_properties",
        "name": "Update Daily Journal Properties (tomorrow)",
        "func": _update_daily_journal_properties,
        "trigger": CronTrigger(
            hour=18,
            minute=10,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "add_daily_review_section",
        "name": "Add Daily Review Section to Daily Action",
        "func": _add_daily_review_section,
        # crontab_generation.py: 0 20 * * * UTC (commented 3:00pm ET / EST).
        # This port writes the section if missing; not a no-op.
        "trigger": CronTrigger(
            hour=13,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "update_modified_files_today",
        "name": "Update Folder-Journal Relations",
        "func": _update_modified_files_today,
        # Live host: */10. crontab_generation.py used 5 0 * * * UTC.
        # */15 is less chatty than every 10m; paths_to_check lists 13
        # Dropbox folders (non-recursive list_folder). Redis last-run
        # cutoff keeps each pass incremental.
        "trigger": CronTrigger(
            minute="*/15",
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "create_weeks",
        "name": "Create Week-Ending Page",
        "func": _create_weeks,
        # crontab_generation.py: 0 6 * * 1 (commented Mon 1:00am CT / CDT).
        "trigger": CronTrigger(
            day_of_week="sun",
            hour=23,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "create_newsletter_page",
        "name": "Create Weekly Newsletter Page",
        "func": _create_newsletter_page,
        # crontab_generation.py: 30 6 * * 5 (commented Fri 1:30am CT).
        "trigger": CronTrigger(
            day_of_week="thu",
            hour=23,
            minute=30,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "create_new_cycle_page",
        "name": "Create Weekly Cycle Page",
        "func": _create_new_cycle_page,
        # crontab_generation.py: 30 8 * * 2 (commented Tue 3:30am CT).
        "trigger": CronTrigger(
            day_of_week="tue",
            hour=1,
            minute=30,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "create_weekly_health_review_page",
        "name": "Create Weekly Health Review Page",
        "func": _create_weekly_health_review_page,
        # crontab_generation.py: 0 9 * * 2 (commented Tue 4:00am CT).
        "trigger": CronTrigger(
            day_of_week="tue",
            hour=2,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "create_weekly_map",
        "name": "Create Weekly Map Page",
        "func": _create_weekly_map,
        # crontab_generation.py: 0 6 * * 4 (commented Thu 1:00am CT).
        "trigger": CronTrigger(
            day_of_week="wed",
            hour=23,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "create_cycle_and_cooling_period_pages",
        "name": "Create 6-Week Cycle and Cooling Period Pages",
        "func": _create_cycle_and_cooling_period_pages,
        # Never in crontab_generation.py; live host runs 0 11 * * 6 UTC
        # (Sat 11:00 UTC = Sat 04:00 PDT / Sat 03:00 PST, "Fri evening-ish").
        # Sat 04:00 local is the PDT equivalent, DST-stable.
        "trigger": CronTrigger(
            day_of_week="sat",
            hour=4,
            minute=0,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "spotify_sync_shazam_to_library",
        "name": "Spotify: Shazam playlist → Liked Songs",
        "func": _spotify_sync_shazam_to_library,
        # Live host: */15. Fills Liked Songs before the +5m drain job.
        "trigger": CronTrigger(
            minute="*/15",
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "spotify_sync_saved_today_to_half_year",
        "name": "Spotify: Liked Songs today → half-year playlist",
        "func": _spotify_sync_saved_today_to_half_year,
        # Live host: 5-59/15 (+5m offset so Shazam saves land first).
        "trigger": CronTrigger(
            minute="5,20,35,50",
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "spotify_create_half_year_playlist",
        "name": "Spotify: create half-year playlist",
        "func": _spotify_create_half_year_playlist,
        # Live host: 0 0 1 1,7 *. Quiet local hour after midnight.
        "trigger": CronTrigger(
            month="1,7",
            day=1,
            hour=0,
            minute=5,
            timezone=SYSTEM_TZ,
        ),
    },
    {
        "id": "spotify_music_of_the_day",
        "name": "Spotify: Music of the Day → daily journal",
        "func": _spotify_music_of_the_day,
        # Previous-calendar-day Liked Songs → yesterday's journal.
        # Not the 15-minute Shazam/drain pair.
        "trigger": CronTrigger(
            hour=3,
            minute=0,
            timezone=SYSTEM_TZ,
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


def run_job_now(job_id, **kwargs):
    """Trigger a scheduled job to run immediately. Returns True if found.

    Optional kwargs are stored on the job (used by parameterized one-shots
    such as ``backfill_readwise_highlights``,
    ``backfill_knowledge_hub_buffet``, ``sync_granola_notes``,
    ``backfill_granola_notes``, ``spotify_music_of_the_day`` (``date``),
    and the daily creation jobs' ``use_today`` recovery flag).
    """
    job = scheduler.get_job(job_id)
    if job is None:
        return False
    updates = {"next_run_time": datetime.now(pytz.utc)}
    if kwargs:
        updates["kwargs"] = kwargs
    scheduler.modify_job(job_id, **updates)
    return True
