"""Alpaca News API integration."""

from __future__ import annotations

import html
import os
from datetime import UTC, datetime, time
from html.parser import HTMLParser

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import (
    VendorNotConfiguredError,
    VendorUnavailableError,
)

_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
_MAX_PAGES = 20


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _plain_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return html.unescape(" ".join(" ".join(parser.parts).split()))


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def fetch_news_articles(
    ticker: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Return normalized, point-in-time Alpaca news for ``ticker``."""

    api_key = os.getenv("APCA_API_KEY_ID", "").strip()
    api_secret = os.getenv("APCA_API_SECRET_KEY", "").strip()
    if not api_key or not api_secret:
        raise VendorNotConfiguredError(
            "APCA_API_KEY_ID and APCA_API_SECRET_KEY are required for Alpaca news"
        )

    limit = get_config()["news_article_limit"]
    start = datetime.combine(
        datetime.strptime(start_date, "%Y-%m-%d").date(),
        time.min,
        tzinfo=UTC,
    )
    end = datetime.combine(
        datetime.strptime(end_date, "%Y-%m-%d").date(),
        time.max,
        tzinfo=UTC,
    )
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    page_token: str | None = None
    articles: list[dict] = []

    for _ in range(_MAX_PAGES):
        params = {
            "symbols": ticker,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "sort": "desc",
            "limit": min(50, limit),
            "include_content": "true",
        }
        if page_token:
            params["page_token"] = page_token
        try:
            response = requests.get(
                _NEWS_URL,
                headers=headers,
                params=params,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise VendorUnavailableError(
                f"Alpaca News request failed: {type(exc).__name__}"
            ) from None

        raw_articles = payload.get("news")
        if not isinstance(raw_articles, list):
            raise VendorUnavailableError("Alpaca News returned an invalid response")
        for raw in raw_articles:
            try:
                published_at = _parse_timestamp(raw["created_at"])
                if not start <= published_at <= end:
                    continue
                raw_content = raw.get("content") or ""
                articles.append(
                    {
                        "title": str(raw["headline"]),
                        "summary": str(raw.get("summary") or ""),
                        "publisher": str(raw.get("source") or "Unknown"),
                        "link": str(raw.get("url") or ""),
                        "pub_date": published_at,
                        "content": _plain_text(str(raw_content)) if raw_content else "",
                        "vendor": "Alpaca",
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
            if len(articles) >= limit:
                return articles

        page_token = payload.get("next_page_token")
        if not page_token:
            break

    return articles
