"""Scheduler setup and API endpoint tests."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")
# Pin PT so Sunday 6:00am is asserted in America/Los_Angeles like other
# timezone-sensitive scheduler tests.
os.environ["SYSTEM_TIMEZONE"] = "America/Los_Angeles"

from fastapi.testclient import TestClient

from config import SYSTEM_TIMEZONE_STR
from main import app
from scheduler import (
    DAILY_CREATION_JOB_IDS,
    ENSURE_TODAYS_DAILY_FILES_JOB_ID,
    OBSIDIAN_CRON_MIGRATION_JOB_IDS,
    RECONCILE_MISSED_OBSIDIAN_WRITES_JOB_ID,
    SCHEDULED_JOBS,
    SPOTIFY_SCHEDULED_JOB_IDS,
    run_job_now,
    scheduler,
)


@pytest.fixture(scope="module")
def client():
    """TestClient as context manager to trigger lifespan (starts scheduler)."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Job registry tests (no lifespan needed)
# ---------------------------------------------------------------------------

def test_scheduled_jobs_not_empty():
    """At least one job is registered in the registry."""
    assert len(SCHEDULED_JOBS) > 0


def test_cycle_summary_job_in_registry():
    """The cycle summary email job is defined in SCHEDULED_JOBS."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    assert "send_cycle_summary_email" in job_ids


def test_linear_digest_job_in_registry():
    """The Linear digest email job is defined in SCHEDULED_JOBS."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    assert "send_linear_digest_email" in job_ids


def test_essay_ideas_job_in_registry():
    """The essay ideas from journal email job is defined in SCHEDULED_JOBS."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    assert "send_essay_ideas_from_journal" in job_ids


def test_readwise_backfill_job_in_registry():
    """The Readwise highlight backfill job is defined in SCHEDULED_JOBS."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    assert "backfill_readwise_highlights" in job_ids


def test_sunday_wrap_up_job_in_registry():
    """The Sunday wrap-up email job is defined in SCHEDULED_JOBS."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    assert "send_sunday_wrap_up_email" in job_ids


def test_knowledge_hub_buffet_backfill_job_in_registry():
    """The Knowledge Hub buffet backfill job is defined in SCHEDULED_JOBS."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    assert "backfill_knowledge_hub_buffet" in job_ids


def test_granola_sync_job_in_registry():
    """The Granola journal sync job is defined in SCHEDULED_JOBS."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    assert "sync_granola_notes" in job_ids


def test_granola_backfill_job_in_registry():
    """The Granola notes backfill job is defined in SCHEDULED_JOBS."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    assert "backfill_granola_notes" in job_ids


def test_daily_creation_jobs_in_registry():
    """Daily journal/action/properties jobs are defined in SCHEDULED_JOBS."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    for job_id in DAILY_CREATION_JOB_IDS:
        assert job_id in job_ids


def test_ensure_todays_daily_files_job_in_registry():
    """Morning catch-up is registered and is not an evening use_today job."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    assert ENSURE_TODAYS_DAILY_FILES_JOB_ID in job_ids
    assert ENSURE_TODAYS_DAILY_FILES_JOB_ID == "ensure_todays_daily_files"
    assert ENSURE_TODAYS_DAILY_FILES_JOB_ID not in DAILY_CREATION_JOB_IDS
    assert "create_daily_journal" in DAILY_CREATION_JOB_IDS


def test_daily_creation_modules_are_importable():
    """Hyphen-free package names import as normal Python modules."""
    from scripts.obsidian.workflows.file_creation.create_daily_action import (
        create_daily_action,
    )
    from scripts.obsidian.workflows.file_creation.create_daily_journal import (
        create_daily_journal,
    )
    from scripts.obsidian.workflows.file_updates.update_daily_journal_properties import (
        update_daily_journal_properties,
    )

    assert callable(create_daily_journal)
    assert callable(create_daily_action)
    assert callable(update_daily_journal_properties)


def test_obsidian_cron_migration_jobs_in_registry():
    """Remaining gd-second-brain-os crontab jobs are defined in SCHEDULED_JOBS."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    for job_id in OBSIDIAN_CRON_MIGRATION_JOB_IDS:
        assert job_id in job_ids
    assert "daily_prep" not in job_ids
    assert "daily_prep" not in OBSIDIAN_CRON_MIGRATION_JOB_IDS
    assert "daily_reflection" not in job_ids
    assert "daily_reflection" not in OBSIDIAN_CRON_MIGRATION_JOB_IDS


def test_obsidian_cron_migration_modules_are_importable():
    """Ported workflow callables import as normal Python modules."""
    from scripts.obsidian.workflows.daily_prep import daily_prep
    from scripts.obsidian.workflows.daily_reflection import daily_reflection
    from scripts.obsidian.workflows.file_creation.create_cycle_and_cooling_period_pages import (
        create_cycle_and_cooling_period_pages,
    )
    from scripts.obsidian.workflows.file_creation.create_new_cycle_page import (
        create_new_cycle_page,
    )
    from scripts.obsidian.workflows.file_creation.create_newsletter_page import (
        create_newsletter_page,
    )
    from scripts.obsidian.workflows.file_creation.create_weekly_health_review_page import (
        create_weekly_health_review_page,
    )
    from scripts.obsidian.workflows.file_creation.create_weekly_map import (
        create_weekly_map,
    )
    from scripts.obsidian.workflows.file_creation.create_weeks import create_weeks
    from scripts.obsidian.workflows.file_updates.add_daily_review_section import (
        add_daily_review_section,
    )
    from scripts.obsidian.workflows.file_updates.update_modified_files_today import (
        update_modified_files_today,
    )

    assert callable(daily_prep)
    assert callable(daily_reflection)
    assert callable(add_daily_review_section)
    assert callable(update_modified_files_today)
    assert callable(create_weeks)
    assert callable(create_newsletter_page)
    assert callable(create_new_cycle_page)
    assert callable(create_weekly_health_review_page)
    assert callable(create_weekly_map)
    assert callable(create_cycle_and_cooling_period_pages)


def test_spotify_jobs_in_registry():
    """Spotify crontab ports are defined in SCHEDULED_JOBS."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    for job_id in SPOTIFY_SCHEDULED_JOB_IDS:
        assert job_id in job_ids


def test_no_spotify_token_refresh_job_is_registered():
    """Access tokens refresh on demand — do not port */55 refresh_redis_token."""
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    assert "refresh_redis_token" not in job_ids
    assert not any("refresh" in job_id and "token" in job_id for job_id in job_ids)
    assert not any("refresh" in job_id and "spotify" in job_id for job_id in job_ids)


def test_spotify_sync_modules_are_importable():
    from services.spotify.music_of_the_day import write_music_of_the_day
    from services.spotify.sync import (
        create_half_year_playlist,
        sync_saved_today_to_half_year,
        sync_shazam_to_library,
    )

    assert callable(sync_shazam_to_library)
    assert callable(sync_saved_today_to_half_year)
    assert callable(create_half_year_playlist)
    assert callable(write_music_of_the_day)


def test_job_definitions_have_required_fields():
    """Every job definition has the required fields."""
    required = {"id", "name", "func", "trigger"}
    for job_def in SCHEDULED_JOBS:
        missing = required - set(job_def.keys())
        assert not missing, f"Job {job_def.get('id', '?')} missing fields: {missing}"


def test_job_funcs_are_callable():
    """Every job function is callable."""
    for job_def in SCHEDULED_JOBS:
        assert callable(job_def["func"]), f"Job {job_def['id']} func is not callable"


# ---------------------------------------------------------------------------
# Scheduler lifecycle tests (need lifespan via client fixture)
# ---------------------------------------------------------------------------

def test_scheduler_is_running(client):
    """Scheduler is running after app startup (via lifespan)."""
    assert scheduler.running


def test_all_registry_jobs_are_registered(client):
    """Every job in SCHEDULED_JOBS is registered in the running scheduler."""
    registered_ids = {job.id for job in scheduler.get_jobs()}
    for job_def in SCHEDULED_JOBS:
        assert job_def["id"] in registered_ids, f"Job {job_def['id']} not registered"


def test_jobs_have_next_run_time(client):
    """All registered jobs have a next_run_time set."""
    for job in scheduler.get_jobs():
        assert job.next_run_time is not None, f"Job {job.id} has no next_run_time"


def test_cycle_summary_runs_on_wednesday(client):
    """The cycle summary job is scheduled for Wednesday."""
    job = scheduler.get_job("send_cycle_summary_email")
    assert job is not None
    trigger_str = str(job.trigger)
    assert "wed" in trigger_str


def test_linear_digest_runs_daily_at_7pm_system_timezone(client):
    """The Linear digest job is scheduled daily at 7pm in system timezone."""
    job = scheduler.get_job("send_linear_digest_email")
    assert job is not None
    trigger_str = str(job.trigger).lower()
    assert "hour='19'" in trigger_str
    assert "minute='0'" in trigger_str
    timezone_key = getattr(job.trigger.timezone, "key", str(job.trigger.timezone))
    assert timezone_key == SYSTEM_TIMEZONE_STR


def test_essay_ideas_job_runs_daily_at_430am_system_timezone(client):
    """The essay ideas job is scheduled daily at 4:30am in system timezone."""
    job = scheduler.get_job("send_essay_ideas_from_journal")
    assert job is not None
    trigger_str = str(job.trigger).lower()
    assert "hour='4'" in trigger_str
    assert "minute='30'" in trigger_str
    timezone_key = getattr(job.trigger.timezone, "key", str(job.trigger.timezone))
    assert timezone_key == SYSTEM_TIMEZONE_STR


def test_granola_sync_job_is_manual_2099_safety_net(client):
    """The Granola sync job is year-2099 (webhook is primary; no */15 poll)."""
    job = scheduler.get_job("sync_granola_notes")
    assert job is not None
    trigger_str = str(job.trigger).lower()
    assert "2099" in trigger_str
    assert "*/15" not in trigger_str
    timezone_key = getattr(job.trigger.timezone, "key", str(job.trigger.timezone))
    assert timezone_key == SYSTEM_TIMEZONE_STR


def test_sunday_wrap_up_job_runs_sunday_6am_system_timezone(client):
    """The Sunday wrap-up job is scheduled Sunday 6:00am in system timezone."""
    job = scheduler.get_job("send_sunday_wrap_up_email")
    assert job is not None
    trigger_str = str(job.trigger).lower()
    assert "sun" in trigger_str
    assert "hour='6'" in trigger_str
    assert "minute='0'" in trigger_str
    timezone_key = getattr(job.trigger.timezone, "key", str(job.trigger.timezone))
    assert timezone_key == SYSTEM_TIMEZONE_STR


def test_daily_creation_jobs_run_evening_before_in_system_timezone(client):
    """Daily creation jobs stagger at 18:00/18:05/18:10 in system timezone."""
    expected = {
        "create_daily_journal": ("18", "0"),
        "create_daily_action": ("18", "5"),
        "update_daily_journal_properties": ("18", "10"),
    }
    for job_id, (hour, minute) in expected.items():
        job = scheduler.get_job(job_id)
        assert job is not None, f"Job {job_id} not registered"
        trigger_str = str(job.trigger).lower()
        assert f"hour='{hour}'" in trigger_str, trigger_str
        assert f"minute='{minute}'" in trigger_str, trigger_str
        timezone_key = getattr(job.trigger.timezone, "key", str(job.trigger.timezone))
        assert timezone_key == SYSTEM_TIMEZONE_STR


def test_ensure_todays_daily_files_runs_at_5am_system_timezone(client):
    """Morning catch-up is daily 05:00 Pacific; evening jobs stay at 18:xx."""
    _assert_cron(ENSURE_TODAYS_DAILY_FILES_JOB_ID, hour="5", minute="0")
    # Evening-before cluster is unchanged.
    journal = scheduler.get_job("create_daily_journal")
    assert "hour='18'" in str(journal.trigger).lower()


def _assert_cron(job_id, *, hour=None, minute=None, day_of_week=None):
    job = scheduler.get_job(job_id)
    assert job is not None, f"Job {job_id} not registered"
    trigger_str = str(job.trigger).lower()
    if hour is not None:
        assert f"hour='{hour}'" in trigger_str, trigger_str
    if minute is not None:
        assert f"minute='{minute}'" in trigger_str, trigger_str
    if day_of_week is not None:
        assert day_of_week in trigger_str, trigger_str
    timezone_key = getattr(job.trigger.timezone, "key", str(job.trigger.timezone))
    assert timezone_key == SYSTEM_TIMEZONE_STR


def test_obsidian_cron_migration_jobs_use_system_timezone_hours(client):
    """Migrated crontab jobs use fixed SYSTEM_TZ hours (DST-stable)."""
    _assert_cron("add_daily_review_section", hour="13", minute="0")
    _assert_cron("create_weeks", day_of_week="sun", hour="23", minute="0")
    _assert_cron("create_newsletter_page", day_of_week="thu", hour="23", minute="30")
    _assert_cron("create_new_cycle_page", day_of_week="tue", hour="1", minute="30")
    _assert_cron("create_weekly_health_review_page", day_of_week="tue", hour="2", minute="0")
    _assert_cron("create_weekly_map", day_of_week="wed", hour="23", minute="0")
    _assert_cron(
        "create_cycle_and_cooling_period_pages",
        day_of_week="sat",
        hour="4",
        minute="0",
    )


def test_spotify_jobs_use_system_timezone_offsets(client):
    """Shazam every 15m; drain at +5m; half-year create Jan/Jul 00:05."""
    shazam = scheduler.get_job("spotify_sync_shazam_to_library")
    assert shazam is not None
    shazam_trigger = str(shazam.trigger).lower()
    assert "*/15" in shazam_trigger, shazam_trigger
    timezone_key = getattr(shazam.trigger.timezone, "key", str(shazam.trigger.timezone))
    assert timezone_key == SYSTEM_TIMEZONE_STR

    drain = scheduler.get_job("spotify_sync_saved_today_to_half_year")
    assert drain is not None
    drain_trigger = str(drain.trigger).lower()
    assert "5" in drain_trigger and "20" in drain_trigger
    assert "35" in drain_trigger and "50" in drain_trigger
    drain_tz = getattr(drain.trigger.timezone, "key", str(drain.trigger.timezone))
    assert drain_tz == SYSTEM_TIMEZONE_STR

    create = scheduler.get_job("spotify_create_half_year_playlist")
    assert create is not None
    create_trigger = str(create.trigger).lower()
    assert "month='1,7'" in create_trigger or "month='1,7" in create_trigger
    assert "day='1'" in create_trigger
    assert "hour='0'" in create_trigger
    assert "minute='5'" in create_trigger
    create_tz = getattr(create.trigger.timezone, "key", str(create.trigger.timezone))
    assert create_tz == SYSTEM_TIMEZONE_STR

    music = scheduler.get_job("spotify_music_of_the_day")
    assert music is not None
    music_trigger = str(music.trigger).lower()
    assert "hour='3'" in music_trigger
    assert "minute='0'" in music_trigger
    assert "*/15" not in music_trigger
    music_tz = getattr(music.trigger.timezone, "key", str(music.trigger.timezone))
    assert music_tz == SYSTEM_TIMEZONE_STR


def test_spotify_music_of_the_day_runs_daily_at_3am_system_timezone(client):
    """Music of the Day is once-daily at 03:00 local, not the 15m drain."""
    _assert_cron("spotify_music_of_the_day", hour="3", minute="0")
    job = scheduler.get_job("spotify_music_of_the_day")
    assert "*/15" not in str(job.trigger)


def test_reconcile_missed_obsidian_writes_job_in_registry():
    job_ids = [j["id"] for j in SCHEDULED_JOBS]
    assert RECONCILE_MISSED_OBSIDIAN_WRITES_JOB_ID in job_ids
    assert RECONCILE_MISSED_OBSIDIAN_WRITES_JOB_ID == "reconcile_missed_obsidian_writes"


def test_reconcile_missed_obsidian_writes_module_is_importable():
    from scripts.obsidian.workflows.reconcile_missed_writes import reconcile_missed_writes
    from services.obsidian.reconcile.runner import reconcile_missed_obsidian_writes

    assert callable(reconcile_missed_writes)
    assert callable(reconcile_missed_obsidian_writes)


def test_update_modified_files_today_runs_every_15_minutes(client):
    """Folder-journal relations run */15, not the live-host */10."""
    job = scheduler.get_job("update_modified_files_today")
    assert job is not None
    trigger_str = str(job.trigger).lower()
    assert "*/15" in trigger_str, trigger_str
    assert "*/10" not in trigger_str
    timezone_key = getattr(job.trigger.timezone, "key", str(job.trigger.timezone))
    assert timezone_key == SYSTEM_TIMEZONE_STR


# ---------------------------------------------------------------------------
# API endpoint tests (need lifespan via client fixture)
# ---------------------------------------------------------------------------

def test_list_jobs_endpoint(client):
    """GET /scheduler/jobs returns job list."""
    response = client.get("/scheduler/jobs")
    assert response.status_code == 200
    data = response.json()
    assert "jobs" in data
    assert len(data["jobs"]) == len(SCHEDULED_JOBS)


def test_list_jobs_returns_expected_fields(client):
    """GET /scheduler/jobs returns id, name, next_run_time, trigger for each job."""
    response = client.get("/scheduler/jobs")
    for job in response.json()["jobs"]:
        assert "id" in job
        assert "name" in job
        assert "next_run_time" in job
        assert "trigger" in job


def test_list_jobs_contains_cycle_summary(client):
    """GET /scheduler/jobs includes the cycle summary job."""
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    assert "send_cycle_summary_email" in job_ids


def test_list_jobs_contains_linear_digest(client):
    """GET /scheduler/jobs includes the Linear digest email job."""
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    assert "send_linear_digest_email" in job_ids


def test_list_jobs_contains_essay_ideas_job(client):
    """GET /scheduler/jobs includes the essay ideas from journal email job."""
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    assert "send_essay_ideas_from_journal" in job_ids


def test_list_jobs_contains_readwise_backfill_job(client):
    """GET /scheduler/jobs includes the Readwise highlight backfill job."""
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    assert "backfill_readwise_highlights" in job_ids


def test_list_jobs_contains_sunday_wrap_up_job(client):
    """GET /scheduler/jobs includes the Sunday wrap-up email job."""
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    assert "send_sunday_wrap_up_email" in job_ids


def test_list_jobs_contains_knowledge_hub_buffet_backfill_job(client):
    """GET /scheduler/jobs includes the Knowledge Hub buffet backfill job."""
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    assert "backfill_knowledge_hub_buffet" in job_ids


def test_list_jobs_contains_granola_sync_job(client):
    """GET /scheduler/jobs includes the Granola journal sync job."""
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    assert "sync_granola_notes" in job_ids


def test_list_jobs_contains_granola_backfill_job(client):
    """GET /scheduler/jobs includes the Granola notes backfill job."""
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    assert "backfill_granola_notes" in job_ids


def test_list_jobs_contains_daily_creation_jobs(client):
    """GET /scheduler/jobs includes the daily journal/action/properties jobs."""
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    for job_id in DAILY_CREATION_JOB_IDS:
        assert job_id in job_ids


def test_list_jobs_contains_spotify_jobs(client):
    """GET /scheduler/jobs includes the Spotify library/playlist jobs."""
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    for job_id in SPOTIFY_SCHEDULED_JOB_IDS:
        assert job_id in job_ids
    assert "refresh_redis_token" not in job_ids


def test_trigger_spotify_job(client):
    """POST /scheduler/jobs/{id}/run fires a Spotify job."""
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post("/scheduler/jobs/spotify_sync_shazam_to_library/run")
    assert response.status_code == 200
    assert response.json()["job_id"] == "spotify_sync_shazam_to_library"
    mock_run.assert_called_once_with("spotify_sync_shazam_to_library")


def test_trigger_music_of_the_day_defaults_date_none(client):
    """POST without date writes the previous calendar day."""
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post("/scheduler/jobs/spotify_music_of_the_day/run")
    assert response.status_code == 200
    assert response.json()["job_id"] == "spotify_music_of_the_day"
    assert response.json()["date"] is None
    mock_run.assert_called_once_with("spotify_music_of_the_day", date=None)


def test_trigger_music_of_the_day_forwards_date(client):
    """POST ?date=YYYY-MM-DD writes that journal day."""
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            "/scheduler/jobs/spotify_music_of_the_day/run",
            params={"date": "2026-09-19"},
        )
    assert response.status_code == 200
    assert response.json()["date"] == "2026-09-19"
    mock_run.assert_called_once_with("spotify_music_of_the_day", date="2026-09-19")


def test_trigger_other_job_does_not_forward_music_of_the_day_date(client):
    """date is ignored for jobs that are not Music of the Day."""
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            "/scheduler/jobs/send_arxiv_email/run",
            params={"date": "2026-09-19"},
        )
    assert response.status_code == 200
    mock_run.assert_called_once_with("send_arxiv_email")


def test_list_jobs_contains_obsidian_cron_migration_jobs(client):
    """GET /scheduler/jobs includes the remaining migrated crontab jobs."""
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    for job_id in OBSIDIAN_CRON_MIGRATION_JOB_IDS:
        assert job_id in job_ids
    assert "daily_prep" not in job_ids
    assert "daily_reflection" not in job_ids


def test_run_job_now_triggers_existing_job_without_executing_workflow():
    """run_job_now should reschedule a known job immediately when present."""
    fake_job = object()
    with patch("scheduler.scheduler.get_job", return_value=fake_job), patch(
        "scheduler.scheduler.modify_job"
    ) as mock_modify:
        assert run_job_now("send_essay_ideas_from_journal") is True
    mock_modify.assert_called_once()


def test_trigger_nonexistent_job(client):
    """POST /scheduler/jobs/{id}/run returns 404 for unknown job."""
    response = client.post("/scheduler/jobs/nonexistent_job/run")
    assert response.status_code == 404


def test_trigger_daily_journal_defaults_use_today_false(client):
    """POST /scheduler/jobs/create_daily_journal/run passes use_today=False."""
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post("/scheduler/jobs/create_daily_journal/run")
    assert response.status_code == 200
    assert response.json()["use_today"] is False
    mock_run.assert_called_once_with("create_daily_journal", use_today=False)


def test_trigger_daily_journal_use_today_true(client):
    """POST /scheduler/jobs/create_daily_journal/run?use_today=true recovers today."""
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            "/scheduler/jobs/create_daily_journal/run",
            params={"use_today": True},
        )
    assert response.status_code == 200
    assert response.json()["use_today"] is True
    mock_run.assert_called_once_with("create_daily_journal", use_today=True)


def test_trigger_other_job_does_not_forward_use_today(client):
    """use_today is ignored for jobs that are not daily creation jobs."""
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            "/scheduler/jobs/send_arxiv_email/run",
            params={"use_today": True},
        )
    assert response.status_code == 200
    mock_run.assert_called_once_with("send_arxiv_email")


def test_trigger_ensure_todays_daily_files(client):
    """POST /scheduler/jobs/ensure_todays_daily_files/run fires the catch-up."""
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            f"/scheduler/jobs/{ENSURE_TODAYS_DAILY_FILES_JOB_ID}/run"
        )
    assert response.status_code == 200
    assert response.json()["job_id"] == ENSURE_TODAYS_DAILY_FILES_JOB_ID
    assert "use_today" not in response.json()
    mock_run.assert_called_once_with(ENSURE_TODAYS_DAILY_FILES_JOB_ID)


def test_trigger_ensure_todays_daily_files_ignores_use_today(client):
    """Morning ensure always targets today; the query flag is not forwarded."""
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            f"/scheduler/jobs/{ENSURE_TODAYS_DAILY_FILES_JOB_ID}/run",
            params={"use_today": False},
        )
    assert response.status_code == 200
    mock_run.assert_called_once_with(ENSURE_TODAYS_DAILY_FILES_JOB_ID)


def test_list_jobs_contains_ensure_todays_daily_files(client):
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    assert ENSURE_TODAYS_DAILY_FILES_JOB_ID in job_ids


def test_ensure_todays_daily_files_calls_helpers_in_order_with_use_today():
    """Wrapper calls journal → action → properties with use_today=True.

    Helpers return True when the file already exists; the wrapper treats
    that as success (no Dropbox work is re-done).
    """
    from scheduler import _ensure_todays_daily_files

    calls = []

    def journal(*, use_today):
        calls.append(("journal", use_today))
        return True  # already exists

    def action(*, use_today):
        calls.append(("action", use_today))
        return True  # already exists

    def props(*, use_today):
        calls.append(("properties", use_today))
        return True

    with (
        patch("scheduler._create_daily_journal", side_effect=journal),
        patch("scheduler._create_daily_action", side_effect=action),
        patch("scheduler._update_daily_journal_properties", side_effect=props),
    ):
        assert _ensure_todays_daily_files() is True

    assert calls == [
        ("journal", True),
        ("action", True),
        ("properties", True),
    ]


def test_ensure_todays_daily_files_returns_false_if_any_helper_fails():
    """A helper False (error, not already-exists) fails the catch-up overall."""
    from scheduler import _ensure_todays_daily_files

    with (
        patch("scheduler._create_daily_journal", return_value=True),
        patch("scheduler._create_daily_action", return_value=False),
        patch("scheduler._update_daily_journal_properties", return_value=True) as props,
    ):
        assert _ensure_todays_daily_files() is False
    # Still attempts properties so a missing DA does not skip YAML updates.
    props.assert_called_once_with(use_today=True)


def test_reconcile_missed_obsidian_writes_runs_hourly_in_system_timezone(client):
    """Top of every hour in SYSTEM_TZ (``0 * * * *``)."""
    job = scheduler.get_job(RECONCILE_MISSED_OBSIDIAN_WRITES_JOB_ID)
    assert job is not None
    trigger_str = str(job.trigger).lower()
    assert "minute='0'" in trigger_str, trigger_str
    # APScheduler omits wildcard fields, so every-hour is ``cron[minute='0']``.
    assert "hour=" not in trigger_str, trigger_str
    hour_field = next(
        (field for field in job.trigger.fields if field.name == "hour"),
        None,
    )
    assert hour_field is not None
    assert str(hour_field) == "*"
    timezone_key = getattr(job.trigger.timezone, "key", str(job.trigger.timezone))
    assert timezone_key == SYSTEM_TIMEZONE_STR


def test_list_jobs_contains_reconcile_missed_obsidian_writes(client):
    response = client.get("/scheduler/jobs")
    job_ids = [j["id"] for j in response.json()["jobs"]]
    assert RECONCILE_MISSED_OBSIDIAN_WRITES_JOB_ID in job_ids


def test_trigger_reconcile_job_defaults_since_none(client):
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            f"/scheduler/jobs/{RECONCILE_MISSED_OBSIDIAN_WRITES_JOB_ID}/run"
        )
    assert response.status_code == 200
    assert response.json()["job_id"] == RECONCILE_MISSED_OBSIDIAN_WRITES_JOB_ID
    assert response.json()["since"] is None
    mock_run.assert_called_once_with(RECONCILE_MISSED_OBSIDIAN_WRITES_JOB_ID, since=None)


def test_trigger_reconcile_job_passes_since_override(client):
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post(
            f"/scheduler/jobs/{RECONCILE_MISSED_OBSIDIAN_WRITES_JOB_ID}/run",
            params={"since": "2026-09-19T12:00:00Z"},
        )
    assert response.status_code == 200
    assert response.json()["since"] == "2026-09-19T12:00:00Z"
    mock_run.assert_called_once_with(
        RECONCILE_MISSED_OBSIDIAN_WRITES_JOB_ID,
        since="2026-09-19T12:00:00Z",
    )


def test_trigger_migrated_obsidian_job(client):
    """POST /scheduler/jobs/{id}/run fires a migrated crontab job."""
    with patch("scheduler.run_job_now", return_value=True) as mock_run:
        response = client.post("/scheduler/jobs/add_daily_review_section/run")
    assert response.status_code == 200
    assert response.json()["job_id"] == "add_daily_review_section"
    mock_run.assert_called_once_with("add_daily_review_section")
