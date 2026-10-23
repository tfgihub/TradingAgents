"""Durable SQLite job and progress-event storage."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

MAX_BATCH_ATTEMPTS = 3
SUMMARY_LIMIT = 600


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
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    analysts_json TEXT NOT NULL,
                    total INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_results (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    job_id TEXT NOT NULL UNIQUE,
                    ticker TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    rating TEXT,
                    summary TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "mode" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN mode TEXT NOT NULL DEFAULT 'live'")
            if "batch_id" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN batch_id TEXT")
                connection.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            connection.execute("CREATE INDEX IF NOT EXISTS jobs_batch_idx ON jobs(batch_id, status)")
            self._recover_interrupted(connection)

    def _recover_interrupted(self, connection: sqlite3.Connection) -> None:
        """A live job that was mid-run cannot resume; a batch job is retried (once)."""
        interrupted = connection.execute(
            "SELECT id, batch_id, attempts FROM jobs WHERE status = 'running'"
        ).fetchall()
        for row in interrupted:
            if row["batch_id"] is not None and row["attempts"] < MAX_BATCH_ATTEMPTS - 1:
                connection.execute(
                    "UPDATE jobs SET status = 'queued', attempts = attempts + 1, updated_at = ? "
                    "WHERE id = ?",
                    (_now(), row["id"]),
                )
                continue
            connection.execute(
                "UPDATE jobs SET status = 'failed', updated_at = ?, error = ? WHERE id = ?",
                (_now(), "runner restarted before the analysis completed", row["id"]),
            )
            if row["batch_id"] is not None:
                self._append_batch_result(connection, row["id"])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def count_active(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running') AND mode = 'live'"
            ).fetchone()
        return int(row[0])

    def count_busy(self) -> int:
        """Queued and running jobs of every mode, including a backtest batch's."""
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

    def create_batch(
        self,
        batch_id: str,
        tickers: list[str],
        trade_dates: list[str],
        analysts: tuple[str, ...],
    ) -> dict:
        """Queue every ticker/date pair as a backtest job; repeating a batch ID is a no-op."""
        timestamp = _now()
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if exists is None:
                pairs = [(t, d) for d in sorted(trade_dates) for t in tickers]
                connection.execute(
                    "INSERT INTO batches (id, created_at, analysts_json, total) VALUES (?, ?, ?, ?)",
                    (batch_id, timestamp, json.dumps(analysts), len(pairs)),
                )
                connection.executemany(
                    """
                    INSERT INTO jobs (
                        id, ticker, trade_date, analysts_json, status, created_at, updated_at,
                        mode, batch_id
                    ) VALUES (?, ?, ?, ?, 'queued', ?, ?, 'backtest', ?)
                    """,
                    [
                        (str(uuid4()), t, d, json.dumps(analysts), timestamp, timestamp, batch_id)
                        for t, d in pairs
                    ],
                )
        status = self.batch_status(batch_id)
        assert status is not None
        return status

    def batch_status(self, batch_id: str) -> dict | None:
        with self._connect() as connection:
            batch = connection.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                return None
            counts = {
                row["status"]: row["n"]
                for row in connection.execute(
                    "SELECT status, count(*) AS n FROM jobs WHERE batch_id = ? GROUP BY status",
                    (batch_id,),
                )
            }
            last = connection.execute(
                "SELECT coalesce(max(seq), 0) FROM batch_results WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return {
            "id": batch_id,
            "total": batch["total"],
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
            "last_seq": int(last[0]),
        }

    def batch_results_after(self, batch_id: str, seq: int, limit: int = 200) -> list[dict]:
        """Finished batch jobs after a cursor: the recommendation dataset, oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT seq, job_id, ticker, trade_date, status, rating, summary, error
                FROM batch_results WHERE batch_id = ? AND seq > ? ORDER BY seq LIMIT ?
                """,
                (batch_id, seq, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def cancel_batch(self, batch_id: str) -> int:
        """Drop the batch's jobs that have not started; a running job finishes."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'failed', updated_at = ?, error = 'cancelled by operator'
                WHERE batch_id = ? AND status = 'queued'
                """,
                (_now(), batch_id),
            )
        return cursor.rowcount

    def _append_batch_result(self, connection: sqlite3.Connection, job_id: str) -> None:
        row = connection.execute(
            "SELECT id, batch_id, ticker, trade_date, status, result_json, error "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None or row["batch_id"] is None:
            return
        result = json.loads(row["result_json"]) if row["result_json"] else {}
        decision = (result.get("reports") or {}).get("final_trade_decision") or ""
        connection.execute(
            """
            INSERT OR IGNORE INTO batch_results (
                batch_id, job_id, ticker, trade_date, status, rating, summary, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["batch_id"],
                job_id,
                row["ticker"],
                row["trade_date"],
                row["status"],
                result.get("rating"),
                str(decision)[:SUMMARY_LIMIT] or None,
                row["error"],
                _now(),
            ),
        )

    def record_batch_result(self, job_id: str) -> None:
        """Publish a finished batch job to the dataset (a no-op for live jobs)."""
        with self._connect() as connection:
            self._append_batch_result(connection, job_id)

    def list_active(self) -> list[dict]:
        """Queued and running jobs in start order, without their payloads."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, ticker, trade_date, status, created_at, mode
                FROM jobs
                WHERE status IN ('queued', 'running') AND mode = 'live'
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
