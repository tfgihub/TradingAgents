"""Translate the TradingAgents graph stream into structured, CLI-style progress.

The terminal UI shows a team/agent status table, a feed of messages and tool
calls, and the report of the latest finished stage. This module derives the
same three things as plain events so a browser can render them.
"""

from __future__ import annotations

import hashlib
from typing import Any

ANALYSTS = {
    "market": ("Market Analyst", "market_report", "Market"),
    "social": ("Sentiment Analyst", "sentiment_report", "Sentiment"),
    "news": ("News Analyst", "news_report", "News"),
    "fundamentals": ("Fundamentals Analyst", "fundamentals_report", "Fundamentals"),
}
_ANALYST_ORDER = tuple(ANALYSTS)

RESEARCH_TEAM = ("Bull Researcher", "Bear Researcher", "Research Manager")
RISK_TEAM = ("Aggressive Analyst", "Neutral Analyst", "Conservative Analyst")

_MESSAGE_LIMIT = 1500
_DATA_LIMIT = 500
_TOOL_ARGS_LIMIT = 300


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        return str(content.get("text", "")).strip()
    if isinstance(content, list):
        parts = [_text(item) for item in content]
        return "\n".join(part for part in parts if part)
    return ""


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def message_kind(message: Any) -> str:
    """Classify a LangChain message the way the terminal UI does."""
    name = type(message).__name__
    if name == "HumanMessage":
        return "Control" if _text(getattr(message, "content", "")) == "Continue" else "User"
    if name == "ToolMessage":
        return "Data"
    if name == "AIMessage":
        return "Agent"
    return "System"


def _history(state: dict, key: str, field: str) -> str:
    value = state.get(key)
    return str(value.get(field, "")).strip() if isinstance(value, dict) else ""


def _compose(*parts: tuple[str, str]) -> str:
    return "\n\n".join(f"### {title}\n{body}" for title, body in parts if body)


def build_sections(state: dict) -> list[dict[str, str]]:
    """Ordered report sections for the final result, one per dashboard tab."""
    sections: list[dict[str, str]] = []
    for _, report_key, title in ANALYSTS.values():
        if state.get(report_key):
            sections.append({"key": report_key, "title": title, "content": str(state[report_key])})
    research = _compose(
        ("Bull Researcher", _history(state, "investment_debate_state", "bull_history")),
        ("Bear Researcher", _history(state, "investment_debate_state", "bear_history")),
        ("Research Manager", str(state.get("investment_plan") or "").strip()),
    )
    if research:
        sections.append({"key": "research", "title": "Research Team", "content": research})
    if state.get("trader_investment_plan"):
        sections.append(
            {"key": "trader", "title": "Trader", "content": str(state["trader_investment_plan"])}
        )
    risk = _compose(
        ("Aggressive Analyst", _history(state, "risk_debate_state", "aggressive_history")),
        ("Conservative Analyst", _history(state, "risk_debate_state", "conservative_history")),
        ("Neutral Analyst", _history(state, "risk_debate_state", "neutral_history")),
    )
    if risk:
        sections.append({"key": "risk", "title": "Risk Management", "content": risk})
    if state.get("final_trade_decision"):
        sections.append(
            {
                "key": "portfolio",
                "title": "Portfolio Manager",
                "content": str(state["final_trade_decision"]),
            }
        )
    return sections


Event = tuple[str, dict[str, Any]]


class ProgressTracker:
    """Stateful translator from graph stream steps to progress events."""

    def __init__(self, analysts: tuple[str, ...] | list[str]) -> None:
        self.selected = [key for key in _ANALYST_ORDER if key in analysts]
        analyst_agents = [ANALYSTS[key][0] for key in self.selected]
        self.teams: list[tuple[str, list[str]]] = [
            ("Analyst Team", analyst_agents),
            ("Research Team", list(RESEARCH_TEAM)),
            ("Trading Team", ["Trader"]),
            ("Risk Management", list(RISK_TEAM)),
            ("Portfolio Management", ["Portfolio Manager"]),
        ]
        self.status = {agent: "pending" for _, agents in self.teams for agent in agents}
        self.state: dict[str, Any] = {}
        self._seen_messages: set[str] = set()
        self._emitted_sections: set[str] = set()
        self._events: list[Event] = []

    def start(self) -> list[Event]:
        plan = [{"team": team, "agents": agents} for team, agents in self.teams if agents]
        self._events = [("progress.plan", {"teams": plan})]
        for agent in (ANALYSTS[key][0] for key in self.selected):
            self._set(agent, "in_progress")
        return self._drain()

    def _drain(self) -> list[Event]:
        events, self._events = self._events, []
        return events

    def _set(self, agent: str, status: str) -> None:
        if agent in self.status and self.status[agent] != status:
            self.status[agent] = status
            self._events.append(("agent.status", {"agent": agent, "status": status}))

    def _section(self, key: str, title: str, content: str) -> None:
        if content and key not in self._emitted_sections:
            self._emitted_sections.add(key)
            self._events.append(
                ("report.section", {"section": key, "title": title, "content": content})
            )

    def _message_id(self, message: Any) -> str:
        identifier = getattr(message, "id", None)
        if identifier is not None:
            return str(identifier)
        digest = hashlib.sha256(
            f"{type(message).__name__}:{_text(getattr(message, 'content', ''))}".encode()
        )
        return digest.hexdigest()

    def messages(self, messages: list[Any]) -> list[Event]:
        for message in messages:
            identifier = self._message_id(message)
            if identifier in self._seen_messages:
                continue
            self._seen_messages.add(identifier)
            kind = message_kind(message)
            content = _text(getattr(message, "content", ""))
            if content:
                limit = _DATA_LIMIT if kind == "Data" else _MESSAGE_LIMIT
                self._events.append(
                    (
                        "agent.message",
                        {"agent": type(message).__name__, "kind": kind, "message": _clip(content, limit)},
                    )
                )
            for call in getattr(message, "tool_calls", None) or []:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
                self._events.append(
                    ("tool.call", {"name": str(name), "args": _clip(str(args), _TOOL_ARGS_LIMIT)})
                )
        return self._drain()

    def update(self, chunk: dict[str, Any]) -> list[Event]:
        """Fold one state chunk into the status table and finished sections."""
        self.state.update({key: value for key, value in chunk.items() if value})
        state = self.state

        all_filed = bool(self.selected)
        for key in self.selected:
            agent, report_key, title = ANALYSTS[key]
            if state.get(report_key):
                self._set(agent, "completed")
                self._section(report_key, title, str(state[report_key]))
            else:
                self._set(agent, "in_progress")
                all_filed = False
        if all_filed and self.status["Bull Researcher"] == "pending":
            self._set("Bull Researcher", "in_progress")

        bull = _history(state, "investment_debate_state", "bull_history")
        bear = _history(state, "investment_debate_state", "bear_history")
        if bull or bear:
            for agent in RESEARCH_TEAM:
                if self.status[agent] != "completed":
                    self._set(agent, "in_progress")
        judge = str(state.get("investment_plan") or "").strip()
        if judge:
            for agent in RESEARCH_TEAM:
                self._set(agent, "completed")
            self._section(
                "research",
                "Research Team",
                _compose(
                    ("Bull Researcher", bull),
                    ("Bear Researcher", bear),
                    ("Research Manager", judge),
                ),
            )
            if self.status["Trader"] == "pending":
                self._set("Trader", "in_progress")

        if state.get("trader_investment_plan"):
            self._section("trader", "Trader", str(state["trader_investment_plan"]))
            if self.status["Trader"] != "completed":
                self._set("Trader", "completed")
                self._set("Aggressive Analyst", "in_progress")

        risk = state.get("risk_debate_state")
        if isinstance(risk, dict):
            histories = {
                "Aggressive Analyst": str(risk.get("aggressive_history", "")).strip(),
                "Conservative Analyst": str(risk.get("conservative_history", "")).strip(),
                "Neutral Analyst": str(risk.get("neutral_history", "")).strip(),
            }
            for agent, history in histories.items():
                if history and self.status[agent] != "completed":
                    self._set(agent, "in_progress")
            decision = str(state.get("final_trade_decision") or "").strip()
            if decision and self.status["Portfolio Manager"] != "completed":
                self._set("Portfolio Manager", "in_progress")
                for agent in (*RISK_TEAM, "Portfolio Manager"):
                    self._set(agent, "completed")
                self._section(
                    "risk",
                    "Risk Management",
                    _compose(
                        ("Aggressive Analyst", histories["Aggressive Analyst"]),
                        ("Conservative Analyst", histories["Conservative Analyst"]),
                        ("Neutral Analyst", histories["Neutral Analyst"]),
                    ),
                )
                self._section("portfolio", "Portfolio Manager", decision)
        return self._drain()

    def finish(self) -> list[Event]:
        for agent in self.status:
            self._set(agent, "completed")
        return self._drain()

    def fail(self) -> list[Event]:
        for agent, status in list(self.status.items()):
            if status == "in_progress":
                self._set(agent, "error")
        return self._drain()
