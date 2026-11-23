"""SQLite への保存。追加のDBサーバーは使わない。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .bars import JST, Bar

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol   TEXT NOT NULL,
    minutes  INTEGER NOT NULL,
    start    TEXT NOT NULL,      -- 足の開始時刻（JST, ISO8601）
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume   REAL NOT NULL,
    ticks    INTEGER NOT NULL,
    complete INTEGER NOT NULL,   -- 0: 途中参加・切断を含む足（検出に使わない）
    source   TEXT NOT NULL DEFAULT 'kabu_push',
    PRIMARY KEY (symbol, minutes, start)
);
CREATE TABLE IF NOT EXISTS watchlist (
    day      TEXT NOT NULL,
    rank     INTEGER NOT NULL,
    symbol   TEXT NOT NULL,
    name     TEXT,
    turnover REAL,               -- 売買代金（百万円）
    PRIMARY KEY (day, symbol)
);
"""


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def save_bars(self, bars: list[Bar], source: str = "kabu_push") -> None:
        if not bars:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(b.symbol, b.minutes, b.start.isoformat(), b.open, b.high, b.low, b.close,
              b.volume, b.ticks, int(b.complete), source) for b in bars],
        )
        self.conn.commit()

    def load_bars(self, symbol: str, minutes: int = 5, limit: int | None = None) -> list[Bar]:
        sql = ("SELECT symbol, minutes, start, open, high, low, close, volume, ticks, complete "
               "FROM bars WHERE symbol=? AND minutes=? ORDER BY start DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql, (symbol, minutes)).fetchall()
        return [Bar(r[0], datetime.fromisoformat(r[2]).astimezone(JST), r[1], r[3], r[4], r[5],
                    r[6], r[7], r[8], bool(r[9])) for r in reversed(rows)]

    def save_watchlist(self, day: str, rows: list[dict]) -> None:
        self.conn.execute("DELETE FROM watchlist WHERE day=?", (day,))
        self.conn.executemany(
            "INSERT INTO watchlist VALUES (?,?,?,?,?)",
            [(day, r["rank"], r["symbol"], r.get("name"), r.get("turnover")) for r in rows],
        )
        self.conn.commit()

    def latest_watchlist(self) -> tuple[str, list[dict]] | None:
        row = self.conn.execute("SELECT MAX(day) FROM watchlist").fetchone()
        if not row or not row[0]:
            return None
        rows = self.conn.execute(
            "SELECT rank, symbol, name, turnover FROM watchlist WHERE day=? ORDER BY rank",
            (row[0],)).fetchall()
        return row[0], [dict(rank=r[0], symbol=r[1], name=r[2], turnover=r[3]) for r in rows]

    def close(self) -> None:
        self.conn.close()
