"""FVG（フェアバリューギャップ）による通知。

上昇の途中でできたFVGのうち、その上昇の「割安ゾーン」（安値から高値までの下半分）にあるものを
対象にする。押し目がそのFVGまで来たときの出来高の出方で、2種類のシグナルを出す。

  FVG反発: 押し安値がFVGの上で止まり、出来高を伴って反発した → 上昇の継続を示唆
  FVG割れ: 出来高を伴ってFVGを終値で下抜けた → 上昇前のレンジのPOCまで戻る可能性

用語:
  FVG  … 3本の足で、1本目の高値より3本目の安値が高いときにできる、約定の空白の価格帯
  POC  … 出来高が最も多かった価格（ここでは、上昇が始まる前の足から出来高の分布を作る）

しきい値は初期値で、実データでの検証はこれから。売買には使わず、通知だけを行う。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from .bars import Bar


@dataclass(frozen=True)
class FvgParams:
    leg_lookback: int = 60       # 上昇の高値・安値を探す本数
    min_leg: float = 0.004       # 上昇とみなす最小の値幅（安値比）
    min_gap: float = 0.0003      # FVGとみなす最小の幅（価格比）
    touch_tol: float = 0.0005    # 押し安値が「FVGまで来た」とみなす余裕（価格比）
    recent: int = 3              # 押し安値がこの本数以内に付いていること
    vol_avg: int = 10            # 出来高の平均を取る本数
    vol_mult: float = 1.5        # 平均の何倍で「出来高が増えた」とみなすか
    range_bars: int = 90         # POCを出すレンジの最大本数
    bin_pct: float = 0.0005      # 出来高分布の価格の刻み（価格比）


@dataclass
class FvgSignal:
    kind: str                    # 'hold'（反発） | 'break'（割れ）
    formed: datetime             # FVGができた足（中央の足）の時刻
    bottom: float
    top: float
    leg_low: float
    leg_high: float
    pullback_low: float
    volume_ratio: float
    poc: float | None
    poc_source: str              # POCをどの範囲の足から出したか
    note: str


def find_fvgs(bars: Sequence[Bar], start: int, end: int, min_gap: float) -> list[tuple[int, float, float]]:
    """bars[start..end] の中の上向きのFVGを (中央の足の位置, 下端, 上端) で返す。"""
    out = []
    for k in range(start + 1, end):
        bottom, top = bars[k - 1].high, bars[k + 1].low
        if top - bottom > max(min_gap * bars[k].close, 0.0):
            out.append((k, bottom, top))
    return out


def poc(bars: Sequence[Bar], bin_pct: float = 0.0005) -> float | None:
    """出来高が最も多い価格。各足の出来高を、その足の安値〜高値に均等に割り振って分布を作る。"""
    bars = [b for b in bars if b.volume > 0]
    if not bars:
        return None
    lo, hi = min(b.low for b in bars), max(b.high for b in bars)
    step = max(hi * bin_pct, 1e-9)
    n = max(1, int((hi - lo) / step) + 1)
    hist = [0.0] * n
    for b in bars:
        a, z = int((b.low - lo) / step), int((b.high - lo) / step)
        share = b.volume / (z - a + 1)
        for i in range(a, z + 1):
            hist[min(i, n - 1)] += share
    best = max(range(n), key=lambda i: hist[i])
    return lo + (best + 0.5) * step if n > 1 else (lo + hi) / 2


def find_leg(bars: Sequence[Bar], p: FvgParams) -> tuple[int, int] | None:
    """直近の上昇（安値の足の位置, 高値の足の位置）。上昇と呼べる値幅が無ければ None。"""
    n = len(bars)
    if n < 4:
        return None
    s0 = max(0, n - p.leg_lookback)
    ih = max(range(s0, n), key=lambda i: (bars[i].high, i))        # 直近の高値（同値なら新しいほう）
    if ih < 3:
        return None
    l0 = max(0, ih - p.leg_lookback)
    il = min(range(l0, ih), key=lambda i: (bars[i].low, -i))       # その高値に向かう上昇の起点
    low, high = bars[il].low, bars[ih].high
    if low <= 0 or (high - low) / low < p.min_leg:
        return None
    # 安値圏での横ばいを上昇に含めないよう、起点を「最後に安値付近にいた足」まで進める
    near = low + (high - low) * 0.1
    for i in range(ih - 2, il, -1):
        if bars[i].low <= near:
            il = i
            break
    if ih - il < 2:
        return None
    return il, ih


def profile(bars: Sequence[Bar], bin_pct: float) -> list[tuple[float, float]]:
    """出来高の分布を (価格, 出来高) の並びで返す（表示用）。"""
    bars = [b for b in bars if b.volume > 0]
    if not bars:
        return []
    lo, hi = min(b.low for b in bars), max(b.high for b in bars)
    step = max(hi * bin_pct, 1e-9)
    n = max(1, int((hi - lo) / step) + 1)
    hist = [0.0] * n
    for b in bars:
        a, z = int((b.low - lo) / step), int((b.high - lo) / step)
        share = b.volume / (z - a + 1)
        for i in range(a, z + 1):
            hist[min(i, n - 1)] += share
    return [(lo + (i + 0.5) * step, v) for i, v in enumerate(hist)]


def structure(bars: Sequence[Bar], p: FvgParams | None = None) -> dict | None:
    """チャートに重ねて表示するための、直近の上昇・割高/割安ゾーン・FVG・出来高分布。"""
    p = p or FvgParams()
    leg = find_leg(bars, p)
    if leg is None:
        return None
    il, ih = leg
    n = len(bars)
    low, high = min(b.low for b in bars[il:ih + 1]), bars[ih].high
    half = (low + high) / 2
    fvgs = []
    for k, bottom, top in find_fvgs(bars, il, ih, p.min_gap):
        broken = next((bars[j].start.isoformat() for j in range(k + 2, n) if bars[j].close < bottom), None)
        fvgs.append(dict(t=bars[k].start.isoformat(), bottom=bottom, top=top,
                         discount=(bottom + top) / 2 <= half, broken=broken))
    prof = profile(bars[il:ih + 1], max(p.bin_pct, (high - low) / high / 40))   # 表示は40段ほどに
    before = list(bars[max(0, il - p.range_bars):il])
    return dict(start=bars[il].start.isoformat(), end=bars[ih].start.isoformat(), low=low, high=high,
                mid=half, fvgs=fvgs, profile=[dict(price=a, vol=v) for a, v in prof],
                leg_poc=max(prof, key=lambda x: x[1])[0] if prof else None,
                range_poc=poc(before, p.bin_pct) if len(before) >= 10 else None,
                pullback=ih < n - 1)


def detect(bars: Sequence[Bar], p: FvgParams | None = None) -> FvgSignal | None:
    """最後の足が確定した時点で、FVGの反発または割れが起きたかを判定する。"""
    p = p or FvgParams()
    n = len(bars)
    if n < p.vol_avg + 6:
        return None
    cur = bars[-1]
    leg = find_leg(bars, p)
    if leg is None or leg[1] >= n - 1:
        return None                                                # まだ高値を更新中 → 押し目ではない
    il, ih = leg
    leg_low, leg_high = min(b.low for b in bars[il:ih + 1]), bars[ih].high
    half = (leg_low + leg_high) / 2                                # これより下が割安ゾーン

    # 割安ゾーンにあり、これまで終値で割られていないFVG。複数あれば一番上（最初に試される）を採る。
    chosen = None
    for k, bottom, top in find_fvgs(bars, il, ih, p.min_gap):
        if (bottom + top) / 2 > half:
            continue
        if any(b.close < bottom for b in bars[k + 2:n - 1]):
            continue
        if chosen is None or top > chosen[2]:
            chosen = (k, bottom, top)
    if chosen is None:
        return None
    k, bottom, top = chosen

    prev = bars[n - 1 - p.vol_avg:n - 1]
    avg = sum(b.volume for b in prev) / len(prev)
    if avg <= 0:
        return None
    ratio = cur.volume / avg
    ipl = min(range(ih + 1, n), key=lambda i: (bars[i].low, -i))
    pullback_low = bars[ipl].low
    if ratio < p.vol_mult:
        return None

    kind = None
    if cur.close < bottom:
        kind = "break"
    elif (bottom <= pullback_low <= top * (1 + p.touch_tol) and n - 1 - ipl <= p.recent
          and cur.close > cur.open and cur.close > top):
        kind = "hold"
    if kind is None:
        return None

    before = [b for b in bars[max(0, il - p.range_bars):il]]
    if len(before) >= 10:
        level, source = poc(before, p.bin_pct), "上昇が始まる前のレンジ"
    else:
        level, source = poc(bars[:n], p.bin_pct), "当日のここまでの全体（上昇前の足が少ないため）"
    zone = f"FVG {bottom:,.1f}〜{top:,.1f}（上昇 {leg_low:,.1f}→{leg_high:,.1f} の割安ゾーン）"
    if kind == "hold":
        note = (f"{zone}の上で押し安値 {pullback_low:,.1f} を付けて反発、出来高 平均の{ratio:.1f}倍"
                " → 上昇継続を示唆")
    else:
        note = (f"{zone}を終値 {cur.close:,.1f} で下抜け、出来高 平均の{ratio:.1f}倍"
                + (f" → POC {level:,.1f}（{source}）まで戻る可能性" if level else ""))
    return FvgSignal(kind, bars[k].start, bottom, top, leg_low, leg_high, pullback_low, ratio,
                     level, source, note)
