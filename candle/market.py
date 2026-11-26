"""地合い（相場全体の状態）の監視。個別株の買いが危険な場面を検知する。

3つを組み合わせる:
  日経225先物      … 日本市場全体の方向。急落したら「警戒」
  TOPIX           … 市場全体の広がり。先物に加えてTOPIXも下げていたら「警戒強化」
  監視中の銘柄     … 実際に個別株がどれだけ下げているか。下落銘柄が一定割合以上なら「新規買い停止」

先物だけで判断しないのは、日経平均が寄与度の高い一部の銘柄に引っ張られることがあるため。
先物やTOPIXを受信できていない場合は、その項目を「取得できていません」として扱い、推測はしない。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

LEVELS = {"normal": "通常", "caution": "警戒", "strong": "警戒強化", "halt": "新規買い停止"}


@dataclass
class MarketParams:
    window: float = 300.0        # 何秒前と比べるか（5分）
    fut_drop: float = 0.3        # 先物がこの%以上下げたら急落とみなす
    topix_drop: float = 0.15     # TOPIXがこの%以上下げたら下落とみなす
    breadth: float = 70.0        # 下落銘柄がこの%以上なら新規買い停止
    min_stocks: int = 20         # 割合を出すのに必要な銘柄数
    hold: float = 300.0          # 停止の条件が外れてからも、この秒数は停止を続ける
    sample: float = 5.0          # 銘柄ごとの価格を記録する最短間隔（秒）


class _Series:
    """価格の時系列（直近だけ保持）。"""

    def __init__(self, keep: float, sample: float = 0.0):
        self.keep, self.sample = keep, sample
        self.points: deque = deque()

    def add(self, price: float, now: datetime) -> None:
        self.last = price
        if self.points and (now - self.points[-1][0]).total_seconds() < self.sample:
            return                      # 記録は間引く（最新値は last に持つ）
        self.points.append((now, price))
        cutoff = now - timedelta(seconds=self.keep)
        while len(self.points) > 1 and self.points[1][0] <= cutoff:
            self.points.popleft()

    def change(self, now: datetime, window: float) -> float | None:
        """window 秒前からの変化率（%）。それだけ前のデータが無ければ None。"""
        target = now - timedelta(seconds=window)
        base = None
        for t, p in self.points:
            if t <= target:
                base = p
            else:
                break
        if base is None or base <= 0:
            return None
        return (self.last - base) / base * 100


class MarketMonitor:
    def __init__(self, params: MarketParams | None = None):
        self.p = params or MarketParams()
        self.symbols: dict[str, str] = {}      # 銘柄コード → 'fut' | 'topix'
        self.labels: dict[str, str] = {}       # 'fut' → 表示名
        self.problems: dict[str, str] = {}     # 'fut' → 取得できない理由
        self.index: dict[str, _Series] = {}
        self.index_time: dict[str, datetime] = {}
        self.stocks: dict[str, _Series] = {}
        self._halt_until: datetime | None = None

    def set_sources(self, symbols: dict[str, str], labels: dict[str, str],
                    problems: dict[str, str]) -> None:
        self.symbols, self.labels, self.problems = dict(symbols), dict(labels), dict(problems)

    def is_market_symbol(self, symbol: str) -> bool:
        return symbol in self.symbols

    def on_price(self, symbol: str, price: float, now: datetime) -> bool:
        """価格を取り込む。先物・TOPIXなら True（個別株としては扱わない）。"""
        keep = self.p.window + 120
        key = self.symbols.get(symbol)
        if key:
            self.index.setdefault(key, _Series(keep, 1.0)).add(price, now)
            self.index_time[key] = now
            return True
        self.stocks.setdefault(symbol, _Series(keep, self.p.sample)).add(price, now)
        return False

    def state(self, now: datetime, watchlist: list[str] | None = None) -> dict:
        p = self.p

        def idx(key: str, limit: float) -> dict:
            if key in self.problems:
                return dict(available=False, reason=self.problems[key], label=self.labels.get(key))
            s, t = self.index.get(key), self.index_time.get(key)
            if s is None or t is None:
                return dict(available=False, reason="まだ受信していません", label=self.labels.get(key))
            if (now - t).total_seconds() > 120:
                return dict(available=False, reason="2分以上、値を受信していません",
                            label=self.labels.get(key))
            chg = s.change(now, p.window)
            if chg is None:
                return dict(available=False, reason="比較する5分前の値がまだありません",
                            label=self.labels.get(key), price=s.last)
            return dict(available=True, change=chg, price=s.last, falling=chg <= -limit,
                        label=self.labels.get(key))

        fut, topix = idx("fut", p.fut_drop), idx("topix", p.topix_drop)
        names = watchlist if watchlist is not None else list(self.stocks)
        changes = [c for c in (self.stocks[s].change(now, p.window) for s in names if s in self.stocks)
                   if c is not None]
        down = sum(1 for c in changes if c < 0)
        if len(changes) >= p.min_stocks:
            breadth = dict(available=True, down=down, total=len(changes),
                           pct=down / len(changes) * 100)
            breadth["falling"] = breadth["pct"] >= p.breadth
        else:
            breadth = dict(available=False, down=down, total=len(changes),
                           reason=f"5分前と比べられる銘柄がまだ {len(changes)} 件です（{p.min_stocks} 件以上で判定）")

        if breadth.get("falling"):
            self._halt_until = now + timedelta(seconds=p.hold)
        halted = self._halt_until is not None and now < self._halt_until
        if halted:
            level = "halt"
        elif fut.get("falling") and topix.get("falling"):
            level = "strong"
        elif fut.get("falling"):
            level = "caution"
        else:
            level = "normal"
        return dict(level=level, label=LEVELS[level], halt=halted, fut=fut, topix=topix,
                    breadth=breadth, window_min=p.window / 60,
                    thresholds=dict(fut_drop=p.fut_drop, topix_drop=p.topix_drop, breadth=p.breadth),
                    held=halted and not breadth.get("falling"))

    def halted(self, now: datetime, watchlist: list[str] | None = None) -> bool:
        return self.state(now, watchlist)["halt"]
