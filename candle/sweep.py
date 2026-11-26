"""流動性スイープの検出。

買った人たちの損切り注文は、直近の目立つ安値の少し下にたまりやすい（流動性プール）。
株価がその安値を割ると損切りの売りが一斉に出て、大口の買い手はその売りを相手に買う。
売りが出尽くすと反転上昇する。板には見えないので、チャートから次の形を探す。

  流動性プール : 直近の押し安値（左右の足より安い安値）で、まだ割られていないもの
  スイープ     : その安値を下回った後、すぐに安値の上まで戻して引ける
  買いの裏付け : 割り込んだ足の出来高が、平均より大きく増えている

しきい値は初期値で、実データでの検証はこれから。通知と記録だけを行い、売買には使わない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from .bars import Bar


@dataclass(frozen=True)
class SweepParams:
    lookback: int = 90           # 流動性プール（押し安値）を探す本数
    pivot: int = 3               # 押し安値と認める左右の本数
    min_age: int = 5             # 押し安値ができてから、これ以上の本数が経っていること
    reclaim_bars: int = 2        # 割り込んでから、この本数以内に安値の上へ戻すこと
    max_depth: float = 0.01      # 割り込みがこれより深い場合は対象外（急落であって、狩りではない）
    vol_avg: int = 10
    vol_mult: float = 2.0        # 平均の何倍で「出来高が増えた」とみなすか


@dataclass
class SweepSignal:
    symbol: str
    time: datetime
    price: float                 # 戻した足の終値
    level: float                 # 流動性プール（割られた押し安値）
    level_time: datetime
    low: float                   # 割り込んだ安値
    note: str
    conditions: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def pools(bars: Sequence[Bar], p: SweepParams | None = None, upto: int | None = None) -> list[tuple[int, float]]:
    """まだ割られていない押し安値を (足の位置, 安値) で返す。upto より前の足だけで判定する。"""
    p = p or SweepParams()
    n = len(bars) if upto is None else upto
    out = []
    for i in range(max(p.pivot, n - p.lookback), n - p.pivot):
        low = bars[i].low
        if any(bars[j].low < low for j in range(i - p.pivot, i + p.pivot + 1) if j != i):
            continue
        if any(bars[j].low < low for j in range(i + 1, n)):
            continue                                  # すでに割られた
        out.append((i, low))
    return out


def detect(bars: Sequence[Bar], p: SweepParams | None = None) -> SweepSignal | None:
    """最後の足が確定した時点で、流動性スイープが起きたかを判定する。"""
    p = p or SweepParams()
    n = len(bars)
    if n < p.vol_avg + p.pivot * 2 + p.min_age:
        return None
    cur = bars[-1]
    for back in range(0, p.reclaim_bars):             # 割り込んだ足（今の足、または直前の足）
        s = n - 1 - back
        swept = [(i, lv) for i, lv in pools(bars, p, upto=s) if s - i >= p.min_age and bars[s].low < lv]
        if not swept:
            continue
        i, level = max(swept, key=lambda x: x[1])     # 割られた中で一番上のプール
        if any(bars[j].low < level for j in range(i + 1, s)):
            continue
        low = min(b.low for b in bars[s:])
        if cur.close <= level or (level - low) / level > p.max_depth:
            continue
        if back and bars[s].close > level:
            continue                                  # 直前の足ですでに戻していた（そのとき判定済み）
        prev = bars[max(0, s - p.vol_avg):s]
        avg = sum(b.volume for b in prev) / len(prev) if prev else 0.0
        vol = max(b.volume for b in bars[s:])
        ratio = vol / avg if avg > 0 else 0.0
        if ratio < p.vol_mult:
            continue
        depth = (level - low) / level * 100
        note = (f"押し安値 {level:,.1f}（{bars[i].start:%H:%M}）を {low:,.1f} まで割り込んだ後、"
                f"{cur.close:,.1f} まで回復。出来高 平均の{ratio:.1f}倍")
        conditions = [
            dict(key="pool", label="流動性プール", ok=True,
                 text=f"{bars[i].start:%H:%M} の押し安値 {level:,.1f}円。それ以降 {s - i}本、割られていなかった。"
                      "この少し下に、買った人たちの損切り注文がたまりやすい。"),
            dict(key="sweep", label="割り込みと回復", ok=True,
                 text=f"{bars[s].start:%H:%M} の足で {low:,.1f}円まで割り込み（{depth:.2f}%）、"
                      f"{'同じ足' if back == 0 else '次の足'}の終値 {cur.close:,.1f}円で安値の上に戻した。"),
            dict(key="volume", label="出来高の増加", ok=True,
                 text=f"割り込んだ足の出来高 {vol:,.0f}株は、直前{len(prev)}本の平均 {avg:,.0f}株の {ratio:.1f}倍"
                      f"（基準 {p.vol_mult:g}倍以上）。損切りの売りと、それを受けた買いが出た可能性。"),
        ]
        metrics = dict(level=level, level_time=bars[i].start.isoformat(), sweep_low=low,
                       depth_pct=depth, volume_ratio=ratio, sweep_volume=vol, avg_volume=avg,
                       bars_since_pool=s - i, reclaim_bars=back + 1)
        return SweepSignal(cur.symbol, cur.end, cur.close, level, bars[i].start, low, note,
                           conditions, metrics)
    return None
