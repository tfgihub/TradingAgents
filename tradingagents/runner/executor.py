"""Execute one TradingAgents graph while recording structured progress."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from tradingagents.agents.rating import run_rating
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
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


def execute_job(store: JobStore, job_id: str, results_root: Path) -> None:
    job = store.get_job(job_id)
    if job is None:
        return
    store.set_status(job_id, "running")
    store.add_event(job_id, "job.started", {})
    try:
        config = deepcopy(DEFAULT_CONFIG)
        config["checkpoint_enabled"] = False
        graph = TradingAgentsGraph(job["analysts"], config=config, debug=False)
        initial_state = graph.create_run_state(job["ticker"], job["trade_date"])
        args = graph.propagator.get_graph_args()
        final_state: dict = {}
        completed_sections: set[str] = set()

        with graph.checkpoint_scope(job["ticker"], job["trade_date"]):
            for messages, state in graph.stream_run(initial_state, **args):
                for message in messages:
                    content = str(getattr(message, "content", "") or "").strip()
                    if content:
                        store.add_event(
                            job_id,
                            "agent.message",
                            {
                                "agent": type(message).__name__,
                                "message": content[:4000],
                            },
                        )
                if state:
                    final_state.update(state)
                    for section in _REPORT_FIELDS:
                        if state.get(section) and section not in completed_sections:
                            completed_sections.add(section)
                            store.add_event(
                                job_id,
                                "report.completed",
                                {"section": section},
                            )

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
        }
        store.set_status(job_id, "completed", result=result)
        store.add_event(
            job_id,
            "job.completed",
            {"rating": result["rating"]},
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        store.set_status(job_id, "failed", error=message)
        store.add_event(job_id, "job.failed", {"error": message})
