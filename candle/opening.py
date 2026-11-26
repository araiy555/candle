"""寄り付き後の押し目買い（厚い買い板を背にして、数ティック上を狙う）の検出。通知と記録のみ。

  1. 時間帯は寄り付きから1時間（9:00〜10:00）
  2. 株価が当日の始値より上（日足が陽線になっている＝日足の向きと同じ方向にだけ買う）
  3. 当日高値からいったん下げている（押し目）
  4. 買う価格（最良売り気配）の3ティック以内の下に、厚い買い板がある
  → 買う価格から7ティック上を利確の目標、厚い買い板の1ティック下を損切りの目安として知らせる。

1ティックの大きさは、板に並んでいる価格の間隔から求める（銘柄ごとの呼値の区分を調べなくて済む）。
限界: 買い板は取り消されることがある（見せ板）。しきい値は初期値で、検証はこれから。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from statistics import median

from .flow import best_quotes


@dataclass
class OpenParams:
    start: time = time(9, 0)
    end: time = time(10, 0)
    wall_ticks: int = 3          # 買う価格の何ティック下までに厚い買い板があればよいか
    target_ticks: int = 7        # 利確の目標（買う価格の何ティック上か）
    mult: float = 4.0            # 他の買い板の中央値の何倍以上を「厚い」とみなすか
    min_value: float = 20_000_000  # 厚い買い板の金額（価格×数量）の下限（円）
    persist: float = 3.0         # この秒数以上、板に出続けたものだけを対象にする
    pullback_ticks: int = 2      # 当日高値から何ティック以上下げていれば「押し目」とみなすか
    break_ticks: int = 3         # 当日高値が始値より何ティック以上、上にあること（上に抜けた後であること）
    cooldown: float = 120.0      # 同じ銘柄で次に知らせるまでの秒数
    timeout: float = 300.0       # この秒数たっても目標にも損切りにも届かなければ「どちらにも届かず」


@dataclass
class OpenSignal:
    symbol: str
    time: datetime
    price: float                 # 買う価格（最良売り気配）
    low: float                   # 損切りの目安（厚い買い板の1ティック下）
    target: float
    note: str
    conditions: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


RESULTS = {"target": "目標に到達", "stop": "損切りに到達", "timeout": "どちらにも届かず"}


def bid_levels(msg: dict) -> list[tuple[float, float]]:
    out = []
    for i in range(1, 11):
        lv = msg.get(f"Buy{i}")
        if isinstance(lv, dict) and lv.get("Price") and lv.get("Qty") is not None:
            out.append((float(lv["Price"]), float(lv["Qty"])))
    return sorted(out, reverse=True)


def board_tick(msg: dict) -> float | None:
    """板に並んでいる価格の最小の間隔＝1ティック。価格が3つ以上見えていなければ None。"""
    prices = sorted({float(lv["Price"]) for side in ("Buy", "Sell") for i in range(1, 11)
                     if isinstance(lv := msg.get(f"{side}{i}"), dict) and lv.get("Price")})
    gaps = [round(b - a, 4) for a, b in zip(prices, prices[1:]) if b - a > 1e-9]
    return min(gaps) if len(gaps) >= 2 else None


class OpenTracker:
    def __init__(self, params: OpenParams | None = None):
        self.p = params or OpenParams()
        self.thick: dict[str, tuple[float, datetime]] = {}   # 銘柄 → (厚い買い板の価格, 見つけた時刻)
        self.last: dict[str, datetime] = {}

    def update(self, symbol: str, msg: dict, now: datetime) -> OpenSignal | None:
        p = self.p
        if not p.start <= now.time() < p.end:
            self.thick.pop(symbol, None)
            return None
        ask, _, bid = best_quotes(msg)
        price, opening, high = msg.get("CurrentPrice"), msg.get("OpeningPrice"), msg.get("HighPrice")
        bids, tick = bid_levels(msg), board_tick(msg)
        if not (ask and bid and price and opening and high and tick) or len(bids) < 5:
            return None
        near = [(pr, q) for pr, q in bids if pr >= ask - p.wall_ticks * tick - 1e-9]
        wall = max(near, key=lambda x: x[1], default=None)
        base = median([q for pr, q in bids if wall is None or pr != wall[0]])
        if wall is None or base <= 0 or wall[1] < p.mult * base or wall[0] * wall[1] < p.min_value:
            self.thick.pop(symbol, None)
            return None
        seen = self.thick.get(symbol)
        if seen is None or seen[0] != wall[0]:
            self.thick[symbol] = seen = (wall[0], now)
        secs = (now - seen[1]).total_seconds()
        pull = round((high - ask) / tick)
        up = round((high - opening) / tick)
        last = self.last.get(symbol)
        if (secs < p.persist or price <= opening or ask <= opening or pull < p.pullback_ticks
                or up < p.break_ticks or (last and (now - last).total_seconds() < p.cooldown)):
            return None
        self.last[symbol] = now
        target, stop = round(ask + p.target_ticks * tick, 4), round(wall[0] - tick, 4)
        ratio, below = wall[1] / base, round((ask - wall[0]) / tick)
        note = (f"買い {ask:,.1f}円 → 目標 {target:,.1f}円（+{p.target_ticks}ティック）／"
                f"厚い買い板 {wall[0]:,.1f}円に {wall[1]:,.0f}株／損切りの目安 {stop:,.1f}円")
        conditions = [
            dict(key="daily", label="日足が陽線", ok=True,
                 text=f"株価 {price:,.1f}円が、始値 {opening:,.1f}円より上。"),
            dict(key="pullback", label="押し目", ok=True,
                 text=f"当日高値 {high:,.1f}円（始値の {up}ティック上）から {pull}ティック下げた位置。"
                      f"基準は {p.pullback_ticks}ティック以上。"),
            dict(key="wall", label="厚い買い板", ok=True,
                 text=f"買う価格の {below}ティック下の {wall[0]:,.1f}円に {wall[1]:,.0f}株"
                      f"（約{wall[0] * wall[1] / 1e8:.2f}億円）。他の買い板の中央値の {ratio:.1f}倍。"
                      f"{secs:.0f}秒間、板に出ていた。"),
        ]
        metrics = dict(entry=ask, target=target, stop=stop, tick=tick, wall_price=wall[0], wall_qty=wall[1],
                       wall_value=wall[0] * wall[1], ratio=ratio, wall_below=below, open=float(opening),
                       day_high=float(high), pullback_ticks=pull, target_ticks=p.target_ticks,
                       outcome=None, outcome_label="判定中", result_seconds=None)
        return OpenSignal(symbol, now, float(ask), stop, target, note, conditions, metrics)
