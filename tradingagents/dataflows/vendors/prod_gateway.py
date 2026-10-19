"""Connection settings for thinkpad-prod's private research gateway.

Production owns every outside credential (OpenBB, Alpaca). ai-node reaches them only through
this authenticated HTTPS gateway, with the runner's own token.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorUnavailableError


def gateway_settings() -> tuple[str, str]:
    """Return the gateway base URL and bearer token, or raise if either is unusable."""
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
        raise VendorNotConfiguredError("OPENBB_RESEARCH_URL must be a private HTTPS endpoint")
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


def window_days(start_date: str, end_date: str) -> int:
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise VendorUnavailableError("the news window contains an invalid date") from exc
    if start > end:
        raise VendorUnavailableError("the news window starts after it ends")
    return max(1, min(365, (end - start).days + 1))
