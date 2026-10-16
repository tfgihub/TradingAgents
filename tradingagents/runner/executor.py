"""Execute one TradingAgents graph while recording structured progress."""

from __future__ import annotations

from copy import deepcopy
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


def _emit(store: JobStore, job_id: str, events: list) -> None:
    for event_type, payload in events:
        store.add_event(job_id, event_type, payload)


def execute_job(store: JobStore, job_id: str, results_root: Path) -> None:
    job = store.get_job(job_id)
    if job is None:
        return
    store.set_status(job_id, "running")
    store.add_event(job_id, "job.started", {})
    tracker: ProgressTracker | None = None
    try:
        config = deepcopy(DEFAULT_CONFIG)
        config["checkpoint_enabled"] = False
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
        message = f"{type(exc).__name__}: {exc}"
        if tracker is not None:
            _emit(store, job_id, tracker.fail())
        store.set_status(job_id, "failed", error=message)
        store.record_batch_result(job_id)
        store.add_event(job_id, "job.failed", {"error": message})
