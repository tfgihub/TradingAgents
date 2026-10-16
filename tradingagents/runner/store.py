"""Durable SQLite job and progress-event storage."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    analysts_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    mode TEXT NOT NULL DEFAULT 'live'
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_job_sequence_idx
                    ON events(job_id, sequence);
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "mode" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN mode TEXT NOT NULL DEFAULT 'live'")
            # A job that was mid-run cannot resume; queued jobs stay queued.
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', updated_at = ?, error = ?
                WHERE status = 'running'
                """,
                (_now(), "runner restarted before the analysis completed"),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def count_active(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchone()
        return int(row[0])

    def has_queued(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM jobs WHERE status = 'queued' LIMIT 1").fetchone()
        return row is not None

    def claim_next_queued(self) -> dict | None:
        """Atomically move the oldest queued job to running and return it."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id FROM jobs WHERE status = 'queued'
                ORDER BY (mode = 'backtest'), created_at, rowid LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
        return self.get_job(row["id"])

    def cancel_queued(self, job_id: str) -> bool:
        """Fail a job that has not started; a running job cannot be interrupted."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', updated_at = ?, error = 'cancelled by operator'
                WHERE id = ? AND status = 'queued'
                """,
                (_now(), job_id),
            )
        if cursor.rowcount == 0:
            return False
        self.add_event(job_id, "job.failed", {"error": "cancelled by operator"})
        return True

    def list_active(self) -> list[dict]:
        """Queued and running jobs in start order, without their payloads."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, ticker, trade_date, status, created_at, mode
                FROM jobs
                WHERE status IN ('queued', 'running')
                ORDER BY created_at, rowid
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_job(
        self, ticker: str, trade_date: str, analysts: tuple[str, ...], mode: str = "live"
    ) -> dict:
        job_id = str(uuid4())
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, ticker, trade_date, analysts_json, status, created_at, updated_at, mode
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (job_id, ticker, trade_date, json.dumps(analysts), timestamp, timestamp, mode),
            )
        self.add_event(job_id, "job.queued", {"ticker": ticker, "trade_date": trade_date})
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["analysts"] = json.loads(value.pop("analysts_json"))
        result_json = value.pop("result_json")
        value["result"] = json.loads(result_json) if result_json else None
        return value

    def set_status(
        self,
        job_id: str,
        status: str,
        *,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = ?, result_json = ?, error = ?
                WHERE id = ?
                """,
                (
                    status,
                    _now(),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    job_id,
                ),
            )

    def add_event(self, job_id: str, event_type: str, payload: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events (job_id, created_at, event_type, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, _now(), event_type, json.dumps(payload, ensure_ascii=False)),
            )

    def events_after(self, job_id: str, sequence: int) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, created_at, event_type, payload_json
                FROM events
                WHERE job_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT 500
                """,
                (job_id, sequence),
            ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "created_at": row["created_at"],
                "type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]
