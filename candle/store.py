"""SQLite への保存。追加のDBサーバーは使わない。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .bars import JST, Bar
from .trade import Trade

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
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL, strategy_id TEXT NOT NULL, symbol TEXT NOT NULL,
    time TEXT NOT NULL, price REAL NOT NULL, pattern_low REAL NOT NULL,
    action TEXT NOT NULL             -- 'entry' または見送り理由
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL, strategy_id TEXT NOT NULL, symbol TEXT NOT NULL,
    signal_time TEXT NOT NULL, entry_time TEXT NOT NULL, entry_price REAL NOT NULL,
    qty INTEGER NOT NULL, initial_stop REAL NOT NULL, stop REAL NOT NULL, high REAL NOT NULL,
    exit_time TEXT, exit_price REAL, exit_reason TEXT
);
CREATE INDEX IF NOT EXISTS trades_mode ON trades(mode, strategy_id);
"""


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def save_bars(self, bars: list[Bar], source: str = "kabu_push",
                  replace: bool = True) -> None:
        """replace=False のときは既存の足（自前で受信した分）を上書きしない。"""
        if not bars:
            return
        self.conn.executemany(
            f"INSERT OR {'REPLACE' if replace else 'IGNORE'} INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(b.symbol, b.minutes, b.start.isoformat(), b.open, b.high, b.low, b.close,
              b.volume, b.ticks, int(b.complete), source) for b in bars],
        )
        self.conn.commit()

    def load_bars(self, symbol: str, minutes: int = 5, limit: int | None = None,
                  source: str | None = None) -> list[Bar]:
        sql = ("SELECT symbol, minutes, start, open, high, low, close, volume, ticks, complete "
               "FROM bars WHERE symbol=? AND minutes=?")
        args: list = [symbol, minutes]
        if source:
            sql += " AND source=?"
            args.append(source)
        sql += " ORDER BY start DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql, args).fetchall()
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

    def symbols_with_bars(self, minutes: int = 5) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT symbol FROM bars WHERE minutes=? ORDER BY symbol", (minutes,))]

    def add_signal(self, mode: str, strategy_id: str, symbol: str, time: datetime,
                   price: float, pattern_low: float, action: str) -> None:
        self.conn.execute(
            "INSERT INTO signals (mode,strategy_id,symbol,time,price,pattern_low,action) "
            "VALUES (?,?,?,?,?,?,?)",
            (mode, strategy_id, symbol, time.isoformat(), price, pattern_low, action))
        self.conn.commit()

    def recent_signals(self, mode: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT strategy_id,symbol,time,price,pattern_low,action FROM signals "
            "WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit)).fetchall()
        keys = ("strategy_id", "symbol", "time", "price", "pattern_low", "action")
        return [dict(zip(keys, r)) for r in rows]

    def save_trade(self, t: Trade) -> None:
        iso = lambda d: d.isoformat() if d else None
        vals = (t.mode, t.strategy_id, t.symbol, iso(t.signal_time), iso(t.entry_time),
                t.entry_price, t.qty, t.initial_stop, t.stop, t.high, iso(t.exit_time),
                t.exit_price, t.exit_reason)
        if t.id is None:
            cur = self.conn.execute(
                "INSERT INTO trades (mode,strategy_id,symbol,signal_time,entry_time,entry_price,"
                "qty,initial_stop,stop,high,exit_time,exit_price,exit_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
            t.id = cur.lastrowid
        else:
            self.conn.execute(
                "UPDATE trades SET mode=?,strategy_id=?,symbol=?,signal_time=?,entry_time=?,"
                "entry_price=?,qty=?,initial_stop=?,stop=?,high=?,exit_time=?,exit_price=?,"
                "exit_reason=? WHERE id=?", vals + (t.id,))
        self.conn.commit()

    def load_trades(self, mode: str, open_only: bool = False) -> list[Trade]:
        sql = ("SELECT id,mode,strategy_id,symbol,signal_time,entry_time,entry_price,qty,"
               "initial_stop,stop,high,exit_time,exit_price,exit_reason FROM trades WHERE mode=?")
        if open_only:
            sql += " AND exit_time IS NULL"
        dt = lambda v: datetime.fromisoformat(v) if v else None
        return [Trade(id=r[0], mode=r[1], strategy_id=r[2], symbol=r[3], signal_time=dt(r[4]),
                      entry_time=dt(r[5]), entry_price=r[6], qty=r[7], initial_stop=r[8],
                      stop=r[9], high=r[10], exit_time=dt(r[11]), exit_price=r[12],
                      exit_reason=r[13])
                for r in self.conn.execute(sql + " ORDER BY id", (mode,))]

    def delete_trades(self, mode: str) -> None:
        self.conn.execute("DELETE FROM trades WHERE mode=?", (mode,))
        self.conn.execute("DELETE FROM signals WHERE mode=?", (mode,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
