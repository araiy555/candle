"""マルチタイムフレーム分析。1分足などの短い足から、5分足・15分足を作り、それぞれの向きを見る。

短い足のサインは、長い足の流れに沿っているほうが続きやすいと言われる。ここでは判定には使わず、
チャートでの切り替え表示と、取引の記録（後で「長い足が上向きのときの成績」を比べる）に使う。

向きの決め方（波の高値・安値の切り上げ・切り下げ）:
  上昇   : 直近の波の高値も安値も、その前より高い
  下降   : 直近の波の高値も安値も、その前より安い
  レンジ : それ以外
"""
from __future__ import annotations

from typing import Sequence

from .bars import Bar

FRAMES = (5, 15)                 # 上位足として見る分足
LABELS = {"up": "上昇", "down": "下降", "range": "レンジ", None: "判定できず"}


def aggregate(bars: Sequence[Bar], minutes: int) -> list[Bar]:
    """短い足をまとめて minutes 分足を作る（同じ日の、同じ区切りの足をまとめる）。"""
    out: list[Bar] = []
    for b in bars:
        start = b.start.replace(minute=b.start.minute - b.start.minute % minutes, second=0, microsecond=0)
        if out and out[-1].start == start:
            a = out[-1]
            a.high, a.low, a.close = max(a.high, b.high), min(a.low, b.low), b.close
            a.volume += b.volume
            a.ticks += b.ticks
            a.complete = a.complete and b.complete
            a.vwap = b.vwap or a.vwap
        else:
            out.append(Bar(b.symbol, start, minutes, b.open, b.high, b.low, b.close, b.volume, b.ticks,
                           b.complete, b.vwap))
    return out


def trend(bars: Sequence[Bar], pivot: int = 2) -> str | None:
    """波の高値・安値の並びから向きを決める。波が2つずつ無ければ None（判定できず）。"""
    n = len(bars)
    highs, lows = [], []
    hi_at = lo_at = -99
    for i in range(pivot, n - pivot):
        around = [j for j in range(i - pivot, i + pivot + 1) if j != i]
        # 同じ値が隣り合う山・谷は1つと数える
        if all(bars[j].high <= bars[i].high for j in around):
            if not (highs and i - hi_at <= pivot and highs[-1] == bars[i].high):
                highs.append(bars[i].high)
            hi_at = i
        if all(bars[j].low >= bars[i].low for j in around):
            if not (lows and i - lo_at <= pivot and lows[-1] == bars[i].low):
                lows.append(bars[i].low)
            lo_at = i
    if len(highs) < 2 or len(lows) < 2:
        return None
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "up"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "down"
    return "range"


def summary(bars: Sequence[Bar], base: int) -> list[dict]:
    """足の種類ごとの向き。base は元の足の分数。"""
    out = [dict(minutes=base, trend=trend(bars), bars=len(bars))]
    for m in FRAMES:
        if m > base and m % base == 0:
            agg = aggregate(bars, m)
            out.append(dict(minutes=m, trend=trend(agg), bars=len(agg)))
    for x in out:
        x["label"] = LABELS[x["trend"]]
    return out


def code(bars: Sequence[Bar], base: int) -> str:
    """取引の記録用。例: '5:up,15:range'（判定できない足は none）。"""
    return ",".join(f"{x['minutes']}:{x['trend'] or 'none'}" for x in summary(bars, base)[1:])
