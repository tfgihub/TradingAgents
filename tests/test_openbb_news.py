"""Private OpenBB company-news provider behavior."""

import json

import pytest
import requests

import tradingagents.dataflows.vendors.openbb_news as openbb_news
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.mark.unit
def test_openbb_news_uses_bounded_as_of_request(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("r" * 32, encoding="utf-8")
    monkeypatch.setenv("OPENBB_RESEARCH_URL", "https://prod.example/api/research")
    monkeypatch.setenv("OPENBB_RESEARCH_TOKEN_FILE", str(token))
    monkeypatch.setattr(openbb_news, "get_config", lambda: {"news_article_limit": 3})
    calls = []

    def get(url, *, headers, params, timeout):
        calls.append((url, headers, params, timeout))
        return _Response(
            {
                "symbol": "AAPL",
                "as_of": "2026-10-17",
                "news": {
                    "provider": "openbb-nasdaq",
                    "retrieved_at_utc": "2026-10-18T04:00:00Z",
                    "items": [
                        {
                            "published_on": "2026-10-17",
                            "title": "Apple announces update",
                            "excerpt": "A bounded excerpt.",
                            "url": "https://example.com/apple",
                            "publisher": "Publisher",
                            "topic": "Technology",
                        }
                    ],
                }
            }
        )

    monkeypatch.setattr(openbb_news.requests, "get", get)

    output = openbb_news.get_news("AAPL", "2026-10-10", "2026-10-17")

    assert calls == [
        (
            "https://prod.example/api/research/AAPL/news",
            {"Authorization": f"Bearer {'r' * 32}"},
            {"as_of": "2026-10-17", "lookback_days": 8, "limit": 3},
            30,
        )
    ]
    assert "Apple announces update" in output
    assert "vendor: OpenBB" in output
    assert "OpenBB retrieval timestamp: 2026-10-18T04:00:00Z" in output


@pytest.mark.unit
def test_openbb_news_requires_private_endpoint_and_token(monkeypatch):
    monkeypatch.delenv("OPENBB_RESEARCH_URL", raising=False)
    monkeypatch.delenv("OPENBB_RESEARCH_TOKEN_FILE", raising=False)

    with pytest.raises(VendorNotConfiguredError):
        openbb_news.get_news("AAPL", "2026-10-10", "2026-10-17")


@pytest.mark.unit
def test_openbb_no_data_allows_router_fallback(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("r" * 32, encoding="utf-8")
    monkeypatch.setenv("OPENBB_RESEARCH_URL", "https://prod.example/api/research")
    monkeypatch.setenv("OPENBB_RESEARCH_TOKEN_FILE", str(token))
    monkeypatch.setattr(openbb_news, "get_config", lambda: {"news_article_limit": 3})
    monkeypatch.setattr(
        openbb_news.requests,
        "get",
        lambda *args, **kwargs: _Response(
            {
                "symbol": "AAPL",
                "as_of": "2020-01-02",
                "news": {"provider": "openbb-nasdaq", "items": []},
            }
        ),
    )

    with pytest.raises(NoMarketDataError):
        openbb_news.get_news("AAPL", "2020-01-01", "2020-01-02")


@pytest.mark.unit
def test_openbb_http_failure_is_vendor_unavailable(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("r" * 32, encoding="utf-8")
    monkeypatch.setenv("OPENBB_RESEARCH_URL", "https://prod.example/api/research")
    monkeypatch.setenv("OPENBB_RESEARCH_TOKEN_FILE", str(token))
    monkeypatch.setattr(openbb_news, "get_config", lambda: {"news_article_limit": 3})
    monkeypatch.setattr(
        openbb_news.requests,
        "get",
        lambda *args, **kwargs: _Response({"detail": "unavailable"}, status_code=502),
    )

    with pytest.raises(VendorUnavailableError):
        openbb_news.get_news("AAPL", "2026-10-10", "2026-10-17")


@pytest.mark.unit
def test_openbb_rejects_news_after_historical_cutoff(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("r" * 32, encoding="utf-8")
    monkeypatch.setenv("OPENBB_RESEARCH_URL", "https://prod.example/api/research")
    monkeypatch.setenv("OPENBB_RESEARCH_TOKEN_FILE", str(token))
    monkeypatch.setattr(openbb_news, "get_config", lambda: {"news_article_limit": 3})
    monkeypatch.setattr(
        openbb_news.requests,
        "get",
        lambda *args, **kwargs: _Response(
            {
                "symbol": "AAPL",
                "as_of": "2020-01-02",
                "news": {
                    "provider": "openbb-nasdaq",
                    "items": [
                        {
                            "published_on": "2020-01-03",
                            "title": "Future article",
                            "url": "https://example.com/future",
                        }
                    ],
                },
            }
        ),
    )

    with pytest.raises(VendorUnavailableError, match="future-dated"):
        openbb_news.get_news("AAPL", "2020-01-01", "2020-01-02")
