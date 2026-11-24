"""実際の発注（第5段階）。kabuステーションAPIで信用の買い・逆指値・返済・取消を行う。

ここは実際のお金が動く部分。注文内容は必ずこのファイルの中だけで組み立てる。
"""
from __future__ import annotations

from dataclasses import dataclass

from .kabu_client import KabuClient


@dataclass
class OrderStatus:
    done: bool                 # 注文が終了したか（全約定・取消済・失効・エラー）
    cum_qty: int = 0           # 約定した株数
    avg_price: float | None = None


@dataclass
class LiveConfig:
    exchange: int = 9            # 9=SOR, 27=東証+
    margin_trade_type: int = 3   # 1=制度信用, 2=一般信用(長期), 3=一般信用(デイトレ)
    account_type: int = 4        # 4=特定, 2=一般
    stop_method: str = "server"  # server=証券会社側に逆指値を置く, client=このプログラムが監視して成行決済


class KabuBroker:
    def __init__(self, client: KabuClient, cfg: LiveConfig | None = None):
        self.client = client
        self.cfg = cfg or LiveConfig()

    def _base(self, symbol: str, qty: int) -> dict:
        return {"Symbol": symbol, "Exchange": self.cfg.exchange, "SecurityType": 1,
                "MarginTradeType": self.cfg.margin_trade_type,
                "AccountType": self.cfg.account_type, "Qty": int(qty), "Price": 0,
                "ExpireDay": 0}

    def buy_body(self, symbol: str, qty: int) -> dict:
        """信用新規買い・成行。"""
        return {**self._base(symbol, qty), "Side": "2", "CashMargin": 2, "DelivType": 0,
                "FrontOrderType": 10}

    def close_body(self, symbol: str, qty: int) -> dict:
        """信用返済売り・成行。"""
        return {**self._base(symbol, qty), "Side": "1", "CashMargin": 3, "DelivType": 2,
                "ClosePositionOrder": 0, "FrontOrderType": 10}

    def stop_body(self, symbol: str, qty: int, trigger: float) -> dict:
        """信用返済売り・逆指値（トリガ価格以下になったら成行）。"""
        return {**self._base(symbol, qty), "Side": "1", "CashMargin": 3, "DelivType": 2,
                "ClosePositionOrder": 0, "FrontOrderType": 30,
                "ReverseLimitOrder": {"TriggerSec": 1, "TriggerPrice": trigger, "UnderOver": 1,
                                      "AfterHitOrderType": 1, "AfterHitPrice": 0}}

    def buy_market(self, symbol: str, qty: int) -> str:
        return self.client.send_order(self.buy_body(symbol, qty))

    def close_market(self, symbol: str, qty: int) -> str:
        return self.client.send_order(self.close_body(symbol, qty))

    def place_stop(self, symbol: str, qty: int, trigger: float) -> str:
        return self.client.send_order(self.stop_body(symbol, qty, trigger))

    def cancel(self, order_id: str) -> None:
        self.client.cancel_order(order_id)

    def order(self, order_id: str) -> OrderStatus:
        rows = self.client.orders(id=order_id)
        row = next((r for r in rows if r.get("ID") == order_id), None)
        if row is None:
            return OrderStatus(False)   # まだ照会に出てこない
        return parse_order(row)

    def positions(self) -> dict[str, int]:
        """信用建玉の銘柄ごとの残株数。"""
        out: dict[str, int] = {}
        for p in self.client.positions(product=2):
            qty = int(p.get("LeavesQty") or 0)
            if qty > 0 and str(p.get("Side")) == "2":
                out[str(p["Symbol"])] = out.get(str(p["Symbol"]), 0) + qty
        return out

    def tick_group(self, symbol: str) -> str:
        info = self.client.symbol_info(symbol, 1)
        return str(info.get("PriceRangeGroup") or "10000")


def parse_order(row: dict) -> OrderStatus:
    """注文約定照会の1件から、終了したか・約定株数・平均約定価格を取り出す。"""
    qty, value = 0.0, 0.0
    for d in row.get("Details") or []:
        if d.get("RecType") == 8 and d.get("Qty"):   # 8 = 約定
            qty += float(d["Qty"])
            value += float(d["Qty"]) * float(d.get("Price") or 0)
    cum = int(row.get("CumQty") or 0)
    avg = value / qty if qty > 0 and value > 0 else None
    state = row.get("State", row.get("OrderState"))
    return OrderStatus(done=state == 5, cum_qty=cum, avg_price=avg if cum > 0 else None)
