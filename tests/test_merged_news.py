"""Alpaca, Yahoo and OpenBB ticker news merged into one report."""

from datetime import UTC, datetime

import pytest

import tradingagents.dataflows.vendors.combined_news as combined_news
import tradingagents.dataflows.vendors.openbb_news as openbb_news
from tradingagents.dataflows.errors import VendorUnavailableError

CONFIG = {
    "news_article_limit": 2,
    "news_supplement_article_limit": 2,
    "news_body_max_chars": 100,
    "news_body_total_max_chars": 1000,
}


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _article(title, vendor, day, hour=12, link=None):
    return {
        "title": title,
        "summary": "",
        "publisher": "Publisher",
        "link": link if link is not None else f"https://{vendor.lower()}.example/{title}",
        "pub_date": datetime(2026, 10, day, hour, tzinfo=UTC),
        "content": "",
        "vendor": vendor,
    }


def _feeds(monkeypatch, alpaca, yahoo, openbb):
    monkeypatch.setattr(combined_news, "get_config", lambda: CONFIG)

    def feed(value):
        def fetch(*args):
            if isinstance(value, Exception):
                raise value
            return value

        return fetch

    monkeypatch.setattr(combined_news, "fetch_alpaca", feed(alpaca))
    monkeypatch.setattr(combined_news, "fetch_yahoo", feed(yahoo))
    monkeypatch.setattr(combined_news, "fetch_openbb", feed(openbb))


@pytest.mark.unit
def test_openbb_articles_follow_the_main_feeds_and_never_displace_them(monkeypatch):
    _feeds(
        monkeypatch,
        alpaca=[_article("A1", "Alpaca", 14), _article("A2", "Alpaca", 13)],
        yahoo=[_article("Y1", "Yahoo", 12)],
        # newer than every Alpaca article, yet listed after them
        openbb=[_article("O1", "OpenBB", 15, hour=0), _article("O2", "OpenBB", 14, hour=0)],
    )

    output = combined_news.get_news_merged("AAPL", "2026-10-01", "2026-10-15")

    titles = [line for line in output.splitlines() if line.startswith("### ")]
    assert [t.split(" (")[0] for t in titles] == ["### A1", "### A2", "### O1", "### O2"]
    assert "vendor: OpenBB" in titles[2]


@pytest.mark.unit
def test_each_feed_is_capped_on_its_own(monkeypatch):
    _feeds(
        monkeypatch,
        alpaca=[_article(f"A{n}", "Alpaca", 10 + n) for n in range(4)],
        yahoo=[],
        openbb=[_article(f"O{n}", "OpenBB", 10 + n) for n in range(4)],
    )

    output = combined_news.get_news_merged("AAPL", "2026-10-01", "2026-10-15")

    assert output.count("\n### ") == 4  # 2 main + 2 OpenBB
    assert output.count("vendor: OpenBB") == 2


@pytest.mark.unit
def test_the_same_story_from_two_feeds_is_listed_once(monkeypatch):
    shared = "https://news.example/story?utm_source=x"
    _feeds(
        monkeypatch,
        alpaca=[_article("Story", "Alpaca", 14, link=shared)],
        yahoo=[],
        openbb=[_article("Story again", "OpenBB", 14, link="https://news.example/story")],
    )

    output = combined_news.get_news_merged("AAPL", "2026-10-01", "2026-10-15")

    assert output.count("\n### ") == 1


@pytest.mark.unit
def test_openbb_down_leaves_the_other_feeds(monkeypatch):
    _feeds(
        monkeypatch,
        alpaca=[_article("A1", "Alpaca", 14)],
        yahoo=[],
        openbb=VendorUnavailableError("gateway down"),
    )

    assert "### A1" in combined_news.get_news_merged("AAPL", "2026-10-01", "2026-10-15")


@pytest.mark.unit
def test_openbb_alone_is_enough_when_alpaca_and_yahoo_have_nothing(monkeypatch):
    _feeds(
        monkeypatch,
        alpaca=VendorUnavailableError("alpaca down"),
        yahoo=[],
        openbb=[_article("O1", "OpenBB", 14)],
    )

    assert "### O1" in combined_news.get_news_merged("AAPL", "2026-10-01", "2026-10-15")


@pytest.mark.unit
def test_every_feed_failing_is_unavailable_so_the_chain_can_fall_back(monkeypatch):
    _feeds(
        monkeypatch,
        alpaca=VendorUnavailableError("alpaca down"),
        yahoo=VendorUnavailableError("yahoo down"),
        openbb=VendorUnavailableError("gateway down"),
    )

    with pytest.raises(VendorUnavailableError, match="OpenBB: gateway down"):
        combined_news.get_news_merged("AAPL", "2026-10-01", "2026-10-15")


@pytest.mark.unit
def test_no_articles_anywhere_is_reported_as_none_found(monkeypatch):
    _feeds(monkeypatch, alpaca=[], yahoo=[], openbb=[])

    assert "No news found for AAPL" in combined_news.get_news_merged(
        "AAPL", "2026-10-01", "2026-10-15"
    )


def _gateway(monkeypatch, tmp_path, items, calls=None):
    token = tmp_path / "token"
    token.write_text("r" * 32, encoding="utf-8")
    monkeypatch.setenv("OPENBB_RESEARCH_URL", "https://prod.example/api/research")
    monkeypatch.setenv("OPENBB_RESEARCH_TOKEN_FILE", str(token))
    monkeypatch.setattr(openbb_news, "get_config", lambda: {"news_article_limit": 5})

    def get(url, *, headers, params, timeout):
        if calls is not None:
            calls.append(params)
        return _Response(
            {
                "symbol": "AAPL",
                "as_of": "2026-10-15",
                "news": {"provider": "openbb-nasdaq", "items": items},
            }
        )

    monkeypatch.setattr(openbb_news.requests, "get", get)


@pytest.mark.unit
def test_openbb_articles_ask_for_the_most_the_gateway_allows(monkeypatch, tmp_path):
    # The gateway cuts the newest `limit` articles to the window, so a small limit
    # finds nothing for an older date.
    calls = []
    items = [
        {"published_on": "2026-10-14", "title": "In", "url": "https://x.example/in",
         "excerpt": "Excerpt", "publisher": "Zacks"},
        {"published_on": "2026-09-01", "title": "Too old", "url": "https://x.example/old"},
    ]
    _gateway(monkeypatch, tmp_path, items, calls)

    articles = openbb_news.fetch_news_articles("AAPL", "2026-10-01", "2026-10-15")

    assert calls[0]["limit"] == 50
    assert [a["title"] for a in articles] == ["In"]
    assert articles[0]["vendor"] == "OpenBB" and articles[0]["summary"] == "Excerpt"
    assert articles[0]["pub_date"] == datetime(2026, 10, 14, tzinfo=UTC)


@pytest.mark.unit
def test_future_dated_openbb_news_is_refused(monkeypatch, tmp_path):
    items = [{"published_on": "2026-10-16", "title": "Future", "url": "https://x.example/f"}]
    _gateway(monkeypatch, tmp_path, items)

    with pytest.raises(VendorUnavailableError, match="future-dated"):
        openbb_news.fetch_news_articles("AAPL", "2026-10-01", "2026-10-15")
