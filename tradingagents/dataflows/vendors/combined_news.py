"""Combined, deduplicated ticker news from Alpaca and Yahoo Finance."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import VendorError, VendorUnavailableError
from tradingagents.dataflows.vendors.alpaca import fetch_news_articles as fetch_alpaca
from tradingagents.dataflows.vendors.yahoo.news import (
    fetch_news_articles_yfinance as fetch_yahoo,
)

_TRACKING_KEYS = {"gclid", "fbclid", "ref", "source"}


def _canonical_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value.strip())
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    ]
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            urlencode(query),
            "",
        )
    )


def _headline_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _deduplicate(articles: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen_urls: set[str] = set()
    headline_vendors: dict[tuple[str, str], set[str]] = {}
    for article in sorted(
        articles,
        key=lambda item: item.get("pub_date") or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    ):
        url = _canonical_url(article.get("link", ""))
        headline = _headline_key(article.get("title", ""))
        published = article.get("pub_date")
        published_day = published.date().isoformat() if published else ""
        headline_key = (headline, published_day)
        vendor = str(article.get("vendor") or "")
        prior_vendors = headline_vendors.get(headline_key, set())
        if (url and url in seen_urls) or (
            headline and prior_vendors and (not url or vendor not in prior_vendors)
        ):
            continue
        if url:
            seen_urls.add(url)
        if headline:
            headline_vendors.setdefault(headline_key, set()).add(vendor)
        unique.append(article)
    return unique


def _format_articles(ticker: str, start_date: str, end_date: str, articles: list[dict]) -> str:
    config = get_config()
    per_article_limit = config["news_body_max_chars"]
    remaining_body_chars = config["news_body_total_max_chars"]
    output = [f"## {ticker} News, from {start_date} to {end_date}:"]
    for article in articles[: config["news_article_limit"]]:
        source = article.get("publisher") or "Unknown"
        vendor = article.get("vendor") or "Unknown"
        output.append(f"\n### {article['title']} (source: {source}; vendor: {vendor})")
        if article.get("pub_date"):
            output.append(f"Published: {article['pub_date'].isoformat()}")
        if article.get("summary"):
            output.append(str(article["summary"]))
        content = str(article.get("content") or "")
        if content and remaining_body_chars > 0:
            body = content[: min(per_article_limit, remaining_body_chars)]
            output.append(f"Article body excerpt: {body}")
            remaining_body_chars -= len(body)
        if article.get("link"):
            output.append(f"Link: {article['link']}")
    return "\n".join(output)


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Fetch both feeds, merge their articles, and remove cross-feed duplicates."""

    articles: list[dict] = []
    errors: list[str] = []
    for vendor, fetcher in (("Alpaca", fetch_alpaca), ("Yahoo Finance", fetch_yahoo)):
        try:
            articles.extend(fetcher(ticker, start_date, end_date))
        except VendorError as exc:
            errors.append(f"{vendor}: {exc}")

    unique = _deduplicate(articles)
    if unique:
        return _format_articles(ticker, start_date, end_date, unique)
    if errors:
        raise VendorUnavailableError("; ".join(errors))
    return f"No news found for {ticker} between {start_date} and {end_date}"
