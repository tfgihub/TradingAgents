"""The runner's bounded model call for production's services."""

import threading

import pytest
import requests
from fastapi.testclient import TestClient

import tradingagents.runner.app as runner_app
from tradingagents.runner.complete import MAX_INPUT_CHARS, Completer
from tradingagents.runner.store import JobStore

TOKEN = "x" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}
BODY = {"system": "Rate the news.", "user": "Stock: AAPL", "max_tokens": 50}


class _Reply:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _client(tmp_path, monkeypatch, post, **env):
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", env.get("provider", "ollama"))
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", env.get("model", "qwen3:14b"))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434/v1")
    monkeypatch.setattr(runner_app, "execute_job", lambda *args: None)
    completer = Completer(post=post)
    app = runner_app.create_app(
        store=JobStore(tmp_path / "jobs.sqlite3"),
        token=TOKEN,
        results_root=tmp_path / "reports",
        completer=completer,
    )
    return TestClient(app), completer


@pytest.mark.unit
def test_a_completion_needs_the_runner_token(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, lambda *a, **k: _Reply({}))

    assert client.post("/v1/complete", json=BODY).status_code == 401


@pytest.mark.unit
def test_the_runners_own_model_answers_with_the_bounds_set_here(tmp_path, monkeypatch):
    calls = []

    def post(url, *, json, timeout):
        calls.append((url, json, timeout))
        return _Reply({"message": {"content": '{"ok": true}'}, "eval_count": 7, "done_reason": "stop"})

    client, _ = _client(tmp_path, monkeypatch, post)

    answer = client.post("/v1/complete", headers=AUTH, json=BODY).json()

    assert answer["content"] == '{"ok": true}' and answer["model"] == "qwen3:14b"
    assert answer["output_tokens"] == 7 and answer["done_reason"] == "stop"
    url, sent, timeout = calls[0]
    assert url == "http://ollama:11434/api/chat" and timeout == 600
    assert sent["model"] == "qwen3:14b" and sent["format"] == "json" and sent["think"] is False
    assert sent["options"] == {"temperature": 0, "num_predict": 50}


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra",
    [{"model": "gpt-4"}, {"max_tokens": 4001}, {"system": ""}, {"user": "x" * (MAX_INPUT_CHARS + 1)}],
)
def test_the_caller_cannot_choose_the_model_or_exceed_the_limits(tmp_path, monkeypatch, extra):
    client, _ = _client(tmp_path, monkeypatch, lambda *a, **k: _Reply({}))

    assert client.post("/v1/complete", headers=AUTH, json={**BODY, **extra}).status_code == 422


@pytest.mark.unit
def test_two_long_halves_that_together_exceed_the_limit_are_refused(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, lambda *a, **k: _Reply({}))
    half = "x" * (MAX_INPUT_CHARS // 2 + 1)

    response = client.post("/v1/complete", headers=AUTH, json={**BODY, "system": half, "user": half})

    assert response.status_code == 413


@pytest.mark.unit
def test_a_model_that_is_not_local_is_refused(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, lambda *a, **k: _Reply({}), provider="openai")

    assert client.post("/v1/complete", headers=AUTH, json=BODY).status_code == 501


@pytest.mark.unit
def test_a_failing_model_is_a_502_that_does_not_echo_the_error(tmp_path, monkeypatch):
    def post(*args, **kwargs):
        raise requests.ConnectionError("http://ollama:11434 refused, secret=abc")

    client, completer = _client(tmp_path, monkeypatch, post)

    response = client.post("/v1/complete", headers=AUTH, json=BODY)

    assert response.status_code == 502 and "secret" not in response.text
    assert completer.inflight == 0  # the slot is given back


@pytest.mark.unit
def test_a_call_in_flight_keeps_the_node_awake_and_too_many_are_refused(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def post(*args, **kwargs):
        started.set()
        release.wait(5)
        return _Reply({"message": {"content": "{}"}})

    client, completer = _client(tmp_path, monkeypatch, post)
    first = threading.Thread(target=lambda: client.post("/v1/complete", headers=AUTH, json=BODY))
    second = threading.Thread(target=lambda: client.post("/v1/complete", headers=AUTH, json=BODY))
    first.start()
    started.wait(5)
    second.start()
    while completer.inflight < 2:
        pass

    assert client.get("/v1/activity", headers=AUTH).json() == {
        "busy": True,
        "jobs": 0,
        "completions": 2,
    }
    assert client.post("/v1/complete", headers=AUTH, json=BODY).status_code == 429
    release.set()
    first.join()
    second.join()
    assert client.get("/v1/activity", headers=AUTH).json()["busy"] is False
