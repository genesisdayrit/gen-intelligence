"""Scheduler setup and API endpoint tests."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TG_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("LINK_SHARE_API_KEY", "test-link-api-key")
os.environ.setdefault("MANUS_API_KEY", "test-manus-key")

from fastapi.testclient import TestClient

from config import SYSTEM_TIMEZONE_STR
from main import app
from scheduler import SCHEDULED_JOBS, scheduler


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


def test_trigger_nonexistent_job(client):
    """POST /scheduler/jobs/{id}/run returns 404 for unknown job."""
    response = client.post("/scheduler/jobs/nonexistent_job/run")
    assert response.status_code == 404
