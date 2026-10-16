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


@pytest.mark.unit
def test_live_jobs_run_before_older_backtest_jobs(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    backtest = store.create_job("AAPL", "2026-10-14", ("news",), mode="backtest")
    live = store.create_job("MSFT", "2026-10-15", ("news",))
    later_backtest = store.create_job("NVDA", "2026-10-14", ("news",), mode="backtest")

    order = [store.claim_next_queued()["id"] for _ in range(3)]

    assert order == [live["id"], backtest["id"], later_backtest["id"]]
    assert store.get_job(live["id"])["mode"] == "live"


@pytest.mark.unit
def test_store_adds_mode_to_a_database_created_before_backtests(tmp_path):
    import sqlite3

    path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, ticker TEXT NOT NULL, trade_date TEXT NOT NULL,"
            " analysts_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL, result_json TEXT, error TEXT)"
        )
        connection.execute(
            "CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,"
            " created_at TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('old', 'AAPL', '2026-10-01', '[]', 'completed', 'x', 'x', NULL, NULL)"
        )

    store = JobStore(path)

    assert store.get_job("old")["mode"] == "live"
    assert store.create_job("MSFT", "2026-10-02", ("news",), mode="backtest")["mode"] == "backtest"


@pytest.mark.unit
def test_runner_accepts_only_known_modes(client):
    payload = {"ticker": "AAPL", "trade_date": "2026-10-15"}
    assert client.post("/v1/jobs", headers=AUTH, json={**payload, "mode": "backtest"}).status_code == 202
    assert client.post("/v1/jobs", headers=AUTH, json={**payload, "mode": "other"}).status_code == 422


BATCH = {
    "batch_id": "11111111-1111-1111-1111-111111111111",
    "tickers": ["aapl", "msft"],
    "trade_dates": ["2026-10-14", "2026-10-13", "2026-10-14"],
}


@pytest.mark.unit
def test_batch_queues_every_pair_oldest_date_first_and_is_idempotent(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(
        runner_app,
        "execute_job",
        lambda store, job_id, root: order.append(
            (store.get_job(job_id)["trade_date"], store.get_job(job_id)["ticker"])
        ),
    )
    store = JobStore(tmp_path / "jobs.sqlite3")
    client = TestClient(
        runner_app.create_app(store=store, token=TOKEN, results_root=tmp_path / "reports")
    )

    first = client.post("/v1/batches", headers=AUTH, json=BATCH)
    again = client.post("/v1/batches", headers=AUTH, json=BATCH)

    assert first.status_code == 202 and first.json()["total"] == 4
    assert again.json()["total"] == 4
    assert order == [
        ("2026-10-13", "AAPL"),
        ("2026-10-13", "MSFT"),
        ("2026-10-14", "AAPL"),
        ("2026-10-14", "MSFT"),
    ]


@pytest.mark.unit
def test_batch_jobs_stay_out_of_the_live_listing_and_queue_cap(client):
    client.post("/v1/batches", headers=AUTH, json=BATCH)
    assert client.get("/v1/jobs", headers=AUTH).json() == {"jobs": []}


@pytest.mark.unit
def test_batch_results_are_published_with_a_cursor(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create_batch("b1", ["AAPL", "MSFT"], ["2026-10-14"], ("news",))
    first = store.claim_next_queued()
    second = store.claim_next_queued()
    store.set_status(
        first["id"],
        "completed",
        result={"rating": "Buy", "reports": {"final_trade_decision": "Rating: Buy " + "x" * 900}},
    )
    store.record_batch_result(first["id"])
    store.set_status(second["id"], "failed", error="boom")
    store.record_batch_result(second["id"])
    store.record_batch_result(second["id"])  # publishing twice must not duplicate

    results = store.batch_results_after("b1", 0)

    assert [(r["ticker"], r["status"], r["rating"]) for r in results] == [
        ("AAPL", "completed", "Buy"),
        ("MSFT", "failed", None),
    ]
    assert len(results[0]["summary"]) == 600 and results[1]["error"] == "boom"
    assert store.batch_results_after("b1", results[0]["seq"]) == [results[1]]
    assert store.batch_status("b1")["completed"] == 1 and store.batch_status("b1")["failed"] == 1


@pytest.mark.unit
def test_restart_requeues_an_interrupted_batch_job_but_gives_up_after_retries(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = JobStore(path)
    store.create_batch("b1", ["AAPL"], ["2026-10-14"], ("news",))
    job = store.claim_next_queued()

    for _ in range(2):  # two restarts mid-run: retried each time
        store = JobStore(path)
        assert store.get_job(job["id"])["status"] == "queued"
        store.claim_next_queued()

    store = JobStore(path)  # the third interruption fails the job and publishes the failure
    assert store.get_job(job["id"])["status"] == "failed"
    assert store.batch_results_after("b1", 0)[0]["status"] == "failed"


@pytest.mark.unit
def test_batch_cancel_only_drops_jobs_that_have_not_started(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create_batch("b1", ["AAPL", "MSFT", "NVDA"], ["2026-10-14"], ("news",))
    started = store.claim_next_queued()

    assert store.cancel_batch("b1") == 2
    assert store.get_job(started["id"])["status"] == "running"
    assert store.batch_status("b1")["failed"] == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    "patch",
    [
        {"trade_dates": ["2999-01-01"]},
        {"tickers": ["../AAPL"]},
        {"tickers": []},
        {"batch_id": "not-a-uuid"},
        {"analysts": []},
    ],
)
def test_batch_rejects_invalid_requests(client, patch):
    assert client.post("/v1/batches", headers=AUTH, json={**BATCH, **patch}).status_code == 422


@pytest.mark.unit
def test_batch_endpoints_require_authentication_and_report_unknown_batches(client):
    assert client.post("/v1/batches", json=BATCH).status_code == 401
    assert client.get("/v1/batches/nope/results", headers=AUTH).status_code == 404
    assert client.post("/v1/batches/nope/cancel", headers=AUTH).status_code == 404
