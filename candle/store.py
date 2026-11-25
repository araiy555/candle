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
        # 旧版で作ったDBに、後から増えた列を足す
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)")}
        for col, decl in (("trail_mode", "TEXT NOT NULL DEFAULT 'r_step'"),
                          ("trail_value", "REAL NOT NULL DEFAULT 0"), ("pattern_low", "REAL"),
                          ("state", "TEXT NOT NULL DEFAULT ''"),
                          ("entry_order", "TEXT NOT NULL DEFAULT ''"),
                          ("stop_order", "TEXT NOT NULL DEFAULT ''"),
                          ("exit_order", "TEXT NOT NULL DEFAULT ''"), ("server_stop", "REAL"),
                          ("tick_group", "TEXT NOT NULL DEFAULT '10000'"),
                          ("exit_qty", "INTEGER NOT NULL DEFAULT 0"),
                          ("exit_value", "REAL NOT NULL DEFAULT 0"),
                          ("holds", "TEXT NOT NULL DEFAULT ''")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {decl}")
        if "note" not in {r[1] for r in self.conn.execute("PRAGMA table_info(signals)")}:
            self.conn.execute("ALTER TABLE signals ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        self.conn.commit()

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

    def known_names(self) -> dict[str, str]:
        """これまでに監視した銘柄の名前（新しい日付のものを優先）。"""
        rows = self.conn.execute(
            "SELECT symbol, name FROM watchlist WHERE name IS NOT NULL ORDER BY day").fetchall()
        return {r[0]: r[1] for r in rows}

    def open_symbols(self) -> list[str]:
        """保有中（テスト・本番とも）の銘柄。"""
        rows = self.conn.execute(
            "SELECT DISTINCT symbol FROM trades WHERE exit_time IS NULL "
            "AND mode IN ('paper','live') ORDER BY symbol").fetchall()
        return [r[0] for r in rows]

    def symbols_with_bars(self, minutes: int = 5) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT symbol FROM bars WHERE minutes=? ORDER BY symbol", (minutes,))]

    def add_signal(self, mode: str, strategy_id: str, symbol: str, time: datetime,
                   price: float, pattern_low: float, action: str, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO signals (mode,strategy_id,symbol,time,price,pattern_low,action,note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (mode, strategy_id, symbol, time.isoformat(), price, pattern_low, action, note))
        self.conn.commit()

    def recent_signals(self, mode: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT strategy_id,symbol,time,price,pattern_low,action,note FROM signals "
            "WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit)).fetchall()
        keys = ("strategy_id", "symbol", "time", "price", "pattern_low", "action", "note")
        return [dict(zip(keys, r)) for r in rows]

    _TRADE_COLS = ("mode", "strategy_id", "symbol", "signal_time", "entry_time", "entry_price",
                   "qty", "initial_stop", "stop", "high", "exit_time", "exit_price", "exit_reason",
                   "trail_mode", "trail_value", "pattern_low", "state", "entry_order",
                   "stop_order", "exit_order", "server_stop", "tick_group", "exit_qty",
                   "exit_value", "holds")
    _TIME_COLS = ("signal_time", "entry_time", "exit_time")

    def save_trade(self, t: Trade) -> None:
        vals = tuple((getattr(t, c).isoformat() if getattr(t, c) else None)
                     if c in self._TIME_COLS else getattr(t, c) for c in self._TRADE_COLS)
        if t.id is None:
            cur = self.conn.execute(
                f"INSERT INTO trades ({','.join(self._TRADE_COLS)}) "
                f"VALUES ({','.join('?' * len(self._TRADE_COLS))})", vals)
            t.id = cur.lastrowid
        else:
            self.conn.execute(
                f"UPDATE trades SET {','.join(c + '=?' for c in self._TRADE_COLS)} WHERE id=?",
                vals + (t.id,))
        self.conn.commit()

    def load_trades(self, mode: str, open_only: bool = False) -> list[Trade]:
        sql = f"SELECT id,{','.join(self._TRADE_COLS)} FROM trades WHERE mode=?"
        if open_only:
            sql += " AND exit_time IS NULL"
        out = []
        for r in self.conn.execute(sql + " ORDER BY id", (mode,)):
            kw = dict(zip(self._TRADE_COLS, r[1:]))
            for c in self._TIME_COLS:
                kw[c] = datetime.fromisoformat(kw[c]) if kw[c] else None
            out.append(Trade(id=r[0], **kw))
        return out

    def delete_trade(self, trade_id: int) -> None:
        self.conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
        self.conn.commit()

    def delete_trades(self, mode: str) -> None:
        self.conn.execute("DELETE FROM trades WHERE mode=?", (mode,))
        self.conn.execute("DELETE FROM signals WHERE mode=?", (mode,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
