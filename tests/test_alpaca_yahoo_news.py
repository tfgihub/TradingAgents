"""Combined Alpaca and Yahoo ticker-news behavior."""

from copy import deepcopy
from datetime import datetime

import pytest

import tradingagents.dataflows.vendors.alpaca.news as alpaca_news
import tradingagents.dataflows.vendors.combined_news as combined_news
import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import VendorNotConfiguredError


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _article(
    title,
    *,
    vendor="Alpaca",
    link="https://example.com/story",
    published="2026-10-14T12:00:00Z",
    content="",
):
    return {
        "title": title,
        "summary": f"{title} summary",
        "publisher": "Wire",
        "link": link,
        "pub_date": datetime.fromisoformat(published.replace("Z", "+00:00")),
        "content": content,
        "vendor": vendor,
    }


@pytest.mark.unit
def test_alpaca_requires_both_credentials(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)

    with pytest.raises(VendorNotConfiguredError):
        alpaca_news.fetch_news_articles("AAPL", "2026-10-01", "2026-10-15")


@pytest.mark.unit
def test_alpaca_request_paginates_strips_html_and_enforces_window(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "paper-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "paper-secret")
    monkeypatch.setattr(
        alpaca_news,
        "get_config",
        lambda: {"news_article_limit": 5},
    )
    calls = []
    pages = [
        {
            "news": [
                {
                    "headline": "Inside",
                    "summary": "Summary",
                    "source": "Publisher",
                    "url": "https://example.com/inside",
                    "created_at": "2026-10-14T12:00:00Z",
                    "content": "<p>Full &amp; useful</p><script>ignore()</script>",
                },
                {
                    "headline": "Outside",
                    "created_at": "2026-10-16T00:00:00Z",
                },
            ],
            "next_page_token": "next",
        },
        {
            "news": [
                {
                    "headline": "Second page",
                    "created_at": "2026-10-13T12:00:00Z",
                }
            ]
        },
    ]

    def fake_get(url, *, headers, params, timeout):
        calls.append((url, headers, params, timeout))
        return _Response(pages[len(calls) - 1])

    monkeypatch.setattr(alpaca_news.requests, "get", fake_get)

    articles = alpaca_news.fetch_news_articles("AAPL", "2026-10-01", "2026-10-15")

    assert [item["title"] for item in articles] == ["Inside", "Second page"]
    assert articles[0]["content"] == "Full & useful"
    assert calls[0][1] == {
        "APCA-API-KEY-ID": "paper-key",
        "APCA-API-SECRET-KEY": "paper-secret",
    }
    assert calls[0][2]["include_content"] == "true"
    assert calls[1][2]["page_token"] == "next"
    assert calls[0][3] == 20


@pytest.mark.unit
def test_combined_feed_deduplicates_cross_vendor_and_bounds_bodies(monkeypatch):
    alpaca = _article(
        "Company wins contract",
        link="https://example.com/story?utm_source=alpaca",
        content="A" * 20,
    )
    yahoo_duplicate = _article(
        "Company wins contract",
        vendor="Yahoo Finance",
        link="https://example.com/story?utm_source=yahoo",
    )
    yahoo_distinct = _article(
        "Second story",
        vendor="Yahoo Finance",
        link="https://example.com/second",
        published="2026-10-13T12:00:00Z",
    )
    monkeypatch.setattr(combined_news, "fetch_alpaca", lambda *args: [alpaca])
    monkeypatch.setattr(
        combined_news,
        "fetch_yahoo",
        lambda *args: [yahoo_duplicate, yahoo_distinct],
    )
    monkeypatch.setattr(
        combined_news,
        "get_config",
        lambda: {
            "news_article_limit": 20,
            "news_body_max_chars": 8,
            "news_body_total_max_chars": 8,
        },
    )

    output = combined_news.get_news("AAPL", "2026-10-01", "2026-10-15")

    assert output.count("Company wins contract") == 2  # heading plus its summary
    assert "Second story" in output
    assert "Article body excerpt: AAAAAAAA" in output
    assert "AAAAAAAAA" not in output
    assert "vendor: Alpaca" in output
    assert "vendor: Yahoo Finance" in output


@pytest.mark.unit
def test_same_vendor_can_keep_same_headline_at_distinct_urls(monkeypatch):
    monkeypatch.setattr(combined_news, "fetch_alpaca", lambda *args: [])
    monkeypatch.setattr(
        combined_news,
        "fetch_yahoo",
        lambda *args: [
            _article("Market update", vendor="Yahoo Finance", link="https://example.com/one"),
            _article("Market update", vendor="Yahoo Finance", link="https://example.com/two"),
        ],
    )
    monkeypatch.setattr(
        combined_news,
        "get_config",
        lambda: {
            "news_article_limit": 20,
            "news_body_max_chars": 3000,
            "news_body_total_max_chars": 12000,
        },
    )

    output = combined_news.get_news("AAPL", "2026-10-01", "2026-10-15")

    assert output.count("### Market update") == 2


@pytest.mark.unit
def test_missing_alpaca_configuration_degrades_to_yahoo(monkeypatch):
    def not_configured(*args):
        raise VendorNotConfiguredError("missing credentials")

    monkeypatch.setattr(combined_news, "fetch_alpaca", not_configured)
    monkeypatch.setattr(
        combined_news,
        "fetch_yahoo",
        lambda *args: [_article("Yahoo story", vendor="Yahoo Finance")],
    )

    output = combined_news.get_news("AAPL", "2026-10-01", "2026-10-15")

    assert "Yahoo story" in output
    assert "vendor: Yahoo Finance" in output


@pytest.mark.unit
def test_default_routing_combines_ticker_news_but_keeps_global_news_on_yahoo(monkeypatch):
    calls = []

    def combined(*args):
        calls.append(("combined", args))
        return "COMBINED"

    def yahoo(*args):
        calls.append(("yahoo", args))
        return "YAHOO"

    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "alpaca_yahoo", combined)
    monkeypatch.setitem(router.VENDOR_METHODS["get_global_news"], "yfinance", yahoo)
    with run_config(deepcopy(default_config.DEFAULT_CONFIG)):
        ticker_result = router.route_to_vendor(
            "get_news",
            "AAPL",
            "2026-10-01",
            "2026-10-15",
        )
        global_result = router.route_to_vendor("get_global_news", "2026-10-15")

    assert ticker_result == "COMBINED"
    assert global_result == "YAHOO"
    assert [call[0] for call in calls] == ["combined", "yahoo"]
