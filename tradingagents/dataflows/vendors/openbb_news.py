"""Ticker news from the private production OpenBB gateway."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from urllib.parse import quote

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.prod_gateway import gateway_settings, window_days

REQUEST_TIMEOUT_SECONDS = 30
MAX_REQUEST_LIMIT = 50


_settings = gateway_settings


def _window_days(start_date: str, end_date: str) -> int:
    return window_days(start_date, end_date)


def _fetch_payload(
    ticker: str, start_date: str, end_date: str, limit: int
) -> tuple[dict, list, date, date]:
    """Ask the gateway for ``limit`` articles and validate the envelope."""
    base_url, token = _settings()
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
    return news, items, window_start, window_end


def _published_on(item: object, window_end: date) -> date:
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
        raise VendorUnavailableError("OpenBB research returned an invalid publication date") from exc
    if published_on > window_end:
        raise VendorUnavailableError("OpenBB research returned future-dated news")
    return published_on


def fetch_news_articles(ticker: str, start_date: str, end_date: str) -> list[dict]:
    """Normalized point-in-time OpenBB articles, for merging with other feeds.

    The gateway returns the newest ``limit`` articles and then cuts them to the window, so a
    small limit finds nothing for an older date. This asks for the most it allows.
    """
    _, items, window_start, window_end = _fetch_payload(
        ticker, start_date, end_date, MAX_REQUEST_LIMIT
    )
    articles: list[dict] = []
    for item in items:
        published_on = _published_on(item, window_end)
        if published_on < window_start:
            continue
        articles.append(
            {
                "title": str(item["title"]),
                "summary": str(item.get("excerpt") or ""),
                "publisher": str(item.get("publisher") or "Unknown"),
                "link": str(item["url"]),
                # The feed dates articles by day only: they sort after that day's timed ones.
                "pub_date": datetime.combine(published_on, time.min, tzinfo=UTC),
                "content": "",
                "vendor": "OpenBB",
            }
        )
    return articles


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Retrieve point-in-time-bounded company news through thinkpad-prod."""
    limit = min(MAX_REQUEST_LIMIT, max(1, int(get_config()["news_article_limit"])))
    news, items, window_start, window_end = _fetch_payload(ticker, start_date, end_date, limit)
    provider = news["provider"]
    output = [
        f"## {ticker} News, from {start_date} to {end_date}:",
        f"Provider: {provider}",
    ]
    item_count = 0
    for item in items[:limit]:
        published_on = _published_on(item, window_end)
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
