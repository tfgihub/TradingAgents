"""Alpaca ticker news, read through thinkpad-prod's private research gateway.

Alpaca credentials live only in production. ai-node authenticates to the gateway with the
runner token and never holds an Alpaca key.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from urllib.parse import quote

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.prod_gateway import gateway_settings, window_days

REQUEST_TIMEOUT_SECONDS = 45
_PROVIDER = "alpaca-news"


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def fetch_news_articles(
    ticker: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Return normalized, point-in-time Alpaca news for ``ticker`` from the production gateway."""

    base_url, token = gateway_settings()
    limit = min(50, max(1, int(get_config()["news_article_limit"])))
    try:
        window_start = date.fromisoformat(start_date)
        window_end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise VendorUnavailableError("Alpaca news received an invalid analysis date") from exc
    try:
        response = requests.get(
            f"{base_url}/{quote(ticker, safe='.-')}/alpaca-news",
            headers={"Authorization": "Bearer " + token},
            params={
                "as_of": end_date,
                "lookback_days": window_days(start_date, end_date),
                "limit": limit,
                "include_content": "true",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise VendorUnavailableError(
            f"Alpaca news gateway request failed: {type(exc).__name__}"
        ) from None
    if response.status_code == 404:
        raise NoMarketDataError(ticker, detail="production does not serve news for this symbol")
    if response.status_code in (401, 403):
        raise VendorUnavailableError("the production gateway rejected the runner token")
    if response.status_code >= 400:
        raise VendorUnavailableError(f"Alpaca news gateway returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise VendorUnavailableError("Alpaca news gateway returned invalid JSON") from exc

    news = payload.get("news") if isinstance(payload, dict) else None
    items = news.get("items") if isinstance(news, dict) else None
    if (
        not isinstance(items, list)
        or news.get("provider") != _PROVIDER
        or payload.get("as_of") != end_date
        or payload.get("symbol") != ticker.upper()
    ):
        raise VendorUnavailableError("Alpaca news gateway returned an invalid response")

    articles: list[dict] = []
    for item in items:
        try:
            published_at = _timestamp(item["published_at_utc"])
            # The gateway cuts the window at the end of the as-of day in New York, so the
            # window is checked against its New York publication date, not the UTC date.
            published_on = date.fromisoformat(str(item["published_on"]))
            title = str(item["title"])
        except (KeyError, TypeError, ValueError):
            continue
        if published_on > window_end:
            raise VendorUnavailableError("Alpaca news gateway returned future-dated news")
        if published_on < window_start:
            continue
        articles.append(
            {
                "title": title,
                "summary": str(item.get("excerpt") or ""),
                "publisher": str(item.get("publisher") or "Unknown"),
                "link": str(item.get("url") or ""),
                "pub_date": published_at,
                "content": str(item.get("content") or ""),
                "vendor": "Alpaca",
            }
        )
        if len(articles) >= limit:
            break
    return articles
