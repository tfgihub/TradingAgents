"""The extra lane: a second worker whose model calls go to another Ollama."""

import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import tradingagents.runner.app as runner_app
import tradingagents.runner.executor as executor
from tradingagents.runner.executor import Lane, execute_job, link_failure
from tradingagents.runner.store import JobStore

TOKEN = "x" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}
MAC = Lane("mac", "http://100.92.169.45:11434/v1")


def queue(store, count, batch="b1"):
    store.create_batch(batch, [f"T{n:02d}" for n in range(count)], ["2026-10-15"], ("market",))


def states(store):
    with store._connect() as connection:
        rows = connection.execute("SELECT id, status, attempts FROM jobs ORDER BY rowid").fetchall()
    return [(r["status"], r["attempts"]) for r in rows]


# ------------------------------------------------------------------------------ the queue


@pytest.mark.unit
def test_a_lane_leaves_the_last_queued_jobs_to_the_main_lane(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    queue(store, 6)

    taken = []
    while (job := store.claim_next_queued(fresh_only=True, keep=4)) is not None:
        taken.append(job["ticker"])

    assert taken == ["T00", "T01"]  # the lane stops once only 4 are left
    assert store.count_queued() == 4
    assert store.claim_next_queued()["ticker"] == "T02"  # the main lane takes the rest


@pytest.mark.unit
def test_a_lane_only_takes_jobs_nobody_has_tried_and_the_main_lane_takes_given_back_ones(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    queue(store, 3)
    first = store.claim_next_queued()
    assert store.give_back(first["id"]) is True

    assert store.claim_next_queued(fresh_only=True)["ticker"] == "T01"  # not the one given back
    assert store.claim_next_queued()["id"] == first["id"]  # the main lane has it again
    assert states(store)[0] == ("running", 1)


@pytest.mark.unit
def test_only_a_running_job_can_be_given_back(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    queue(store, 1)
    only = store.claim_next_queued()
    store.set_status(only["id"], "completed", result={})

    assert store.give_back(only["id"]) is False
    assert store.give_back("no-such-job") is False
    assert states(store) == [("completed", 0)]


# --------------------------------------------------------------------- what fails and how


class _Link(Exception):
    pass


@pytest.mark.unit
def test_a_dropped_connection_is_a_link_failure_whatever_wraps_it():
    class APIConnectionError(Exception):
        pass

    class Wrapped(Exception):
        pass

    try:
        try:
            raise httpx.ConnectError("refused")
        except httpx.ConnectError as inner:
            raise Wrapped("graph failed") from inner
    except Wrapped as caught:
        assert link_failure(caught) is True
    assert link_failure(APIConnectionError("x")) is True
    assert link_failure(httpx.ReadTimeout("slow")) is True
    assert link_failure(ValueError("the model answered nonsense")) is False
    assert link_failure(KeyError("rating")) is False


def _run_with(monkeypatch, tmp_path, error, lane):
    store = JobStore(tmp_path / "jobs.sqlite3")
    queue(store, 1)
    job = store.claim_next_queued()
    seen = {}

    class Boom:
        def __init__(self, analysts, config=None, debug=False):
            seen["backend_url"] = config.get("backend_url")
            raise error

    monkeypatch.setattr(executor, "TradingAgentsGraph", Boom)
    execute_job(store, job["id"], tmp_path / "reports", lane)
    return store, job["id"], seen


@pytest.mark.unit
def test_a_lane_job_gives_its_model_calls_to_the_lane_and_hands_back_when_the_link_drops(
    tmp_path, monkeypatch
):
    store, job_id, seen = _run_with(monkeypatch, tmp_path, httpx.ConnectError("refused"), MAC)

    assert seen["backend_url"] == MAC.url
    assert states(store) == [("queued", 1)]  # back in the queue, for the main lane
    kinds = [
        e["type"] if "type" in e else e.get("event_type") for e in store.events_after(job_id, 0)
    ]
    assert "job.requeued" in kinds and "job.failed" not in kinds


@pytest.mark.unit
def test_a_real_failure_on_a_lane_is_a_failure_not_a_retry(tmp_path, monkeypatch):
    store, _, _ = _run_with(monkeypatch, tmp_path, ValueError("bad analysis"), MAC)

    assert states(store) == [("failed", 0)]


@pytest.mark.unit
def test_the_main_lane_never_hands_a_job_back_and_keeps_the_configured_model_server(
    tmp_path, monkeypatch
):
    store, _, seen = _run_with(monkeypatch, tmp_path, httpx.ConnectError("refused"), None)

    assert states(store) == [("failed", 0)]  # nowhere else to go
    assert seen["backend_url"] != MAC.url


# ------------------------------------------------------------------------------- the app


@pytest.mark.unit
def test_lanes_are_read_from_one_setting_and_bad_ones_are_refused():
    assert runner_app.parse_lanes("") == []
    assert runner_app.parse_lanes(
        "mac=http://100.92.169.45:11434/v1/, spare=https://x.example/v1"
    ) == [
        Lane("mac", "http://100.92.169.45:11434/v1"),
        Lane("spare", "https://x.example/v1"),
    ]
    for bad in (
        "mac",
        "mac=ftp://x",
        "main=http://x/v1",
        "a b=http://x/v1",
        "m=http://x/v1,m=http://y/v1",
    ):
        with pytest.raises(RuntimeError):
            runner_app.parse_lanes(bad)


@pytest.mark.unit
def test_a_lane_is_up_when_its_ollama_lists_models(monkeypatch):
    seen = []

    def fake_get(url, timeout):
        seen.append((url, timeout))
        return httpx.Response(200)

    monkeypatch.setattr(runner_app.httpx, "get", fake_get)
    assert runner_app.lane_is_up(MAC) is True
    assert seen == [("http://100.92.169.45:11434/api/tags", runner_app.LANE_PROBE_SECONDS)]

    monkeypatch.setattr(runner_app.httpx, "get", lambda url, timeout: httpx.Response(500))
    assert runner_app.lane_is_up(MAC) is False

    def down(url, timeout):
        raise httpx.ConnectTimeout("asleep")

    monkeypatch.setattr(runner_app.httpx, "get", down)
    assert runner_app.lane_is_up(MAC) is False


def _app(tmp_path, monkeypatch, lanes, run):
    monkeypatch.setattr(runner_app, "execute_job", run)
    store = JobStore(tmp_path / "jobs.sqlite3")
    app = runner_app.create_app(
        store=store, token=TOKEN, results_root=tmp_path / "reports", lanes=lanes
    )
    return store, TestClient(app)


def _wait(condition, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.unit
def test_without_extra_lanes_the_runner_is_as_before(tmp_path, monkeypatch):
    calls = []

    def run(store, job_id, root, *rest):
        calls.append((job_id, rest))
        store.set_status(job_id, "completed", result={})

    store, client = _app(tmp_path, monkeypatch, [], run)
    client.post(
        "/v1/batches",
        headers=AUTH,
        json={
            "batch_id": "6f1c2c9e-8f0e-4a27-9b8e-0f6f6f6f6f6f",
            "tickers": ["AAPL", "MSFT"],
            "trade_dates": ["2026-10-15"],
        },
    )

    assert _wait(lambda: len(calls) == 2)
    assert all(rest == () for _, rest in calls)  # the main lane is called with three arguments
    ready = client.get("/health/ready").json()
    assert ready["max_concurrent_jobs"] == 1 and ready["lanes"] == ["main"]


@pytest.mark.unit
def test_a_batch_is_shared_between_the_main_lane_and_an_extra_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_app, "lane_is_up", lambda lane: True)
    monkeypatch.setattr(runner_app, "LANE_KEEP", 2)
    done = []
    gate = threading.Event()

    def run(store, job_id, root, lane=None):
        if lane is not None:
            gate.wait(5)  # the extra lane is slow
        else:
            time.sleep(0.05)  # and the main lane is not instant, so the extra lane gets a start
        done.append((store.get_job(job_id)["ticker"], lane.name if lane else "main"))
        store.set_status(job_id, "completed", result={})

    store, client = _app(tmp_path, monkeypatch, [MAC], run)
    tickers = [f"T{n:02d}" for n in range(8)]
    client.post(
        "/v1/batches",
        headers=AUTH,
        json={
            "batch_id": "6f1c2c9e-8f0e-4a27-9b8e-0f6f6f6f6f6f",
            "tickers": tickers,
            "trade_dates": ["2026-10-15"],
        },
    )

    assert _wait(lambda: sum(1 for _, lane in done if lane == "main") >= 7)
    gate.set()
    assert _wait(lambda: len(done) == 8)
    lanes = dict(done)
    assert sorted(lanes) == tickers  # every job ran once
    assert sum(1 for lane in lanes.values() if lane == "mac") == 1  # the slow lane took one
    ready = client.get("/health/ready").json()
    assert ready["max_concurrent_jobs"] == 2 and ready["lanes"] == ["main", "mac"]


@pytest.mark.unit
def test_an_extra_lane_that_is_down_takes_nothing_and_the_main_lane_finishes_everything(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runner_app, "lane_is_up", lambda lane: False)
    monkeypatch.setattr(runner_app, "LANE_RETRY_SECONDS", 0.05)
    done = []

    def run(store, job_id, root, lane=None):
        done.append(lane.name if lane else "main")
        store.set_status(job_id, "completed", result={})

    store, client = _app(tmp_path, monkeypatch, [MAC], run)
    client.post(
        "/v1/batches",
        headers=AUTH,
        json={
            "batch_id": "6f1c2c9e-8f0e-4a27-9b8e-0f6f6f6f6f6f",
            "tickers": [f"T{n:02d}" for n in range(9)],
            "trade_dates": ["2026-10-15"],
        },
    )

    assert _wait(lambda: len(done) == 9)
    assert set(done) == {"main"}


@pytest.mark.unit
def test_a_job_the_extra_lane_gives_back_is_finished_by_the_main_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_app, "lane_is_up", lambda lane: True)
    monkeypatch.setattr(runner_app, "LANE_KEEP", 1)
    done = []

    def run(store, job_id, root, lane=None):
        if lane is not None:
            store.give_back(job_id)  # the Mac went to sleep in the middle of it
            return
        done.append(store.get_job(job_id)["ticker"])
        store.set_status(job_id, "completed", result={})

    store, client = _app(tmp_path, monkeypatch, [MAC], run)
    tickers = [f"T{n:02d}" for n in range(6)]
    client.post(
        "/v1/batches",
        headers=AUTH,
        json={
            "batch_id": "6f1c2c9e-8f0e-4a27-9b8e-0f6f6f6f6f6f",
            "tickers": tickers,
            "trade_dates": ["2026-10-15"],
        },
    )

    assert _wait(lambda: sorted(done) == tickers)
    assert all(status == "completed" for status, _ in states(store))
