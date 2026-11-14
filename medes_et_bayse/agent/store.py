"""SQLite log of cycles, predictions and trades (also the SLM's live track record)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, status TEXT, summary TEXT, stats_json TEXT);
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, created_at TEXT, event_id TEXT, market_id TEXT,
  title TEXT, category TEXT, yes_price REAL, p_slm REAL, p_llm REAL, p_yes REAL, source TEXT,
  hours_to_close REAL, decision TEXT, reason TEXT, resolved_outcome INTEGER);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, created_at TEXT, event_id TEXT, market_id TEXT,
  outcome_id TEXT, outcome TEXT, title TEXT, price REAL, effective_price REAL, p_win REAL, net_edge REAL,
  kelly_fraction REAL, stake REAL, currency TEXT, dry_run INTEGER, status TEXT, response_json TEXT,
  settled INTEGER DEFAULT 0, pnl REAL);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, created_at TEXT, category TEXT, note TEXT);
"""


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


class AgentStore:
    def __init__(self, path: str | Path = "data/medes.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def start_cycle(self) -> int:
        return int(self._exec("INSERT INTO cycles(started_at,status) VALUES(?,?)", (_iso(), "running")).lastrowid)

    def finish_cycle(self, cycle_id: int, status: str, summary: str, stats: dict) -> None:
        self._exec("UPDATE cycles SET finished_at=?,status=?,summary=?,stats_json=? WHERE id=?",
                   (_iso(), status, summary, json.dumps(stats, default=str), cycle_id))

    def log_prediction(self, cycle_id: int, **row: Any) -> None:
        cols = ["cycle_id", "created_at"] + list(row)
        self._exec(f"INSERT INTO predictions({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                   tuple([cycle_id, _iso()] + list(row.values())))

    def log_trade(self, cycle_id: int, **row: Any) -> int:
        cols = ["cycle_id", "created_at"] + list(row)
        return int(self._exec(f"INSERT INTO trades({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                              tuple([cycle_id, _iso()] + list(row.values()))).lastrowid)

    def note(self, cycle_id: int, category: str, note: str) -> None:
        self._exec("INSERT INTO notes(cycle_id,created_at,category,note) VALUES(?,?,?,?)", (cycle_id, _iso(), category, note))

    def _day(self, now: datetime) -> str:
        return now.astimezone(timezone.utc).date().isoformat()

    def stakes_on_day(self, now: datetime) -> float:
        row = self._exec("SELECT COALESCE(SUM(stake),0) s FROM trades WHERE substr(created_at,1,10)=? AND status IN ('dry_run','submitted','filled')",
                         (self._day(now),)).fetchone()
        return float(row["s"])

    def realized_pnl_on_day(self, now: datetime) -> float:
        row = self._exec("SELECT COALESCE(SUM(pnl),0) s FROM trades WHERE settled=1 AND substr(created_at,1,10)=?", (self._day(now),)).fetchone()
        return float(row["s"])

    def has_open_position(self, market_id: str) -> bool:
        row = self._exec("SELECT 1 FROM trades WHERE market_id=? AND settled=0 AND status IN ('dry_run','submitted','filled') LIMIT 1", (market_id,)).fetchone()
        return row is not None

    def open_positions(self) -> int:
        return int(self._exec("SELECT COUNT(*) c FROM trades WHERE settled=0 AND status IN ('dry_run','submitted','filled')").fetchone()["c"])

    def recent_trades(self, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self._exec("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def last_cycle(self) -> Optional[dict]:
        row = self._exec("SELECT * FROM cycles ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
