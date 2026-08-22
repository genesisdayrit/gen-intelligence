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
from scheduler import SCHEDULED_JOBS, run_job_now, scheduler


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
