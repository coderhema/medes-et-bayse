from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid
from typing import Any, Optional


DEFAULT_DB_PATH = Path(os.getenv("HERMES_DB_PATH", "tmp/hermes_agent.sqlite3"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    summary TEXT,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS agent_memory (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE TABLE IF NOT EXISTS agent_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class HermesMemoryEntry:
    namespace: str
    key: str
    value: str
    source: str
    run_id: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class HermesLogEntry:
    id: int
    run_id: Optional[str]
    level: str
    category: str
    message: str
    payload: Optional[str]
    created_at: str


class HermesDatabase:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or DEFAULT_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(SCHEMA)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _json(payload: Any) -> Optional[str]:
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False, default=str)

    def start_run(self, *, metadata: Optional[dict[str, Any]] = None) -> str:
        run_id = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs (run_id, started_at, status, summary, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, self._now(), "running", None, self._json(metadata)),
            )
        return run_id

    def finish_run(self, run_id: str, *, status: str = "completed", summary: Optional[str] = None, metadata: Optional[dict[str, Any]] = None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET finished_at = ?, status = ?, summary = COALESCE(?, summary), metadata = COALESCE(?, metadata)
                WHERE run_id = ?
                """,
                (self._now(), status, summary, self._json(metadata), run_id),
            )

    def log_event(self, category: str, message: str, *, level: str = "info", payload: Any = None, run_id: Optional[str] = None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_logs (run_id, level, category, message, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, level, category, message, self._json(payload), self._now()),
            )

    def remember(self, namespace: str, key: str, value: Any, *, source: str = "agent", run_id: Optional[str] = None) -> None:
        serialized = self._json(value)
        if serialized is None:
            serialized = "null"
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_memory (namespace, key, value, source, run_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    value = excluded.value,
                    source = excluded.source,
                    run_id = excluded.run_id,
                    updated_at = excluded.updated_at
                """,
                (namespace, key, serialized, source, run_id, now, now),
            )

    def recall(self, namespace: str, key: Optional[str] = None) -> list[HermesMemoryEntry]:
        query = "SELECT namespace, key, value, source, run_id, created_at, updated_at FROM agent_memory WHERE namespace = ?"
        params: list[Any] = [namespace]
        if key is not None:
            query += " AND key = ?"
            params.append(key)
        query += " ORDER BY updated_at DESC"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [HermesMemoryEntry(**dict(row)) for row in rows]

    def recent_logs(self, *, limit: int = 20, category: Optional[str] = None) -> list[HermesLogEntry]:
        query = "SELECT id, run_id, level, category, message, payload, created_at FROM agent_logs"
        params: list[Any] = []
        if category is not None:
            query += " WHERE category = ?"
            params.append(category)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [HermesLogEntry(**dict(row)) for row in rows]

    def recent_memory(self, *, namespace: Optional[str] = None, limit: int = 20) -> list[HermesMemoryEntry]:
        query = "SELECT namespace, key, value, source, run_id, created_at, updated_at FROM agent_memory"
        params: list[Any] = []
        if namespace is not None:
            query += " WHERE namespace = ?"
            params.append(namespace)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [HermesMemoryEntry(**dict(row)) for row in rows]
