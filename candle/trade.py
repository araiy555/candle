"""1件の取引（仮想・バックテスト・将来の実取引で共通）と損切り・トレーリングの規則。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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

    @property
    def risk(self) -> float:
        """1株あたりの初期リスク（1R）。"""
        return self.entry_price - self.initial_stop

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    def trail(self, price: float) -> bool:
        """高値更新に応じて損切りを引き上げる。変更があれば True。

        +1R到達で建値、+2Rで+1R、+3Rで+2R…と、以降も1Rごとに追従する。
        固定の利確は置かない。
        """
        self.high = max(self.high, price)
        n = int((self.high - self.entry_price) // self.risk) if self.risk > 0 else 0
        if n >= 1:
            new_stop = self.entry_price + (n - 1) * self.risk
            if new_stop > self.stop:
                self.stop = new_stop
                return True
        return False

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
