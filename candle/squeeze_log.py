"""スクイーズ検知の記録と、その後の値動きの追跡。

検知したものは、上がったかどうかに関係なくすべて保存する。
保存するのは実際に受信した値だけで、受信できなかった時点は「欠損」として理由を残す。
騰落率などの計算値は保存せず、表示のときに保存済みの株価から計算する。
"""
from __future__ import annotations

import json
import queue
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .bars import JST
from .flow import FlowSignal, best_quotes

PRE_SECONDS = 60                       # 検知前に残す長さ
HORIZONS = (10, 30, 60, 180, 300)      # 検知後に株価を記録する時点（秒）
POST_SECONDS = HORIZONS[-1]

SCHEMA = """
CREATE TABLE IF NOT EXISTS squeeze_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, name TEXT, time TEXT NOT NULL,
    price REAL NOT NULL,            -- 検知時の株価（受信値）
    prev_close REAL, change REAL, change_pct REAL,   -- 前日終値・前日比（受信値。無ければ NULL）
    volume REAL,                    -- 当日の累積出来高（受信値）
    metrics TEXT NOT NULL,          -- 判定に使った数値（計算値）
    conditions TEXT NOT NULL,       -- 3条件それぞれの成否と理由
    board TEXT,                     -- 検知時の板（受信値。無ければ NULL）
    horizons TEXT NOT NULL,         -- 検知後の各時点の株価、または欠損の理由
    max_price REAL, min_price REAL, -- 追跡中に受信した約定値の最高・最安
    tracked_seconds REAL NOT NULL DEFAULT 0,   -- 途切れずに追跡できた秒数
    status TEXT NOT NULL,           -- tracking / done / incomplete
    gap_reason TEXT,                -- 追跡が途切れた理由
    tape TEXT                       -- APIの歩み値（取得できた場合）または取得できなかった理由
);
CREATE INDEX IF NOT EXISTS squeeze_events_time ON squeeze_events(time);
CREATE TABLE IF NOT EXISTS squeeze_ticks (
    event_id INTEGER NOT NULL, rel REAL NOT NULL,    -- 検知時刻からの秒数（負は検知前）
    price REAL, vol REAL, side INTEGER,              -- vol/side は配信の差分からの推定
    ask REAL, ask_qty REAL, bid REAL, bid_qty REAL   -- 最良気配（受信値）
);
CREATE INDEX IF NOT EXISTS squeeze_ticks_event ON squeeze_ticks(event_id, rel);
"""
BOARD_KEYS = [f"{side}{i}" for side in ("Sell", "Buy") for i in range(1, 11)]


@dataclass
class _Tick:
    t: datetime
    price: float | None
    vol: float
    side: int
    ask: float | None
    ask_qty: float | None
    bid: float | None
    bid_qty: float | None


@dataclass
class _Active:
    id: int
    symbol: str
    t0: datetime
    last_price: float
    last_trade_time: datetime
    max_price: float
    min_price: float
    horizons: dict = field(default_factory=dict)
    gap: str | None = None
    gap_at: float | None = None
    rows: list = field(default_factory=list)


def _board(msg: dict) -> str | None:
    levels = {}
    for k in BOARD_KEYS:
        v = msg.get(k)
        if isinstance(v, dict) and v.get("Price") is not None:
            levels[k] = {"Price": v.get("Price"), "Qty": v.get("Qty")}
    if not levels:
        return None
    extra = {k: msg.get(k) for k in ("MarketOrderSellQty", "MarketOrderBuyQty", "OverSellQty",
                                     "UnderBuyQty") if msg.get(k) is not None}
    return json.dumps({"levels": levels, **extra}, ensure_ascii=False)


class SqueezeLog:
    def __init__(self, store, tape_fetcher=None):
        self.conn = store.conn
        self.conn.executescript(SCHEMA)
        if "kind" not in {r[1] for r in self.conn.execute("PRAGMA table_info(squeeze_events)")}:
            # kind: 'squeeze'（板と約定の流れ） / 'wall'（大口の売り板）
            self.conn.execute("ALTER TABLE squeeze_events ADD COLUMN kind TEXT NOT NULL DEFAULT 'squeeze'")
        # 前回の実行中に追跡が終わらなかった分は、推測で埋めずに「未完了」として残す
        self.conn.execute(
            "UPDATE squeeze_events SET status='incomplete', "
            "gap_reason=COALESCE(gap_reason, 'プログラムが停止したため、以降は記録できていません') "
            "WHERE status='tracking'")
        self.conn.commit()
        self.recent: dict[str, deque] = defaultdict(deque)
        self.active: list[_Active] = []
        self.tape_fetcher = tape_fetcher       # symbol -> 歩み値の行（APIから取得）
        self._tape_results: queue.Queue = queue.Queue()

    # --- 配信1件ごと -----------------------------------------------------------
    def on_tick(self, symbol: str, msg: dict, now: datetime, vol: float, side: int) -> None:
        now = now.astimezone(JST)
        ask, ask_qty, bid = best_quotes(msg)
        bid_qty = (msg.get("Buy1") or {}).get("Qty") or msg.get("AskQty")
        price = msg.get("CurrentPrice")
        tick = _Tick(now, float(price) if price is not None else None, vol, side, ask, ask_qty,
                     bid, float(bid_qty) if bid_qty else None)
        buf = self.recent[symbol]
        buf.append(tick)
        cutoff = now - timedelta(seconds=PRE_SECONDS + 5)
        while buf and buf[0].t < cutoff:
            buf.popleft()
        for a in self.active:
            if a.symbol != symbol or a.gap:
                continue
            rel = (now - a.t0).total_seconds()
            if rel > POST_SECONDS:
                continue
            self._finalize_due(a, rel)      # この配信より前に過ぎた時点を、直前の株価で確定する
            a.rows.append(self._row(a.id, rel, tick))
            if tick.price is not None and vol > 0:
                a.last_price, a.last_trade_time = tick.price, now
                a.max_price, a.min_price = max(a.max_price, tick.price), min(a.min_price, tick.price)

    def on_signal(self, sig, msg: dict, name: str | None, kind: str = "squeeze") -> int:
        """検知を保存し、追跡を始める。上がるかどうかに関係なく必ず保存する。

        sig は symbol / time / price / metrics / conditions を持つもの（スクイーズ、売り板の出来事）。
        """
        t0 = sig.time.astimezone(JST)
        f = lambda k: float(msg[k]) if msg.get(k) is not None else None
        cur = self.conn.execute(
            "INSERT INTO squeeze_events (symbol,name,time,price,prev_close,change,change_pct,"
            "volume,metrics,conditions,board,horizons,max_price,min_price,status,kind) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'tracking',?)",
            (sig.symbol, name, t0.isoformat(), sig.price, f("PreviousClose"),
             f("ChangePreviousClose"), f("ChangePreviousClosePer"), f("TradingVolume"),
             json.dumps(sig.metrics, ensure_ascii=False),
             json.dumps(sig.conditions, ensure_ascii=False), _board(msg), "{}",
             sig.price, sig.price, kind))
        event_id = cur.lastrowid
        pre = [self._row(event_id, (t.t - t0).total_seconds(), t) for t in self.recent[sig.symbol]
               if (t0 - t.t).total_seconds() <= PRE_SECONDS]
        self.conn.executemany("INSERT INTO squeeze_ticks VALUES (?,?,?,?,?,?,?,?,?)", pre)
        self.conn.commit()
        self.active.append(_Active(event_id, sig.symbol, t0, sig.price, t0, sig.price, sig.price))
        return event_id

    @staticmethod
    def _row(event_id: int, rel: float, t: _Tick) -> tuple:
        return (event_id, round(rel, 3), t.price, t.vol, t.side, t.ask, t.ask_qty, t.bid, t.bid_qty)

    # --- 追跡 -------------------------------------------------------------------
    def _finalize_due(self, a: _Active, rel: float) -> None:
        for h in HORIZONS:
            if str(h) in a.horizons or rel < h:
                continue
            at = a.t0 + timedelta(seconds=h)
            # その時点までに受信した最後の約定値。age はその約定が何秒前のものか。
            a.horizons[str(h)] = {"price": a.last_price,
                                  "age": round(max(0.0, (at - a.last_trade_time).total_seconds()), 1)}

    def on_gap(self, reason: str, now: datetime) -> None:
        """受信が途切れた。以降の時点は欠損として扱う。"""
        now = now.astimezone(JST)
        for a in self.active:
            if not a.gap:
                a.gap, a.gap_at = reason, max(0.0, (now - a.t0).total_seconds())

    def on_clock(self, now: datetime) -> None:
        now = now.astimezone(JST)
        for a in list(self.active):
            rel = (now - a.t0).total_seconds()
            if not a.gap:
                self._finalize_due(a, rel)
            done = rel >= POST_SECONDS
            if a.gap or done:
                for h in HORIZONS:
                    if str(h) not in a.horizons:
                        a.horizons[str(h)] = {"missing": a.gap or "記録できていません"}
            if a.rows:
                self.conn.executemany("INSERT INTO squeeze_ticks VALUES (?,?,?,?,?,?,?,?,?)", a.rows)
                a.rows = []
            tracked = a.gap_at if a.gap else min(rel, POST_SECONDS)
            status = "incomplete" if a.gap else ("done" if done else "tracking")
            self.conn.execute(
                "UPDATE squeeze_events SET horizons=?, max_price=?, min_price=?, tracked_seconds=?, "
                "status=?, gap_reason=? WHERE id=?",
                (json.dumps(a.horizons, ensure_ascii=False), a.max_price, a.min_price,
                 round(max(0.0, tracked), 1), status, a.gap, a.id))
            if status != "tracking":
                self.active.remove(a)
                self._fetch_tape(a)
        self.conn.commit()
        self._save_tapes()

    # --- APIの歩み値（取得できる場合だけ） ---------------------------------------
    def _fetch_tape(self, a: _Active) -> None:
        if self.tape_fetcher is None:
            self._tape_results.put((a.id, {"missing": "この起動方法では歩み値APIを使っていません"}))
            return

        def work():
            try:
                rows = self.tape_fetcher(a.symbol)
            except Exception as e:
                self._tape_results.put((a.id, {"missing": f"歩み値APIの取得に失敗しました（{e}）"}))
                return
            lo, hi = a.t0 - timedelta(seconds=PRE_SECONDS), a.t0 + timedelta(seconds=POST_SECONDS)
            picked, unparsed = [], 0
            for r in rows or []:
                try:
                    t = datetime.fromisoformat(str(r.get("Time")))
                    t = t.replace(tzinfo=JST) if t.tzinfo is None else t.astimezone(JST)
                except ValueError:
                    unparsed += 1
                    continue
                if lo <= t <= hi:
                    picked.append({"rel": round((t - a.t0).total_seconds(), 3),
                                   "price": r.get("Price"), "volume": r.get("Volume")})
            if not rows:
                result = {"missing": "歩み値APIの応答が空でした"}
            elif not picked:
                result = {"missing": f"歩み値APIの応答 {len(rows)}件の中に、この時間帯のデータがありませんでした"
                          + (f"（時刻を読めなかった行 {unparsed}件）" if unparsed else "")}
            else:
                result = {"rows": sorted(picked, key=lambda x: x["rel"]), "source_rows": len(rows)}
            self._tape_results.put((a.id, result))

        threading.Thread(target=work, daemon=True).start()

    def _save_tapes(self) -> None:
        changed = False
        while True:
            try:
                event_id, result = self._tape_results.get_nowait()
            except queue.Empty:
                break
            self.conn.execute("UPDATE squeeze_events SET tape=? WHERE id=?",
                              (json.dumps(result, ensure_ascii=False), event_id))
            changed = True
        if changed:
            self.conn.commit()
