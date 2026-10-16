"""Authenticated, single-concurrency TradingAgents runner API."""

from __future__ import annotations

import hmac
import os
import threading
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.runner.executor import execute_job
from tradingagents.runner.store import JobStore

_ANALYSTS = frozenset({"market", "social", "news", "fundamentals"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateJobRequest(_StrictModel):
    ticker: str = Field(min_length=1, max_length=24)
    trade_date: str
    analysts: tuple[str, ...] = ("market", "social", "news", "fundamentals")

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        safe_ticker_component(normalized)
        return normalized

    @field_validator("trade_date")
    @classmethod
    def validate_trade_date(cls, value: str) -> str:
        try:
            valid = datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
        except ValueError:
            valid = False
        if not valid or value > get_current_date():
            raise ValueError("trade_date must be YYYY-MM-DD and cannot be in the future")
        return value

    @field_validator("analysts")
    @classmethod
    def validate_analysts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)) or not set(value) <= _ANALYSTS:
            raise ValueError("analysts must be a non-empty unique subset of the supported analysts")
        return value


def _read_token() -> str:
    path = os.getenv("TRADINGAGENTS_RUNNER_TOKEN_FILE", "")
    if not path:
        raise RuntimeError("TRADINGAGENTS_RUNNER_TOKEN_FILE is required")
    token = Path(path).read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("runner API token must contain at least 32 characters")
    return token


def create_app(
    *,
    store: JobStore | None = None,
    token: str | None = None,
    results_root: Path | None = None,
) -> FastAPI:
    data_root = Path(os.getenv("TRADINGAGENTS_RUNNER_DATA_DIR", "~/.tradingagents/runner")).expanduser()
    job_store = store or JobStore(data_root / "jobs.sqlite3")
    api_token = token or _read_token()
    reports = results_root or data_root / "reports"
    execution_lock = threading.Lock()

    app = FastAPI(title="TradingAgents private runner", docs_url=None, redoc_url=None)
    app.state.store = job_store

    def authorize(authorization: str = Header(default="")) -> None:
        scheme, _, supplied = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, api_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid runner API token",
            )

    def run(job_id: str) -> None:
        with execution_lock:
            execute_job(job_store, job_id, reports)

    @app.get("/health/live")
    def health_live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready() -> dict:
        return {"status": "ready", "max_concurrent_jobs": 1}

    @app.post("/v1/jobs", status_code=202, dependencies=[Depends(authorize)])
    def create_job(body: CreateJobRequest, background: BackgroundTasks) -> dict:
        if job_store.has_active_job():
            raise HTTPException(status_code=409, detail="an analysis is already queued or running")
        job = job_store.create_job(body.ticker, body.trade_date, body.analysts)
        background.add_task(run, job["id"])
        return job

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(authorize)])
    def get_job(job_id: str) -> dict:
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.get("/v1/jobs/{job_id}/events", dependencies=[Depends(authorize)])
    def get_events(job_id: str, after: int = 0) -> dict:
        if after < 0:
            raise HTTPException(status_code=422, detail="after must not be negative")
        if job_store.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {"events": job_store.events_after(job_id, after)}

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)


if os.getenv("TRADINGAGENTS_RUNNER_AUTOCREATE_APP") == "true":
    app = create_app()
