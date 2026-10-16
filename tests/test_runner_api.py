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
def test_runner_queues_additional_jobs_in_order(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(
        runner_app, "execute_job", lambda store, job_id, root: order.append(job_id)
    )
    store = JobStore(tmp_path / "jobs.sqlite3")
    client = TestClient(
        runner_app.create_app(store=store, token=TOKEN, results_root=tmp_path / "reports")
    )
    ids = []
    for ticker in ("AAPL", "MSFT", "NVDA"):
        response = client.post(
            "/v1/jobs", headers=AUTH, json={"ticker": ticker, "trade_date": "2026-10-15"}
        )
        assert response.status_code == 202
        ids.append(response.json()["id"])

    assert order == ids


@pytest.mark.unit
def test_runner_lists_active_jobs_oldest_first(client):
    client.post("/v1/jobs", headers=AUTH, json={"ticker": "AAPL", "trade_date": "2026-10-15"})
    client.post("/v1/jobs", headers=AUTH, json={"ticker": "MSFT", "trade_date": "2026-10-15"})
    # execute_job is stubbed, so claimed jobs remain running and stay listed.
    jobs = client.get("/v1/jobs", headers=AUTH).json()["jobs"]
    assert [job["ticker"] for job in jobs] == ["AAPL", "MSFT"]
    assert client.get("/v1/jobs").status_code == 401


@pytest.mark.unit
def test_runner_cancels_only_queued_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_app, "execute_job", lambda *args: None)
    store = JobStore(tmp_path / "jobs.sqlite3")
    client = TestClient(
        runner_app.create_app(store=store, token=TOKEN, results_root=tmp_path / "reports")
    )
    # Created straight in the store so it stays queued (the API would start it).
    queued = store.create_job("MSFT", "2026-10-15", ("news",))

    response = client.post(f"/v1/jobs/{queued['id']}/cancel", headers=AUTH)
    assert response.status_code == 200
    job = client.get(f"/v1/jobs/{queued['id']}", headers=AUTH).json()
    assert (job["status"], job["error"]) == ("failed", "cancelled by operator")
    assert client.post(f"/v1/jobs/{queued['id']}/cancel", headers=AUTH).status_code == 409
    assert client.post("/v1/jobs/missing/cancel", headers=AUTH).status_code == 404
    assert client.post(f"/v1/jobs/{queued['id']}/cancel").status_code == 401


@pytest.mark.unit
def test_runner_rejects_jobs_when_queue_is_full(client, monkeypatch):
    monkeypatch.setattr(runner_app, "MAX_ACTIVE_JOBS", 1)
    payload = {"ticker": "AAPL", "trade_date": "2026-10-15"}
    assert client.post("/v1/jobs", headers=AUTH, json=payload).status_code == 202
    assert client.post("/v1/jobs", headers=AUTH, json=payload).status_code == 429


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"ticker": "../AAPL", "trade_date": "2026-10-15"},
        {"ticker": "AAPL", "trade_date": "2999-01-01"},
        {"ticker": "AAPL", "trade_date": "not-a-date"},
        {"ticker": "AAPL", "trade_date": "2026-10-15", "analysts": []},
    ],
)
def test_runner_rejects_invalid_inputs(client, payload):
    assert client.post("/v1/jobs", headers=AUTH, json=payload).status_code == 422


@pytest.mark.unit
def test_store_fails_interrupted_running_job_but_keeps_queued_jobs(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = JobStore(path)
    running = store.create_job("AAPL", "2026-10-15", ("news",))
    queued = store.create_job("MSFT", "2026-10-15", ("news",))
    assert store.claim_next_queued()["id"] == running["id"]

    reopened = JobStore(path)

    assert reopened.get_job(running["id"])["status"] == "failed"
    assert reopened.get_job(queued["id"])["status"] == "queued"
    assert reopened.claim_next_queued()["id"] == queued["id"]
    assert reopened.claim_next_queued() is None
