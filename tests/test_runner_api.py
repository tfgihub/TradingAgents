import pytest
from fastapi.testclient import TestClient

import tradingagents.runner.app as runner_app
from tradingagents.runner.store import JobStore

TOKEN = "x" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_app, "execute_job", lambda *args: None)
    app = runner_app.create_app(
        store=JobStore(tmp_path / "jobs.sqlite3"),
        token=TOKEN,
        results_root=tmp_path / "reports",
    )
    return TestClient(app)


@pytest.mark.unit
def test_runner_requires_authentication(client):
    response = client.get("/v1/jobs/not-found")
    assert response.status_code == 401


@pytest.mark.unit
def test_runner_creates_job_and_exposes_progress(client):
    response = client.post(
        "/v1/jobs",
        headers=AUTH,
        json={
            "ticker": "aapl",
            "trade_date": "2026-10-15",
            "analysts": ["market", "news"],
        },
    )
    assert response.status_code == 202
    job = response.json()
    assert job["ticker"] == "AAPL"
    events = client.get(f"/v1/jobs/{job['id']}/events", headers=AUTH).json()["events"]
    assert events[0]["type"] == "job.queued"


@pytest.mark.unit
def test_runner_rejects_second_active_job(client):
    payload = {"ticker": "AAPL", "trade_date": "2026-10-15"}
    assert client.post("/v1/jobs", headers=AUTH, json=payload).status_code == 202
    response = client.post("/v1/jobs", headers=AUTH, json=payload)
    assert response.status_code == 409


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"ticker": "../AAPL", "trade_date": "2026-10-15"},
        {"ticker": "AAPL", "trade_date": "2026-10-16"},
        {"ticker": "AAPL", "trade_date": "not-a-date"},
        {"ticker": "AAPL", "trade_date": "2026-10-15", "analysts": []},
    ],
)
def test_runner_rejects_invalid_inputs(client, payload):
    assert client.post("/v1/jobs", headers=AUTH, json=payload).status_code == 422


@pytest.mark.unit
def test_store_marks_interrupted_jobs_failed(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = JobStore(path)
    job = store.create_job("AAPL", "2026-10-15", ("news",))

    reopened = JobStore(path)

    assert reopened.get_job(job["id"])["status"] == "failed"
