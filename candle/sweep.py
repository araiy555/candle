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
    major: int = 10              # 外部流動性とみなす大きな波：左右この本数の中で一番安い（高い）
    bos_lookback: int = 60       # 高値ブレイクと、直近の上昇を探す本数
    min_leg: float = 0.004       # 上昇とみなす最小の値幅（安値比）


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


def liquidity_levels(bars: Sequence[Bar], p: SweepParams | None = None, major: int | None = None,
                     max_internal: int = 3) -> list[dict]:
    """チャートに表示する流動性（損切りがたまりやすい価格）。まだ抜かれていない高値と安値。

      上の流動性 : まだ上に抜かれていない高値（この上に、売った人の損切りがたまりやすい）
      下の流動性 : まだ下に割られていない安値（この下に、買った人の損切りがたまりやすい）
      外部流動性 : 当日の高値・安値、または左右 major 本の中で一番高い（安い）大きな波の高値・安値
      内部流動性 : その内側にできた小さな波の高値・安値（左右 pivot 本）

    内部流動性は数が多くなるので、今の株価に近いものから上下 max_internal 本ずつに絞る。
    """
    p = p or SweepParams()
    major = major or p.major
    n = len(bars)
    if n < p.pivot * 2 + 1:
        return []
    last = bars[-1].close
    day_high, day_low = max(b.high for b in bars), min(b.low for b in bars)
    out = []
    for side, val, better in (("high", lambda b: b.high, lambda a, b: a > b),
                              ("low", lambda b: b.low, lambda a, b: a < b)):
        found = []
        for i in range(p.pivot, n - p.pivot):
            lv = val(bars[i])
            if any(better(val(bars[j]), lv) for j in range(i - p.pivot, i + p.pivot + 1) if j != i):
                continue
            if any(better(val(bars[j]), lv) for j in range(i + 1, n)):
                continue                              # すでに抜かれた（流動性は消化済み）
            if found and found[-1]["level"] == lv:
                continue                              # 同じ価格が続くときは最初の1本
            big = not any(better(val(bars[j]), lv) for j in range(max(0, i - major), min(n, i + major + 1)) if j != i)
            edge = lv == (day_high if side == "high" else day_low)
            found.append(dict(side=side, t=bars[i].start.isoformat(), level=lv,
                              kind="external" if (big and min(i, n - 1 - i) >= major) or edge else "internal"))
        ext = [f for f in found if f["kind"] == "external"]
        inner = sorted((f for f in found if f["kind"] == "internal"), key=lambda f: abs(f["level"] - last))
        out += ext + inner[:max_internal]
    return out


def find_bos(bars: Sequence[Bar], upto: int, p: SweepParams) -> dict | None:
    """upto より前で、いちばん新しい「高値ブレイク」を探す。

    高値ブレイク: それまで抜かれていなかった戻り高値（左右の足より高い高値）を、終値で上に抜けた足。
    抜けたということは、そこで新しく買った人がいて、その損切りが下にたまる（内部流動性）。
    """
    start = max(p.pivot, upto - p.bos_lookback)
    highs = [k for k in range(start, upto - p.pivot)
             if all(bars[j].high <= bars[k].high for j in range(k - p.pivot, k + p.pivot + 1) if j != k)]
    best = None
    for k in highs:
        level = bars[k].high
        j = next((j for j in range(k + p.pivot + 1, upto) if bars[j].close > level), None)
        if j is None:
            continue
        if best is None or j > best["bos_index"] or (j == best["bos_index"] and level > best["bos_level"]):
            prev = bars[max(0, j - p.vol_avg):j]
            avg = sum(b.volume for b in prev) / len(prev) if prev else 0.0
            best = dict(bos_index=j, bos_high_index=k, bos_level=level, bos_time=bars[j].start.isoformat(),
                        bos_volume_ratio=bars[j].volume / avg if avg > 0 else None)
    return best


def context(bars: Sequence[Bar], s: int, i: int, level: float, low: float, p: SweepParams) -> dict:
    """スイープが起きた場面の分類（後で、どの場面のスイープが効くのかを比べるための印）。

      after_bos : 高値ブレイクの後で、割られた押し安値が、抜かれた高値より後にできたもの
      zone      : 直近の上昇（安値〜高値）の中での位置。discount=割安（下半分） / premium=割高 / below=範囲の下
      liquidity : external=当日安値を割った（外部流動性） / internal=当日安値より上の押し安値（内部流動性）
    """
    bos = find_bos(bars, s, p)
    after_bos = bool(bos and i > bos["bos_high_index"])
    day_low = min(b.low for b in bars[:s])
    seg = bars[max(0, s - p.bos_lookback):s]
    ih = max(range(len(seg)), key=lambda k: (seg[k].high, k))
    leg_high = seg[ih].high
    leg_low = min(b.low for b in seg[:ih + 1])
    zone = None
    if leg_low > 0 and (leg_high - leg_low) / leg_low >= p.min_leg:
        mid = (leg_low + leg_high) / 2
        zone = "below" if low < leg_low else "discount" if low < mid else "premium"
    # 外部流動性: 当日安値、または左右 major 本の中で一番安い大きな波の安値（チャートの表示と同じ決め方）
    big = i >= p.major and not any(bars[j].low < level for j in range(i - p.major, min(s, i + p.major + 1)) if j != i)
    ext = "当日安値" if level <= day_low else "大きな波の安値" if big else None
    out = dict(after_bos=after_bos, zone=zone, liquidity="external" if ext else "internal", ext_kind=ext,
               leg_low=leg_low, leg_high=leg_high, bos_setup=after_bos and zone == "discount" and not ext)
    if bos:
        out.update(bos_level=bos["bos_level"], bos_time=bos["bos_time"], bos_volume_ratio=bos["bos_volume_ratio"])
    return out


ZONES = {"discount": "割安ゾーン", "premium": "割高ゾーン", "below": "上昇の起点より下", None: "判定できず"}


def context_conditions(c: dict) -> list:
    bos = (f"{c['bos_time'][11:16]} の足が、戻り高値 {c['bos_level']:,.1f}円を終値で上に抜けた"
           + (f"（出来高 平均の{c['bos_volume_ratio']:.1f}倍）" if c.get("bos_volume_ratio") else "") + "。"
           if c.get("bos_level") else "直近に、戻り高値を上に抜けた足は無い。")
    return [
        dict(key="bos", label="高値ブレイクの後", ok=c["after_bos"],
             text=bos + ("割られた押し安値は、その後にできたもの。" if c["after_bos"] else "")),
        dict(key="zone", label="割安ゾーン", ok=c["zone"] == "discount",
             text=f"直近の上昇 {c['leg_low']:,.1f}〜{c['leg_high']:,.1f}円の中で、{ZONES[c['zone']]}。"),
        dict(key="liquidity", label="内部流動性", ok=c["liquidity"] == "internal",
             text="大きな波の内側にできた、小さな波の押し安値を割った（内部流動性）。" if c["liquidity"] == "internal"
             else f"{c.get('ext_kind') or '外部の安値'}を割った（外部流動性）。"),
    ]


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
        ctx = context(bars, s, i, level, low, p)
        metrics.update(ctx)
        conditions += context_conditions(ctx)
        return SweepSignal(cur.symbol, cur.end, cur.close, level, bars[i].start, low, note,
                           conditions, metrics)
    return None
