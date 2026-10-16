from types import SimpleNamespace

import pytest

from tradingagents.runner.progress import ProgressTracker, build_sections, message_kind


class HumanMessage:
    def __init__(self, content, id=None):
        self.content, self.id = content, id


class AIMessage:
    def __init__(self, content, id=None, tool_calls=None):
        self.content, self.id, self.tool_calls = content, id, tool_calls or []


class ToolMessage:
    def __init__(self, content, id=None):
        self.content, self.id = content, id


def statuses(events):
    return {e[1]["agent"]: e[1]["status"] for e in events if e[0] == "agent.status"}


@pytest.mark.unit
def test_tracker_plans_only_selected_analysts_and_starts_them_together():
    tracker = ProgressTracker(["news", "market"])
    events = tracker.start()

    plan = events[0][1]["teams"]
    assert plan[0] == {"team": "Analyst Team", "agents": ["Market Analyst", "News Analyst"]}
    assert statuses(events) == {"Market Analyst": "in_progress", "News Analyst": "in_progress"}


@pytest.mark.unit
def test_tracker_follows_the_cli_status_flow_and_emits_each_section_once():
    tracker = ProgressTracker(["market"])
    tracker.start()

    events = tracker.update({"market_report": "# Market"})
    assert statuses(events) == {"Market Analyst": "completed", "Bull Researcher": "in_progress"}
    assert ("report.section", {"section": "market_report", "title": "Market", "content": "# Market"}) in events
    assert tracker.update({"market_report": "# Market"}) == []

    events = tracker.update(
        {
            "investment_debate_state": {"bull_history": "bull", "bear_history": "bear"},
            "investment_plan": "buy it",
        }
    )
    assert statuses(events)["Research Manager"] == "completed"
    assert statuses(events)["Trader"] == "in_progress"
    research = next(e[1] for e in events if e[0] == "report.section")
    assert research["section"] == "research"
    assert "### Bull Researcher\nbull" in research["content"]

    events = tracker.update({"trader_investment_plan": "**Action**: Buy"})
    assert statuses(events) == {"Trader": "completed", "Aggressive Analyst": "in_progress"}

    events = tracker.update(
        {
            "risk_debate_state": {
                "aggressive_history": "a",
                "conservative_history": "c",
                "neutral_history": "n",
            },
            "final_trade_decision": "Rating: Buy",
        }
    )
    assert statuses(events)["Portfolio Manager"] == "completed"
    assert {e[1]["section"] for e in events if e[0] == "report.section"} == {"risk", "portfolio"}


@pytest.mark.unit
def test_tracker_marks_running_agents_as_error_on_failure():
    tracker = ProgressTracker(["market", "news"])
    tracker.start()
    tracker.update({"market_report": "done"})

    assert statuses(tracker.fail()) == {"News Analyst": "error"}


@pytest.mark.unit
def test_tracker_dedupes_messages_and_records_tool_calls():
    tracker = ProgressTracker(["market"])
    call = {"name": "get_stock_data", "args": {"symbol": "AAPL"}}
    ai = AIMessage("Fetching prices", id="1", tool_calls=[call])
    data = ToolMessage("x" * 2000, id="2")

    first = tracker.messages([ai, data])
    assert tracker.messages([ai, data]) == []

    kinds = [e[1]["kind"] for e in first if e[0] == "agent.message"]
    assert kinds == ["Agent", "Data"]
    assert [e for e in first if e[0] == "tool.call"][0][1]["name"] == "get_stock_data"
    data_event = [e for e in first if e[1].get("kind") == "Data"][0]
    assert len(data_event[1]["message"]) <= 500


@pytest.mark.unit
def test_message_kind_matches_cli_classification():
    assert message_kind(HumanMessage("Continue")) == "Control"
    assert message_kind(HumanMessage("question")) == "User"
    assert message_kind(ToolMessage("data")) == "Data"
    assert message_kind(AIMessage("hello")) == "Agent"
    assert message_kind(SimpleNamespace(content="x")) == "System"


@pytest.mark.unit
def test_build_sections_orders_tabs_for_the_final_result():
    sections = build_sections(
        {
            "market_report": "m",
            "sentiment_report": "s",
            "investment_debate_state": {"bull_history": "b", "bear_history": ""},
            "investment_plan": "plan",
            "trader_investment_plan": "t",
            "risk_debate_state": {"aggressive_history": "a"},
            "final_trade_decision": "d",
        }
    )
    assert [s["key"] for s in sections] == [
        "market_report",
        "sentiment_report",
        "research",
        "trader",
        "risk",
        "portfolio",
    ]
