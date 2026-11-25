"""1件の取引（仮想・バックテスト・将来の実取引で共通）と損切り・トレーリングの規則。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


# 東証の呼値の単位（上限価格, 刻み）。10003 は TOPIX500 構成銘柄など細かい刻みの銘柄。
_TICKS = {
    "10000": [(3000, 1), (5000, 5), (30000, 10), (50000, 50), (300000, 100), (500000, 500),
              (3000000, 1000), (5000000, 5000), (30000000, 10000), (50000000, 50000)],
    "10003": [(1000, 0.1), (3000, 0.5), (10000, 1), (30000, 5), (100000, 10), (300000, 50),
              (1000000, 100), (3000000, 500), (10000000, 1000), (30000000, 5000)],
}


def tick_size(price: float, group: str = "10000") -> float:
    """その価格帯の呼値の単位。不明なグループは粗い刻み（どの銘柄でも有効な価格になる）で扱う。"""
    for limit, tick in _TICKS.get(str(group), _TICKS["10000"]):
        if price <= limit:
            return tick
    return 100000


def trigger_price(stop: float, group: str = "10000") -> float:
    """損切りライン stop を「下回ったら」に相当する逆指値のトリガ価格（stop 未満で最大の呼値）。"""
    tick = tick_size(stop, group)
    steps = math.ceil(round(stop / tick, 6))        # stop 以上で最小の呼値
    return round(steps * tick - tick, 1)


@dataclass
class Trade:
    mode: str                 # 'paper' | 'backtest' | 'live'
    strategy_id: str
    symbol: str
    signal_time: datetime
    entry_time: datetime
    entry_price: float
    qty: int
    initial_stop: float
    stop: float
    high: float = 0.0
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    id: int | None = None
    trail_mode: str = "r_step"    # r_step | percent | yen | none
    trail_value: float = 0.0
    pattern_low: float | None = None
    # --- 以下は実取引（live）の注文管理用 ---
    state: str = ""               # entering | open | replacing | exiting
    entry_order: str = ""
    stop_order: str = ""
    exit_order: str = ""
    server_stop: float | None = None   # 証券会社側に出ている逆指値のトリガ価格
    tick_group: str = "10000"
    exit_qty: int = 0             # 決済済みの株数（分割約定の途中経過）
    exit_value: float = 0.0       # 決済済みの約定代金
    holds: str = ""               # 買いの約定番号と株数（"E123:100,E124:300"）。返済する建玉の指定に使う

    def hold_list(self) -> list[tuple[str, int]]:
        return [(a, int(b)) for a, b in (x.split(":") for x in self.holds.split(",") if ":" in x)]

    @property
    def risk(self) -> float:
        """1株あたりの初期リスク（1R）。"""
        return self.entry_price - self.initial_stop

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    def trail(self, price: float) -> bool:
        """高値更新に応じて損切りを引き上げる。変更があれば True。固定の利確は置かない。

        r_step : +1R到達で建値、+2Rで+1R、+3Rで+2R…と1Rごとに追従する。
        percent: 高値から trail_value % 下に追従する。
        yen    : 高値から trail_value 円下に追従する。
        none   : 引き上げない（初期損切りのまま）。
        """
        self.high = max(self.high, price)
        new_stop = None
        if self.trail_mode == "r_step":
            n = int((self.high - self.entry_price) // self.risk) if self.risk > 0 else 0
            if n >= 1:
                new_stop = self.entry_price + (n - 1) * self.risk
        elif self.trail_mode == "percent" and self.trail_value > 0:
            new_stop = self.high * (1 - self.trail_value / 100)
        elif self.trail_mode == "yen" and self.trail_value > 0:
            new_stop = self.high - self.trail_value
        if new_stop is not None and new_stop > self.stop + 1e-9:
            self.stop = new_stop
            return True
        return False

    def next_raise(self) -> tuple[float, float] | None:
        """次に損切りラインが上がる株価と、そのときの新しい損切りライン（R方式のみ）。"""
        if self.trail_mode != "r_step" or self.risk <= 0:
            return None
        n = max(0, int((self.high - self.entry_price) // self.risk))
        return self.entry_price + (n + 1) * self.risk, self.entry_price + n * self.risk

    def close(self, time: datetime, price: float, reason: str) -> None:
        self.exit_time, self.exit_price, self.exit_reason = time, price, reason

    def pnl(self, price: float | None = None) -> float:
        p = self.exit_price if price is None else price
        return 0.0 if p is None else (p - self.entry_price) * self.qty

    def r_multiple(self, price: float | None = None) -> float:
        p = self.exit_price if price is None else price
        return 0.0 if p is None or self.risk <= 0 else (p - self.entry_price) / self.risk


def summarize(trades: list[Trade]) -> dict:
    """決済済み取引の成績（勝率・PF・期待損益）。"""
    done = [t for t in trades if not t.is_open]
    n = len(done)
    if n == 0:
        return dict(trades=0, win_rate=None, pf=None, expectancy=None, expectancy_r=None,
                    total=0.0, avg_hold_min=None)
    pnls = [t.pnl() for t in done]
    gain = sum(x for x in pnls if x > 0)
    loss = -sum(x for x in pnls if x < 0)
    hold = [(t.exit_time - t.entry_time).total_seconds() / 60 for t in done]
    return dict(
        trades=n,
        win_rate=sum(1 for x in pnls if x > 0) / n,
        pf=(gain / loss) if loss > 0 else None,
        expectancy=sum(pnls) / n,
        expectancy_r=sum(t.r_multiple() for t in done) / n,
        total=sum(pnls),
        avg_hold_min=sum(hold) / n,
    )
