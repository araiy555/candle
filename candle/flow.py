"""板と約定の流れから、急騰前の需給の変化（スクイーズ）を検出する。

ローソク足のパターンとは独立したシグナル。足の確定を待たず、配信を受けるたびに判定する。

見ているもの（直近 window 秒）:
- 売り板の吸収   : 売り気配にぶつけた買いの量が、出ていた最良売り気配の数量の何倍か。
                   それだけ買われても売り気配が下がっていないこと。
- 買い約定の連続 : 買い方向の約定が続いている回数、買い約定の比率。
- 売り圧力への耐性: まとまった売りが出たのに、株価と買い気配が下がっていないこと。

約定の向きは配信からの推定（直前の最良気配と約定値の位置関係）。kabuステーションの配信は
すべての約定を1件ずつ届けるとは限らないため、厳密な歩み値ではない。しきい値は初期値で、
実データでの検証はこれから。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .bars import JST, SESSIONS


@dataclass
class FlowParams:
    window: float = 60.0         # 見る時間の長さ（秒）
    min_trades: int = 6          # この数以上の約定の更新があること
    surge: float = 2.0           # 出来高が、その銘柄の当日平均ペースの何倍以上か
    absorb: float = 3.0          # 買い約定の量が、最良売り気配の平均数量の何倍以上か
    run: int = 5                 # 買い約定がこの回数以上続く
    buy_ratio: float = 0.7       # または、買い約定の比率がこれ以上
    sell_share: float = 0.25     # 耐性: 売り約定がこの比率以上あったのに下がらない
    max_rise: float = 0.004      # すでにこれ以上（期間の安値比）上がっていたら対象外（急騰「前」を狙う）
    cooldown: float = 300.0      # 同じ銘柄の再通知までの時間（秒）


@dataclass
class FlowSignal:
    symbol: str
    time: datetime
    price: float
    low: float                   # 期間の安値（損切りの基準に使える）
    reasons: list[str]
    note: str


@dataclass
class _Event:
    t: datetime
    price: float
    vol: float
    side: int                    # +1 買い方向, -1 売り方向, 0 不明
    ask: float | None
    ask_qty: float | None
    bid: float | None


@dataclass
class _State:
    day: object = None
    cum: float | None = None
    last_price: float | None = None
    last_side: int = 0
    ask: float | None = None
    ask_qty: float | None = None
    bid: float | None = None
    events: deque = field(default_factory=deque)
    last_signal: datetime | None = None


def best_quotes(msg: dict) -> tuple[float | None, float | None, float | None]:
    """配信から (最良売り気配, その数量, 最良買い気配) を取り出す。

    kabuステーションAPIでは BidPrice が最良「売り」気配、AskPrice が最良「買い」気配
    （一般的な呼び方と逆）なので注意。
    """
    sell1, buy1 = msg.get("Sell1") or {}, msg.get("Buy1") or {}
    ask = sell1.get("Price") or msg.get("BidPrice")
    ask_qty = sell1.get("Qty") or msg.get("BidQty")
    bid = buy1.get("Price") or msg.get("AskPrice")
    return (float(ask) if ask else None, float(ask_qty) if ask_qty else None,
            float(bid) if bid else None)


def session_seconds(now: datetime) -> float:
    """その日の立会が始まってから now までの、立会時間の合計（秒）。"""
    now = now.astimezone(JST)
    total = 0.0
    for start, end in SESSIONS:
        s = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        e = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        if now > s:
            total += (min(now, e) - s).total_seconds()
    return total


class FlowDetector:
    def __init__(self, params: FlowParams | None = None):
        self.p = params or FlowParams()
        self._states: dict[str, _State] = {}

    def update(self, symbol: str, msg: dict, now: datetime) -> FlowSignal | None:
        """配信1件を取り込み、条件がそろえばシグナルを返す。"""
        price, cum = msg.get("CurrentPrice"), msg.get("TradingVolume")
        ask, ask_qty, bid = best_quotes(msg)
        st = self._states.setdefault(symbol, _State())
        now = now.astimezone(JST)
        if st.day != now.date():
            st.__init__()
            st.day = now.date()
        if price is None or cum is None:
            st.ask, st.ask_qty, st.bid = ask, ask_qty, bid
            return None
        price, cum = float(price), float(cum)
        vol = cum - st.cum if st.cum is not None else 0.0
        if st.cum is not None and vol < 0:
            st.events.clear()
            vol = 0.0
        if vol > 0:
            # 約定の向き: 直前の最良売り気配以上で付けば買い、最良買い気配以下で付けば売り
            if st.ask is not None and price >= st.ask:
                side = 1
            elif st.bid is not None and price <= st.bid:
                side = -1
            elif st.last_price is not None and price != st.last_price:
                side = 1 if price > st.last_price else -1
            else:
                side = st.last_side
            st.events.append(_Event(now, price, vol, side, st.ask, st.ask_qty, st.bid))
            st.last_side = side
        st.cum, st.last_price = cum, price
        st.ask, st.ask_qty, st.bid = ask, ask_qty, bid
        cutoff = now - timedelta(seconds=self.p.window)
        while st.events and st.events[0].t < cutoff:
            st.events.popleft()
        if vol <= 0:
            return None
        return self._judge(symbol, st, cum, now)

    def _judge(self, symbol: str, st: _State, cum: float, now: datetime) -> FlowSignal | None:
        p, ev = self.p, st.events
        if len(ev) < p.min_trades:
            return None
        if st.last_signal and (now - st.last_signal).total_seconds() < p.cooldown:
            return None
        elapsed = session_seconds(now)
        if elapsed < p.window * 5:        # 寄り付き直後は平均ペースが定まらない
            return None
        total = sum(e.vol for e in ev)
        buy = sum(e.vol for e in ev if e.side > 0)
        sell = sum(e.vol for e in ev if e.side < 0)
        expected = (cum - total) / max(elapsed - p.window, 1.0) * p.window
        surge = total / expected if expected > 0 else 0.0
        first, last = ev[0], ev[-1]
        low = min(e.price for e in ev)
        if surge < p.surge or last.price < first.price:
            return None
        if low > 0 and (last.price - low) / low > p.max_rise:
            return None                   # すでに上がりすぎ

        reasons = []
        asks = [e.ask_qty for e in ev if e.ask_qty]
        avg_ask = sum(asks) / len(asks) if asks else 0.0
        absorbed = buy / avg_ask if avg_ask > 0 else 0.0
        ask_held = first.ask is None or st.ask is None or st.ask >= first.ask
        if absorbed >= p.absorb and ask_held:
            reasons.append(f"売り板の吸収 {absorbed:.1f}倍")

        run = 0
        for e in reversed(ev):
            if e.side <= 0:
                break
            run += 1
        ratio = buy / total if total > 0 else 0.0
        if run >= p.run or ratio >= p.buy_ratio:
            reasons.append(f"買い約定の連続 {run}回・買い比率 {ratio:.0%}")

        bid_held = first.bid is None or st.bid is None or st.bid >= first.bid
        if total > 0 and sell / total >= p.sell_share and bid_held:
            reasons.append(f"売り圧力への耐性（売り {sell / total:.0%} でも下げず）")

        if len(reasons) < 2:
            return None
        st.last_signal = now
        note = "、".join(reasons) + f"、出来高 通常の{surge:.1f}倍"
        return FlowSignal(symbol, now, last.price, low, reasons, note)
