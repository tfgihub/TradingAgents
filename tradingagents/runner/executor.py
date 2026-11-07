"""Execute one TradingAgents graph while recording structured progress."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from tradingagents.agents.rating import run_rating
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.runner.progress import ProgressTracker, build_sections
from tradingagents.runner.store import JobStore

_REPORT_FIELDS = (
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "investment_plan",
    "trader_investment_plan",
    "final_trade_decision",
)


@dataclass(frozen=True)
class Lane:
    """An extra place to run a job's model calls: the same graph, another Ollama."""

    name: str
    url: str  # the OpenAI-compatible base URL, e.g. http://100.92.169.45:11434/v1


# Why a call to a lane's model server failed, not why an analysis did: the job is given back.
_LINK_ERRORS = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "RemoteProtocolError",
        "ReadError",
        "ReadTimeout",
    }
)


def link_failure(exc: BaseException) -> bool:
    """Whether ``exc`` (or what caused it) is the model server being unreachable or dropping."""

    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if any(cls.__name__ in _LINK_ERRORS for cls in type(exc).__mro__):
            return True
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
    return False


def _emit(store: JobStore, job_id: str, events: list) -> None:
    for event_type, payload in events:
        store.add_event(job_id, event_type, payload)


def execute_job(store: JobStore, job_id: str, results_root: Path, lane: Lane | None = None) -> None:
    job = store.get_job(job_id)
    if job is None:
        return
    store.set_status(job_id, "running")
    store.add_event(job_id, "job.started", {"lane": lane.name} if lane else {})
    tracker: ProgressTracker | None = None
    try:
        config = deepcopy(DEFAULT_CONFIG)
        config["checkpoint_enabled"] = False
        if lane is not None:
            config["backend_url"] = lane.url
        if job.get("mode") == "backtest":
            # A historical replay must not learn from, or write into, the live decision memory.
            memory_path = results_root / job_id / "memory" / "trading_memory.md"
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            config["memory_log_path"] = str(memory_path)
        graph = TradingAgentsGraph(job["analysts"], config=config, debug=False)
        initial_state = graph.create_run_state(job["ticker"], job["trade_date"])
        args = graph.propagator.get_graph_args()
        final_state: dict = {}
        tracker = ProgressTracker(job["analysts"])
        _emit(store, job_id, tracker.start())

        with graph.checkpoint_scope(job["ticker"], job["trade_date"]):
            for messages, state in graph.stream_run(initial_state, **args):
                _emit(store, job_id, tracker.messages(messages))
                if state:
                    final_state.update(state)
                    _emit(store, job_id, tracker.update(state))
        _emit(store, job_id, tracker.finish())

        graph.record_decision(job["ticker"], job["trade_date"], final_state)
        report_path = graph.save_reports(
            final_state,
            job["ticker"],
            save_path=results_root / job_id,
            html=True,
        )
        result = {
            "ticker": job["ticker"],
            "trade_date": job["trade_date"],
            "rating": run_rating(final_state),
            "lane": lane.name if lane else "main",
            "report_path": str(report_path),
            "reports": {field: final_state.get(field, "") for field in _REPORT_FIELDS},
            "sections": build_sections(final_state),
        }
        store.set_status(job_id, "completed", result=result)
        store.record_batch_result(job_id)
        store.add_event(
            job_id,
            "job.completed",
            {"rating": result["rating"]},
        )
    except Exception as exc:
        if lane is not None and link_failure(exc) and store.give_back(job_id):
            store.add_event(
                job_id,
                "job.requeued",
                {"lane": lane.name, "why": f"{type(exc).__name__}: {exc}"[:300]},
            )
            return
        message = f"{type(exc).__name__}: {exc}"
        if tracker is not None:
            _emit(store, job_id, tracker.fail())
        store.set_status(job_id, "failed", error=message)
        store.record_batch_result(job_id)
        store.add_event(job_id, "job.failed", {"error": message})
