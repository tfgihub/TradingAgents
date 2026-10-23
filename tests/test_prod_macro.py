"""Macro indicators through the private production gateway."""

import pytest
import requests

import tradingagents.dataflows.vendors.prod_macro as prod_macro
from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorUnavailableError


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _payload(observations, **changes):
    base = {
        "indicator": "fed_funds_rate",
        "title": "Effective Federal Funds Rate",
        "units": "Percent",
        "frequency": "Daily",
        "source": "Federal Reserve Bank of New York",
        "as_of": "2026-10-23",
        "window_start": "2025-10-23",
        "observations": observations,
    }
    return {**base, **changes}


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("r" * 32, encoding="utf-8")
    monkeypatch.setenv("OPENBB_RESEARCH_URL", "https://prod.example/api/research")
    monkeypatch.setenv("OPENBB_RESEARCH_TOKEN_FILE", str(token))


def _serve(monkeypatch, response):
    calls = []

    def get(url, *, headers, params, timeout):
        calls.append((url, headers, params, timeout))
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(prod_macro.requests, "get", get)
    return calls


@pytest.mark.unit
def test_the_request_names_the_indicator_the_date_and_the_window(gateway, monkeypatch):
    calls = _serve(monkeypatch, _Response(_payload([{"date": "2026-10-22", "value": 3.88}])))

    prod_macro.get_macro_data(" Fed_Funds_Rate ", "2026-10-23", 90)

    url, headers, params, timeout = calls[0]
    assert url == "https://prod.example/api/research/macro/fed_funds_rate"
    assert headers == {"Authorization": "Bearer " + "r" * 32}
    assert params == {"as_of": "2026-10-23", "lookback_days": 90}
    assert timeout == prod_macro.REQUEST_TIMEOUT_SECONDS


@pytest.mark.unit
def test_the_report_has_the_latest_value_the_change_and_a_table(gateway, monkeypatch):
    rows = [{"date": "2026-10-20", "value": 3.87}, {"date": "2026-10-22", "value": 3.88}]
    _serve(monkeypatch, _Response(_payload(rows)))

    out = prod_macro.get_macro_data("fed_funds_rate", "2026-10-23")

    assert "## Federal Reserve Bank of New York: Effective Federal Funds Rate" in out
    assert "- Units: Percent" in out and "- Window: 2025-10-23 to 2026-10-23" in out
    assert "**Latest:** 3.88 (2026-10-22)" in out and "+0.01 (+0.26%)" in out
    assert "| 2026-10-20 | 3.87 |" in out and "| 2026-10-22 | 3.88 |" in out


@pytest.mark.unit
def test_a_long_series_shows_only_the_recent_rows(gateway, monkeypatch):
    rows = [
        {"date": f"2026-{m:02d}-{d:02d}", "value": float(m)}
        for m in range(1, 10)
        for d in (1, 5, 10, 15, 20, 25, 27)
    ]
    _serve(monkeypatch, _Response(_payload(rows)))

    out = prod_macro.get_macro_data("fed_funds_rate", "2026-10-23")

    assert f"most recent {prod_macro.MAX_ROWS} of {len(rows)}" in out
    assert out.count("\n| 2026-") == prod_macro.MAX_ROWS


@pytest.mark.unit
def test_an_empty_window_says_so_instead_of_inventing_a_value(gateway, monkeypatch):
    _serve(monkeypatch, _Response(_payload([])))

    out = prod_macro.get_macro_data("cpi", "2026-10-23")

    assert "No observations" in out and "Latest" not in out


@pytest.mark.unit
def test_an_indicator_prod_does_not_have_gets_guidance_not_an_error(gateway, monkeypatch):
    _serve(monkeypatch, _Response({"detail": "no keyless source for 'vix'; available: cpi"}, 404))

    out = prod_macro.get_macro_data("vix", "2026-10-23")

    assert out == "Macro data: no keyless source for 'vix'; available: cpi"


@pytest.mark.unit
@pytest.mark.parametrize(
    "response",
    [
        _Response({}, 502),
        _Response({"detail": "x"}, 500),
        requests.ConnectTimeout("slow"),
        _Response(["not", "an", "object"]),
        _Response({"observations": [{"date": "2026-10-22"}]}),  # no value, no metadata
        _Response(_payload([{"date": "2026-10-24", "value": 3.9}])),  # after the as-of date
        _Response(_payload([{"date": "2026-10-22", "value": "high"}])),
    ],
)
def test_a_failed_or_malformed_answer_is_an_unavailable_vendor(gateway, monkeypatch, response):
    _serve(monkeypatch, response)

    with pytest.raises(VendorUnavailableError):
        prod_macro.get_macro_data("fed_funds_rate", "2026-10-23")


@pytest.mark.unit
def test_an_invalid_date_is_refused_before_any_request(gateway, monkeypatch):
    calls = _serve(monkeypatch, _Response(_payload([])))

    with pytest.raises(VendorUnavailableError):
        prod_macro.get_macro_data("cpi", "yesterday")

    assert calls == []


@pytest.mark.unit
def test_without_the_gateway_configured_it_is_not_configured(monkeypatch):
    monkeypatch.delenv("OPENBB_RESEARCH_URL", raising=False)

    with pytest.raises(VendorNotConfiguredError):
        prod_macro.get_macro_data("cpi", "2026-10-23")


@pytest.mark.unit
def test_production_is_asked_first_and_fred_is_the_fallback():
    from tradingagents.dataflows.router import VENDOR_METHODS
    from tradingagents.default_config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["data_vendors"]["macro_data"] == "prod_macro,fred"
    assert set(VENDOR_METHODS["get_macro_indicators"]) == {"prod_macro", "fred"}
