"""Combined Alpaca and Yahoo ticker-news behavior."""

from copy import deepcopy
from datetime import datetime

import pytest

import tradingagents.dataflows.vendors.alpaca.news as alpaca_news
import tradingagents.dataflows.vendors.combined_news as combined_news
import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)


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


def _gateway(monkeypatch, tmp_path, get=None):
    token = tmp_path / "token"
    token.write_text("r" * 32, encoding="utf-8")
    monkeypatch.setenv("OPENBB_RESEARCH_URL", "https://prod.example/api/research")
    monkeypatch.setenv("OPENBB_RESEARCH_TOKEN_FILE", str(token))
    monkeypatch.setattr(alpaca_news, "get_config", lambda: {"news_article_limit": 5})
    if get is not None:
        monkeypatch.setattr(alpaca_news.requests, "get", get)


def _payload(items, as_of="2026-10-15", provider="alpaca-news"):
    return {
        "symbol": "AAPL",
        "as_of": as_of,
        "news": {"provider": provider, "retrieved_at_utc": "2026-10-16T00:00:00Z", "items": items},
    }


@pytest.mark.unit
def test_alpaca_needs_the_production_gateway_not_an_alpaca_key(monkeypatch):
    monkeypatch.delenv("OPENBB_RESEARCH_URL", raising=False)
    monkeypatch.setenv("APCA_API_KEY_ID", "paper-key")  # a stray key must not be used
    monkeypatch.setenv("APCA_API_SECRET_KEY", "paper-secret")

    with pytest.raises(VendorNotConfiguredError):
        alpaca_news.fetch_news_articles("AAPL", "2026-10-01", "2026-10-15")


@pytest.mark.unit
def test_alpaca_news_comes_through_the_gateway_with_the_runner_token(monkeypatch, tmp_path):
    calls = []

    def fake_get(url, *, headers, params, timeout):
        calls.append((url, headers, params, timeout))
        return _Response(
            _payload(
                [
                    {
                        "published_on": "2026-10-14",
                        "published_at_utc": "2026-10-14T12:00:00+00:00",
                        "title": "Inside",
                        "excerpt": "Summary",
                        "url": "https://example.com/inside",
                        "publisher": "Publisher",
                        "content": "Full body text",
                    },
                    {  # published 23:30 New York on the as-of day: UTC date is the next day
                        "published_on": "2026-10-15",
                        "published_at_utc": "2026-10-16T03:30:00+00:00",
                        "title": "Late evening",
                    },
                    {
                        "published_on": "2026-09-01",
                        "published_at_utc": "2026-09-01T12:00:00+00:00",
                        "title": "Too old",
                    },
                ]
            )
        )

    _gateway(monkeypatch, tmp_path, fake_get)

    articles = alpaca_news.fetch_news_articles("AAPL", "2026-10-01", "2026-10-15")

    assert [item["title"] for item in articles] == ["Inside", "Late evening"]
    assert articles[0]["content"] == "Full body text" and articles[0]["vendor"] == "Alpaca"
    url, headers, params, _ = calls[0]
    assert url == "https://prod.example/api/research/AAPL/alpaca-news"
    assert headers == {"Authorization": "Bearer " + "r" * 32}
    assert params == {
        "as_of": "2026-10-15",
        "lookback_days": 15,
        "limit": 5,
        "include_content": "true",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "error"),
    [(404, NoMarketDataError), (401, VendorUnavailableError), (502, VendorUnavailableError)],
)
def test_gateway_refusals_map_to_vendor_errors_so_the_chain_can_fall_back(
    monkeypatch, tmp_path, status, error
):
    response = _Response({})
    response.status_code = status
    _gateway(monkeypatch, tmp_path, lambda *args, **kwargs: response)

    with pytest.raises(error):
        alpaca_news.fetch_news_articles("AAPL", "2026-10-01", "2026-10-15")


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        _payload([], provider="openbb-nasdaq"),
        _payload([], as_of="2026-10-16"),
        _payload([{"published_on": "2026-10-16", "published_at_utc": "2026-10-16T14:00:00Z",
                   "title": "From the future"}]),
        {"unexpected": True},
    ],
)
def test_an_inconsistent_gateway_response_is_rejected(monkeypatch, tmp_path, payload):
    _gateway(monkeypatch, tmp_path, lambda *args, **kwargs: _Response(payload))

    with pytest.raises(VendorUnavailableError):
        alpaca_news.fetch_news_articles("AAPL", "2026-10-01", "2026-10-15")


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
