"""Macroeconomic indicators from the private production gateway.

The ai-node asks thinkpad-prod, which reads keyless public sources (the New York Fed, the
Treasury and the Bureau of Labor Statistics), so the ai-node needs no FRED key and no route to
those hosts. The report has the same shape as the FRED one.
"""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote

import requests

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.prod_gateway import gateway_settings

REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_LOOKBACK_DAYS = 365
MAX_LOOKBACK_DAYS = 730
# Recent values matter most for a decision; a daily series would otherwise flood the context.
MAX_ROWS = 40


def get_macro_data(
    indicator: str,
    as_of_date: str,
    look_back_days: int | None = None,
) -> str:
    """A macroeconomic series as a markdown report: latest value, change, recent observations."""
    try:
        as_of = date.fromisoformat(as_of_date)
    except ValueError as exc:
        raise VendorUnavailableError("the macro request has an invalid date") from exc
    days = min(MAX_LOOKBACK_DAYS, max(1, look_back_days or DEFAULT_LOOKBACK_DAYS))
    name = indicator.strip().lower()
    base_url, token = gateway_settings()
    try:
        response = requests.get(
            f"{base_url}/macro/{quote(name, safe='_')}",
            headers={"Authorization": f"Bearer {token}"},
            params={"as_of": as_of_date, "lookback_days": days},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            # Guidance rather than an error, so a bad indicator does not abort the run.
            return f"Macro data: {response.json().get('detail', 'unknown indicator')}"
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise VendorUnavailableError(f"macro request through production failed: {exc}") from exc
    return _report(payload, as_of, days)


def _report(payload: object, as_of: date, days: int) -> str:
    try:
        assert isinstance(payload, dict)
        points = [(str(o["date"]), float(o["value"])) for o in payload["observations"]]
        if any(date.fromisoformat(d) > as_of for d, _ in points):
            raise VendorUnavailableError("production returned a future-dated macro value")
        key, title, units = payload["indicator"], payload["title"], payload["units"]
        frequency, source = payload["frequency"], payload["source"]
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        raise VendorUnavailableError("production returned an invalid macro response") from exc

    start = (as_of - timedelta(days=days)).isoformat()
    header = (
        f"## {source}: {title} ({key})\n"
        f"- Units: {units}\n"
        f"- Frequency: {frequency}\n"
        f"- Window: {start} to {as_of.isoformat()}\n"
    )
    if not points:
        return header + (
            f"\nNo observations for {key} in this window. The series may report less often "
            "than the window (try a longer look_back_days), or have nothing public by then."
        )
    first_date, first = points[0]
    last_date, last = points[-1]
    change = last - first
    percent = f" ({change / first * 100:+.2f}%)" if first else ""
    summary = (
        f"\n**Latest:** {last:g} ({last_date}) | "
        f"**Change from {first_date} to {last_date}:** {change:+.2f}{percent}, from {first:g}\n"
    )
    shown, note = points, ""
    if len(points) > MAX_ROWS:
        shown = points[-MAX_ROWS:]
        note = f"\n_(showing the most recent {MAX_ROWS} of {len(points)} observations)_\n"
    table = "\n| Date | Value |\n| --- | --- |\n" + "\n".join(f"| {d} | {v:g} |" for d, v in shown)
    return header + summary + note + table + "\n"
