"""Ticker news from the private production OpenBB gateway."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)

REQUEST_TIMEOUT_SECONDS = 30


def _settings() -> tuple[str, str]:
    base_url = os.getenv("OPENBB_RESEARCH_URL", "").rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise VendorNotConfiguredError(
            "OPENBB_RESEARCH_URL must be a private HTTPS endpoint"
        )
    token_path = os.getenv("OPENBB_RESEARCH_TOKEN_FILE", "")
    if not token_path:
        raise VendorNotConfiguredError("OPENBB_RESEARCH_TOKEN_FILE is required")
    try:
        token = Path(token_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VendorNotConfiguredError(
            f"OPENBB_RESEARCH_TOKEN_FILE could not be read: {exc}"
        ) from exc
    if len(token) < 32:
        raise VendorNotConfiguredError(
            "OPENBB_RESEARCH_TOKEN_FILE must contain at least 32 characters"
        )
    return base_url, token


def _window_days(start_date: str, end_date: str) -> int:
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise VendorUnavailableError("OpenBB received an invalid analysis date") from exc
    if start > end:
        raise VendorUnavailableError("OpenBB news start date is after its end date")
    return max(1, min(365, (end - start).days + 1))


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Retrieve point-in-time-bounded company news through thinkpad-prod."""
    base_url, token = _settings()
    limit = min(50, max(1, int(get_config()["news_article_limit"])))
    try:
        window_start = date.fromisoformat(start_date)
        window_end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise VendorUnavailableError("OpenBB received an invalid analysis date") from exc
    try:
        response = requests.get(
            f"{base_url}/{quote(ticker, safe='.-')}/news",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "as_of": end_date,
                "lookback_days": _window_days(start_date, end_date),
                "limit": limit,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise VendorUnavailableError(f"OpenBB research request failed: {exc}") from exc

    news = payload.get("news") if isinstance(payload, dict) else None
    items = news.get("items") if isinstance(news, dict) else None
    provider = news.get("provider") if isinstance(news, dict) else None
    if (
        not isinstance(items, list)
        or not isinstance(provider, str)
        or not provider
        or payload.get("as_of") != end_date
        or payload.get("symbol") != ticker.upper()
    ):
        raise VendorUnavailableError("OpenBB research returned an invalid news response")

    output = [
        f"## {ticker} News, from {start_date} to {end_date}:",
        f"Provider: {provider}",
    ]
    item_count = 0
    for item in items[:limit]:
        if (
            not isinstance(item, dict)
            or not item.get("title")
            or not item.get("url")
            or not item.get("published_on")
        ):
            raise VendorUnavailableError("OpenBB research returned a malformed news item")
        try:
            published_on = date.fromisoformat(str(item["published_on"]))
        except ValueError as exc:
            raise VendorUnavailableError(
                "OpenBB research returned an invalid publication date"
            ) from exc
        if published_on > window_end:
            raise VendorUnavailableError("OpenBB research returned future-dated news")
        if published_on < window_start:
            continue
        item_count += 1
        publisher = item.get("publisher") or "Unknown"
        output.append(f"\n### {item['title']} (source: {publisher}; vendor: OpenBB)")
        output.append(f"Published: {published_on.isoformat()}")
        if item.get("topic"):
            output.append(f"Topic: {item['topic']}")
        if item.get("excerpt"):
            output.append(str(item["excerpt"]))
        output.append(f"Link: {item['url']}")
    if item_count == 0:
        raise NoMarketDataError(
            ticker,
            detail=f"OpenBB returned no company news from {start_date} through {end_date}",
        )
    retrieved = news.get("retrieved_at_utc")
    if retrieved:
        output.append(f"\nOpenBB retrieval timestamp: {retrieved}")
    return "\n".join(output)
