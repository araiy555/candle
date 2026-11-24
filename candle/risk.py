"""全戦略共通のリスク管理。仮想売買でも実取引でも同じ判定を通す。"""
from __future__ import annotations

from dataclasses import dataclass

from .trade import Trade


@dataclass
class RiskLimits:
    total_cap: float = 1_000_000      # 総投資上限（円）
    per_symbol_cap: float = 500_000   # 1銘柄あたりの投資上限（円）
    daily_max_loss: float = 10_000    # 1日の最大損失（円）。到達で新規停止
    unit: int = 100                   # 売買単位
    min_risk_pct: float = 0.001       # 1Rが小さすぎるサインは見送る（価格比）
    max_risk_pct: float = 0.03        # 1Rが大きすぎるサインは見送る（価格比）


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits
        self.halted = False       # 緊急停止（手動）
        self.feed_ok = False      # API接続中か
        self.realized_today = 0.0

    def check_entry(self, strategy_id: str, symbol: str, price: float, stop: float,
                    open_trades: list[Trade], unrealized: float = 0.0) -> tuple[int, str]:
        """新規買いの可否と数量を返す。不可の場合は (0, 理由)。"""
        L = self.limits
        if self.halted:
            return 0, "緊急停止中"
        if not self.feed_ok:
            return 0, "API切断中"
        if self.realized_today + min(unrealized, 0.0) <= -L.daily_max_loss:
            return 0, "1日の最大損失に到達"
        risk = price - stop
        if risk <= 0 or risk < price * L.min_risk_pct:
            return 0, "損切り幅が小さすぎる"
        if risk > price * L.max_risk_pct:
            return 0, "損切り幅が大きすぎる"
        if any(t.strategy_id == strategy_id and t.symbol == symbol for t in open_trades):
            return 0, "同一戦略で保有中"
        used_total = sum(t.entry_price * t.qty for t in open_trades)
        used_symbol = sum(t.entry_price * t.qty for t in open_trades if t.symbol == symbol)
        budget = min(L.total_cap - used_total, L.per_symbol_cap - used_symbol)
        qty = int(budget // (price * L.unit)) * L.unit
        if qty <= 0:
            return 0, "資金上限を超える"
        return qty, "entry"

    def on_close(self, trade: Trade) -> None:
        self.realized_today += trade.pnl()

    def new_day(self) -> None:
        self.realized_today = 0.0
