"""Per-job SQLite storage: job metadata, input rows and row results.

One database file per job keeps jobs isolated and lets batches be read without
holding the whole workbook in memory. Connections are opened per operation so
the store is safe to use from the worker thread and the UI thread.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .excel_io import InputRow
from .matcher import MatchResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    total_rows INTEGER,
    processed_rows INTEGER NOT NULL DEFAULT 0,
    ingested INTEGER NOT NULL DEFAULT 0,
    sheet_name TEXT,
    error_message TEXT,
    metrics_json TEXT
);
CREATE TABLE IF NOT EXISTS input_rows (
    row_index INTEGER PRIMARY KEY,
    excel_row INTEGER NOT NULL,
    address_a TEXT,
    address_b TEXT
);
CREATE TABLE IF NOT EXISTS results (
    row_index INTEGER PRIMARY KEY,
    normalized_a TEXT,
    normalized_b TEXT,
    decision TEXT NOT NULL,
    score INTEGER,
    explanation TEXT NOT NULL,
    result_text TEXT NOT NULL,
    details_json TEXT,
    error TEXT
);
"""

STATUSES = ("queued", "running", "completed", "failed")


@dataclass(frozen=True)
class JobMeta:
    id: str
    filename: str
    file_hash: str
    status: str
    created_at: float
    updated_at: float
    total_rows: int | None
    processed_rows: int
    ingested: bool
    sheet_name: str | None
    error_message: str | None
    metrics: dict[str, Any]

    @property
    def progress(self) -> float:
        if not self.total_rows:
            return 1.0 if self.status == "completed" else 0.0
        return min(1.0, self.processed_rows / self.total_rows)


class JobStore:
    def __init__(self, job_dir: Path) -> None:
        self.job_dir = Path(job_dir)
        self.db_path = self.job_dir / "job.db"
        self.input_path = self.job_dir / "input.xlsx"
        self.output_path = self.job_dir / "output.xlsx"

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- job metadata -------------------------------------------------
    def create(self, job_id: str, filename: str, file_hash: str) -> None:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        with self._conn() as c:
            c.executescript(SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute(
                "INSERT INTO job (id, filename, file_hash, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (job_id, filename, file_hash, "queued", now, now),
            )

    def exists(self) -> bool:
        return self.db_path.exists()

    def meta(self) -> JobMeta:
        with self._conn() as c:
            r = c.execute("SELECT * FROM job").fetchone()
        if r is None:
            raise KeyError(f"No job metadata in {self.job_dir.name}")
        return JobMeta(
            id=r["id"], filename=r["filename"], file_hash=r["file_hash"], status=r["status"],
            created_at=r["created_at"], updated_at=r["updated_at"], total_rows=r["total_rows"],
            processed_rows=r["processed_rows"], ingested=bool(r["ingested"]), sheet_name=r["sheet_name"],
            error_message=r["error_message"],
            metrics=json.loads(r["metrics_json"]) if r["metrics_json"] else {},
        )

    def update(self, **fields: Any) -> None:
        allowed = {"status", "total_rows", "processed_rows", "ingested", "sheet_name", "error_message", "metrics_json"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown job fields: {unknown}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"Bad status {fields['status']}")
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE job SET {cols}", tuple(fields.values()))

    # ---- input rows ---------------------------------------------------
    def ingest(self, rows: Iterable[InputRow], chunk: int = 1000) -> int:
        """Store input rows (replacing any partial earlier ingest)."""
        n = 0
        with self._conn() as c:
            c.execute("DELETE FROM input_rows")
            c.execute("DELETE FROM results")
            buf: list[tuple[Any, ...]] = []
            for r in rows:
                buf.append((r.row_index, r.excel_row, r.address_a, r.address_b))
                n += 1
                if len(buf) >= chunk:
                    c.executemany("INSERT INTO input_rows VALUES (?,?,?,?)", buf)
                    buf.clear()
            if buf:
                c.executemany("INSERT INTO input_rows VALUES (?,?,?,?)", buf)
        return n

    def fetch_batch(self, start: int, size: int) -> list[InputRow]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM input_rows WHERE row_index >= ? ORDER BY row_index LIMIT ?", (start, size)
            ).fetchall()
        return [InputRow(r["row_index"], r["excel_row"], r["address_a"], r["address_b"]) for r in rows]

    # ---- results ------------------------------------------------------
    def save_results(self, items: list[tuple[int, MatchResult]]) -> None:
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (idx, m.normalized_a, m.normalized_b, m.decision, m.score, m.explanation,
                     m.result_text, m.details_json(), m.error)
                    for idx, m in items
                ],
            )

    def result_count(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM results").fetchone()[0])

    def iter_output_rows(self, decision: str | None = None, row_indices: list[int] | None = None) -> Iterator[tuple[str | None, ...]]:
        """Five output columns in input order. Inner join: every input row must have a result."""
        sql = (
            "SELECT i.row_index AS idx, i.address_a, i.address_b, r.normalized_a, r.normalized_b, "
            "r.result_text FROM input_rows i JOIN results r ON r.row_index = i.row_index"
        )
        params: list[Any] = []
        if decision:
            sql += " WHERE r.decision = ?"
            params.append(decision)
        sql += " ORDER BY i.row_index"
        wanted = set(row_indices) if row_indices is not None else None
        with self._conn() as c:
            cur = c.execute(sql, params)
            for r in cur:
                if wanted is not None and r["idx"] not in wanted:
                    continue
                yield (r["address_a"], r["address_b"], r["normalized_a"], r["normalized_b"], r["result_text"])

    def results_frame(self) -> pd.DataFrame:
        """All rows joined with structured details, for summaries and follow-up questions."""
        with self._conn() as c:
            df = pd.read_sql_query(
                "SELECT i.row_index, i.excel_row, i.address_a AS AddressA, i.address_b AS AddressB, "
                "r.normalized_a AS normalized_AddressA, r.normalized_b AS normalized_AddressB, "
                "r.decision, r.score, r.explanation, r.result_text AS Result, r.details_json, r.error "
                "FROM input_rows i JOIN results r ON r.row_index = i.row_index ORDER BY i.row_index",
                c,
            )
        details = df["details_json"].map(lambda s: json.loads(s) if s else {})
        df["reason_codes"] = details.map(lambda d: d.get("reason_codes", []))
        df["conflicts"] = details.map(lambda d: d.get("conflicts", []))
        df["missing_one_side"] = details.map(lambda d: d.get("missing_one_side", []))
        df["status_a"] = details.map(lambda d: d.get("status_a", ""))
        df["status_b"] = details.map(lambda d: d.get("status_b", ""))
        return df.drop(columns=["details_json"])

    def decision_counts(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT decision, COUNT(*) FROM results GROUP BY decision").fetchall()
        counts = {"MATCH": 0, "NON-MATCH": 0, "REVIEW": 0}
        counts.update({r[0]: int(r[1]) for r in rows})
        return counts
