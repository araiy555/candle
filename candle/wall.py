"""大口の売り板（売り注文が集まっている価格帯）の観察。

機関投資家などの大口の買い手は、自分の注文を約定させられるだけの売り注文が集まっている場所で
執行することが多い。そこで、まとまった売り板を見つけ、株価が近づいたときに次のことを記録する。

  1. 大量の売り注文が集まっている価格を検出する
  2. その価格に株価が接近する
  3. 売り板が約定によって減っていくか（買いに吸収されているか）
  4. 売り板が補充されるか
  5. 売り板を突破した後、株価がどう動くか（追跡は squeeze_log が行う）

結果は3種類: 吸収して突破 / 取消で消えた / 跳ね返された。

限界: 見えるのは板の上から10本だけ。板の数量が減った理由（約定か取消か）は、その間の出来高からの
推定。表示されない注文（アイスバーグなど）は分からない。しきい値は初期値で、検証はこれから。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import median


@dataclass
class WallParams:
    mult: float = 4.0            # 他の売り板の中央値の何倍以上を「大口」とみなすか
    min_value: float = 20_000_000  # 売り板の金額（価格×数量）の下限（円）
    persist: float = 10.0        # この秒数以上、板に出続けたものだけを対象にする（見せ板の瞬間的な出入りを除く）
    near: float = 0.001          # 最良売り気配がこの率以内まで近づいたら「接近」
    absorb_share: float = 0.5    # 最大数量のこの割合以上が約定で減っていれば「吸収」
    reject: float = 0.004        # 接近後にこの率以上、下に離れたら「跳ね返された」


@dataclass
class Wall:
    price: float
    seen: datetime
    max_qty: float
    last_qty: float
    ratio: float                 # 検出時、他の売り板の中央値の何倍だったか
    executed: float = 0.0        # 約定で減った数量（推定）
    cancelled: float = 0.0       # 取消で減った数量（推定）
    added: float = 0.0           # 補充された数量
    approached: datetime | None = None


@dataclass
class WallEvent:
    kind: str                    # 'approach' | 'break' | 'pulled' | 'rejected'
    symbol: str
    time: datetime
    price: float                 # その時点の株価
    wall: Wall
    note: str
    conditions: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


OUTCOMES = {"break": "吸収して突破", "pulled": "取消で消えた", "rejected": "跳ね返された"}


def ask_levels(msg: dict) -> list[tuple[float, float]]:
    out = []
    for i in range(1, 11):
        lv = msg.get(f"Sell{i}")
        if isinstance(lv, dict) and lv.get("Price") and lv.get("Qty") is not None:
            out.append((float(lv["Price"]), float(lv["Qty"])))
    return sorted(out)


class WallTracker:
    def __init__(self, params: WallParams | None = None):
        self.p = params or WallParams()
        self.walls: dict[str, Wall] = {}

    def update(self, symbol: str, msg: dict, now: datetime, vol: float = 0.0) -> list[WallEvent]:
        """配信1件を取り込む。vol はこの配信までに増えた出来高。起きた出来事を返す。"""
        asks = ask_levels(msg)
        price = msg.get("CurrentPrice")
        if len(asks) < 5 or price is None:
            return []
        price = float(price)
        wall = self.walls.get(symbol)
        if wall is None:
            self._find(symbol, asks, now)
            return []
        events: list[WallEvent] = []
        level = next((q for p, q in asks if p == wall.price), None)
        traded = vol if vol > 0 and price == wall.price else 0.0   # 売り板の価格で付いた約定
        confirmed = (now - wall.seen).total_seconds() >= self.p.persist

        if level is not None:
            diff = level - wall.last_qty
            if diff < 0:
                ex = min(-diff, traded)
                wall.executed += ex
                wall.cancelled += -diff - ex
            else:
                wall.executed += traded
                wall.added += diff + traded        # 約定で減った分も含めて積み直されている
            wall.last_qty = level
            wall.max_qty = max(wall.max_qty, level)
            if not confirmed:
                return []
            if wall.approached is None and asks[0][0] >= wall.price * (1 - self.p.near):
                wall.approached = now
                events.append(self._event("approach", symbol, now, price, wall))
            elif wall.approached and price <= wall.price * (1 - self.p.reject):
                events.append(self._event("rejected", symbol, now, price, wall))
                del self.walls[symbol]
            return events

        # 板の10本の中に、その価格が見えなくなった
        if wall.price < asks[0][0]:                # 最良売り気配より下 → 売り板が無くなった
            # 株価が売り板の価格以上で付いているなら、残りはその間の出来高で約定したとみなす
            ex = min(wall.last_qty, vol if vol > 0 and price >= wall.price else 0.0)
            wall.executed += ex
            wall.cancelled += wall.last_qty - ex
            wall.last_qty = 0.0
            del self.walls[symbol]
            if confirmed:
                absorbed = wall.executed >= self.p.absorb_share * wall.max_qty
                events.append(self._event("break" if absorbed and price >= wall.price else "pulled",
                                          symbol, now, price, wall))
        else:                                      # 株価が下がって、売り板が表示範囲の外に出た
            del self.walls[symbol]
            if confirmed and wall.approached:
                events.append(self._event("rejected", symbol, now, price, wall))
        return events

    def _find(self, symbol: str, asks: list[tuple[float, float]], now: datetime) -> None:
        price, qty = max(asks, key=lambda x: x[1])
        others = [q for p, q in asks if p != price]
        base = median(others) if others else 0.0
        if base > 0 and qty >= self.p.mult * base and price * qty >= self.p.min_value:
            self.walls[symbol] = Wall(price, now, qty, qty, qty / base)

    def _event(self, kind: str, symbol: str, now: datetime, price: float, w: Wall) -> WallEvent:
        share = w.executed / w.max_qty if w.max_qty else 0.0
        secs = (now - w.seen).total_seconds()
        head = f"{w.price:,.1f}円の売り板 {w.max_qty:,.0f}株（約{w.price * w.max_qty / 1e8:.2f}億円）"
        if kind == "approach":
            note = f"{head}に接近"
        else:
            note = (f"{head}を{OUTCOMES[kind]}：約定で {w.executed:,.0f}株（{share:.0%}）、"
                    f"取消で {w.cancelled:,.0f}株、補充 {w.added:,.0f}株")
        conditions = [
            dict(key="wall", label="大口の売り板", ok=True,
                 text=f"{w.price:,.1f}円に最大 {w.max_qty:,.0f}株（約{w.price * w.max_qty / 1e8:.2f}億円）。"
                      f"検出時、他の売り板の中央値の {w.ratio:.1f}倍。{secs:.0f}秒間、板に出ていた。"),
            dict(key="executed", label="約定による減少（推定）", ok=share >= self.p.absorb_share,
                 text=f"{w.executed:,.0f}株（最大数量の {share:.0%}）。"
                      f"基準は {self.p.absorb_share:.0%}以上で「買いに吸収された」とみなす。"),
            dict(key="cancelled", label="取消による減少（推定）", ok=w.cancelled > 0,
                 text=f"{w.cancelled:,.0f}株。その間の出来高で説明できない減少を、取消として数えている。"),
            dict(key="added", label="補充", ok=w.added > 0,
                 text=f"{w.added:,.0f}株。約定や取消の後に積み直された数量。"),
        ]
        metrics = dict(outcome=kind, outcome_label=OUTCOMES.get(kind, "接近"), wall_price=w.price,
                       max_qty=w.max_qty, wall_value=w.price * w.max_qty, ratio=w.ratio,
                       executed=w.executed, cancelled=w.cancelled, added=w.added,
                       executed_share=share, seconds=secs,
                       approach_seconds=(now - w.approached).total_seconds() if w.approached else None)
        return WallEvent(kind, symbol, now, price, w, note, conditions, metrics)
