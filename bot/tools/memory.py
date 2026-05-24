"""SQLite memory tool for PyFlue agent.

Provides the agent with persistent memory for:
  - Logging trades (trades table)
  - Storing market research (market_research table)
  - Recording agent reasoning notes (agent_notes table)

Database path is configurable via DATABASE_PATH env var,
defaulting to /data/medes.db for Render persistent disk.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

DEFAULT_DB_PATH = os.getenv("DATABASE_PATH", "/data/medes.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    event_id TEXT,
    event_title TEXT,
    side TEXT NOT NULL,
    outcome TEXT,
    price REAL,
    amount REAL,
    currency TEXT DEFAULT 'USD',
    edge REAL,
    kelly_fraction REAL,
    strategy TEXT,
    dry_run INTEGER DEFAULT 1,
    status TEXT DEFAULT 'pending',
    result_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    source TEXT,
    title TEXT,
    url TEXT,
    summary TEXT NOT NULL,
    market_id TEXT,
    event_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT,
    category TEXT DEFAULT 'general',
    note TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);
"""

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_db(db_path: Optional[str] = None) -> None:
    """Create tables if they don't exist."""
    with _lock:
        conn = _get_connection(db_path)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
            logger.info(f"Memory DB initialized at {db_path or DEFAULT_DB_PATH}")
        finally:
            conn.close()


async def log_trade(
    market_id: str,
    side: str,
    amount: float,
    *,
    event_id: str = "",
    event_title: str = "",
    outcome: str = "",
    price: float = 0.0,
    currency: str = "USD",
    edge: float = 0.0,
    kelly_fraction: float = 0.0,
    strategy: str = "",
    dry_run: bool = True,
    status: str = "pending",
    result_json: str = "",
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Log a trade to the trades table."""
    with _lock:
        conn = _get_connection(db_path)
        try:
            conn.execute(
                """INSERT INTO trades
                   (market_id, event_id, event_title, side, outcome, price, amount,
                    currency, edge, kelly_fraction, strategy, dry_run, status, result_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (market_id, event_id, event_title, side, outcome, price, amount,
                 currency, edge, kelly_fraction, strategy, int(dry_run), status, result_json, _now()),
            )
            conn.commit()
            trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            logger.info(f"Trade #{trade_id} logged: {side} {market_id} ${amount}")
            return {"trade_id": trade_id, "status": "logged"}
        finally:
            conn.close()


async def log_research(
    query: str,
    summary: str,
    *,
    source: str = "tavily",
    title: str = "",
    url: str = "",
    market_id: str = "",
    event_id: str = "",
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Store market research results."""
    with _lock:
        conn = _get_connection(db_path)
        try:
            conn.execute(
                """INSERT INTO market_research
                   (query, source, title, url, summary, market_id, event_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (query, source, title, url, summary, market_id, event_id, _now()),
            )
            conn.commit()
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            return {"research_id": row_id, "status": "logged"}
        finally:
            conn.close()


async def log_note(
    note: str,
    *,
    cycle_id: str = "",
    category: str = "general",
    metadata: Optional[dict] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Store an agent reasoning note."""
    meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else ""
    with _lock:
        conn = _get_connection(db_path)
        try:
            conn.execute(
                """INSERT INTO agent_notes
                   (cycle_id, category, note, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (cycle_id, category, note, meta_json, _now()),
            )
            conn.commit()
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            return {"note_id": row_id, "status": "logged"}
        finally:
            conn.close()


async def query_memory(
    table: str,
    limit: int = 10,
    where: str = "",
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Read from memory tables. Tables: trades, market_research, agent_notes."""
    allowed = {"trades", "market_research", "agent_notes"}
    if table not in allowed:
        return [{"error": f"Invalid table. Allowed: {', '.join(allowed)}"}]
    query = f"SELECT * FROM {table}"
    if where:
        query += f" WHERE {where}"
    query += f" ORDER BY id DESC LIMIT {int(limit)}"
    with _lock:
        conn = _get_connection(db_path)
        try:
            rows = conn.execute(query).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            return [{"error": str(exc)}]
        finally:
            conn.close()
