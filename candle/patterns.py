"""ローソク足パターンの検出（第2段階）。

各パターンは独立した関数で、最後の足が確定した時点で判定する。
成立した場合は損切りの基準となる「パターン形成時の安値」を返す。
これらは検証候補であり、優位性が証明されたものではない。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Sequence

from .bars import Bar, bucket_start


@dataclass(frozen=True)
class Params:
    trend_bars: int = 6          # 直前の下落を確認する本数（6本=30分）
    low_tolerance: float = 0.0005  # 安値が「同じ」とみなす許容差（価格比）
    db_lookback: int = 30        # 二点底を探す本数
    db_min_gap: int = 4          # 2つの底の間の最小本数
    db_tolerance: float = 0.003  # 2つの底の安値の許容差（価格比）
    db_swing: int = 2            # 底と認める左右の本数
    min_drop: float = 0.0        # 直前の下落率の下限（0.003 = 0.3%）。これ未満の下げはサインにしない


def expected_next(start: datetime, minutes: int) -> datetime | None:
    """次の足の開始時刻。昼休みはまたぐ。大引け後は None。"""
    nxt = start + timedelta(minutes=minutes)
    if nxt.hour == 11 and nxt.minute == 30:
        return nxt.replace(hour=12, minute=30)
    if nxt.hour == 15 and nxt.minute == 30:
        return None
    return nxt if bucket_start(nxt, minutes) == nxt else None


def contiguous_tail(bars: Sequence[Bar]) -> list[Bar]:
    """末尾から、欠けや不完全な足を含まない連続部分だけを取り出す。"""
    out: list[Bar] = []
    for b in reversed(bars):
        if not b.complete:
            break
        if out and expected_next(b.start, b.minutes) != out[-1].start:
            break
        out.append(b)
    out.reverse()
    return out


def _body(b: Bar) -> float:
    return abs(b.close - b.open)


def _bull(b: Bar) -> bool:
    return b.close > b.open


def _bear(b: Bar) -> bool:
    return b.close < b.open


def decline(bars: Sequence[Bar], n: int, p: Params) -> float | None:
    """パターン（末尾n本）に向けて、直前にどれだけ下げてきたか。

    直前 trend_bars 本とパターンの中での最高値から、パターンの安値までの下落率。
    """
    if len(bars) < n + p.trend_bars:
        return None
    high = max(b.high for b in bars[-(n + p.trend_bars):])
    low = min(b.low for b in bars[-n:])
    return (high - low) / high if high > 0 else None


def _downtrend(bars: Sequence[Bar], n: int, p: Params) -> bool:
    """パターン（末尾n本）の直前が下落局面で、パターン内に直近安値があるか。"""
    if len(bars) < n + p.trend_bars:
        return False
    prior = bars[-(n + p.trend_bars):-n]
    pattern_low = min(b.low for b in bars[-n:])
    if not (prior[0].close > prior[-1].close and pattern_low <= min(b.low for b in prior)):
        return False
    return (decline(bars, n, p) or 0.0) >= p.min_drop   # 横ばいの中の小さな上下は底打ちと見なさない


def tweezer_bottom(bars: Sequence[Bar], p: Params) -> float | None:
    """毛抜き底: 陰線→陽線で、2本の安値がほぼ同じ。"""
    if len(bars) < 2:
        return None
    a, b = bars[-2], bars[-1]
    if not (_bear(a) and _bull(b)) or abs(a.low - b.low) > b.close * p.low_tolerance:
        return None
    return min(a.low, b.low) if _downtrend(bars, 2, p) else None


def bullish_engulfing(bars: Sequence[Bar], p: Params) -> float | None:
    """強気包み足: 陽線の実体が直前の陰線の実体を包む。"""
    if len(bars) < 2:
        return None
    a, b = bars[-2], bars[-1]
    if not (_bear(a) and _bull(b) and b.open <= a.close and b.close >= a.open
            and _body(b) > _body(a)):
        return None
    return min(a.low, b.low) if _downtrend(bars, 2, p) else None


def hammer(bars: Sequence[Bar], p: Params) -> float | None:
    """ハンマー: 下ヒゲが実体の2倍以上、上ヒゲが小さい。"""
    if not bars:
        return None
    b = bars[-1]
    rng, body = b.high - b.low, _body(b)
    if rng <= 0 or body <= 0:
        return None
    lower = min(b.open, b.close) - b.low
    upper = b.high - max(b.open, b.close)
    if lower < 2 * body or upper > 0.15 * rng or body > 0.35 * rng:
        return None
    return b.low if _downtrend(bars, 1, p) else None


def morning_star(bars: Sequence[Bar], p: Params) -> float | None:
    """明けの明星: 大陰線→小さな実体→1本目の実体の半分を超える陽線。"""
    if len(bars) < 3:
        return None
    a, s, c = bars[-3], bars[-2], bars[-1]
    if not (_bear(a) and _bull(c)) or (a.high - a.low) <= 0:
        return None
    if _body(a) < 0.6 * (a.high - a.low) or _body(s) > 0.3 * _body(a):
        return None
    if max(s.open, s.close) > a.close or c.close <= (a.open + a.close) / 2:
        return None
    return min(a.low, s.low, c.low) if _downtrend(bars, 3, p) else None


def piercing_line(bars: Sequence[Bar], p: Params) -> float | None:
    """切り込み線: 陰線の終値以下で始まり、実体の半分を超えて戻す陽線（始値は超えない）。"""
    if len(bars) < 2:
        return None
    a, b = bars[-2], bars[-1]
    if not (_bear(a) and _bull(b) and b.open <= a.close
            and (a.open + a.close) / 2 < b.close < a.open):
        return None
    return min(a.low, b.low) if _downtrend(bars, 2, p) else None


def bullish_harami(bars: Sequence[Bar], p: Params) -> float | None:
    """強気はらみ線: 大陰線の実体の中に収まる小さな陽線。"""
    if len(bars) < 2:
        return None
    a, b = bars[-2], bars[-1]
    if not (_bear(a) and _bull(b)) or (a.high - a.low) <= 0:
        return None
    if _body(a) < 0.6 * (a.high - a.low) or _body(b) > 0.5 * _body(a):
        return None
    if not (b.open >= a.close and b.close <= a.open):
        return None
    return min(a.low, b.low) if _downtrend(bars, 2, p) else None


def double_bottom(bars: Sequence[Bar], p: Params) -> float | None:
    """二点底: 近い水準の2つの底の後、間の高値（ネックライン）を終値で上抜けた足。"""
    w = list(bars[-p.db_lookback:])
    k = p.db_swing
    if len(w) < 2 * k + p.db_min_gap + 3:
        return None
    lows = [i for i in range(k, len(w) - 1)
            if all(w[i].low <= w[j].low for j in range(max(0, i - k), min(len(w) - 1, i + k) + 1))]
    last, prev = w[-1], w[-2]
    for j in reversed(lows):
        for i in reversed(lows):
            if j - i < p.db_min_gap:
                continue
            if abs(w[i].low - w[j].low) > last.close * p.db_tolerance:
                continue
            neck = max(b.high for b in w[i + 1:j])
            # 2番目の底の後、今回の足で初めてネックラインを上抜けたこと
            if last.close > neck and prev.close <= neck and j < len(w) - 1 \
                    and all(b.close <= neck for b in w[j:-1]) \
                    and max(b.high for b in w[:i + 1]) > neck:
                return min(w[i].low, w[j].low)
    return None


@dataclass(frozen=True)
class Strategy:
    id: str
    name: str
    detect: Callable[[Sequence[Bar], Params], float | None]


STRATEGIES: tuple[Strategy, ...] = (
    Strategy("tweezer_bottom", "毛抜き底", tweezer_bottom),
    Strategy("bullish_engulfing", "強気包み足", bullish_engulfing),
    Strategy("hammer", "ハンマー", hammer),
    Strategy("morning_star", "明けの明星", morning_star),
    Strategy("piercing_line", "切り込み線", piercing_line),
    Strategy("bullish_harami", "強気はらみ線", bullish_harami),
    Strategy("double_bottom", "二点底", double_bottom),
    # 板と約定の流れによる検出。足のパターンではないので、ここでは判定しない（flow.py が行う）。
    Strategy("squeeze", "スクイーズ", lambda bars, p: None),
    # FVG（fvg.py が判定する）。通知のみで、売買の対象にはしない。
    Strategy("fvg_hold", "FVG反発", lambda bars, p: None),
    Strategy("fvg_break", "FVG割れ", lambda bars, p: None),
    # 大口の売り板（wall.py が判定する）。通知のみ。
    Strategy("wall_break", "売り板突破", lambda bars, p: None),
    Strategy("wall_approach", "売り板接近", lambda bars, p: None),
    # 流動性スイープ（sweep.py が判定する）。通知のみ。
    Strategy("liq_sweep", "流動性ブレイク", lambda bars, p: None),
    # 寄り付き後の押し目買い（opening.py が判定する）。通知のみ。
    Strategy("open_dip", "寄り付き押し目", lambda bars, p: None),
    # 以前は場面ごとに通知を分けていた（今は流動性ブレイクに一本化。過去の記録の表示用に名前だけ残す）
    Strategy("bos_dip", "内部流動性ブレイク", lambda bars, p: None),
    # 下の外部流動性（当日安値・大きな波の安値・前日安値）を割って戻した形（sweep.py が判定する）。通知のみ。
    Strategy("ext_liq", "外部流動性ブレイク", lambda bars, p: None),
)
EXT_STRATEGY = "ext_liq"
BOS_STRATEGY = "bos_dip"
OPEN_STRATEGY = "open_dip"
# 管理画面で通知のオン・オフを選べるもの（初期状態はすべてオン）
NOTIFY_CHOICES = ("tweezer_bottom", "squeeze", "fvg_hold", "fvg_break", "wall_break", "liq_sweep", "open_dip")
SWEEP_STRATEGY = "liq_sweep"
WALL_STRATEGIES = {"break": "wall_break", "approach": "wall_approach"}
FLOW_STRATEGY = "squeeze"
FVG_STRATEGIES = {"hold": "fvg_hold", "break": "fvg_break"}
STRATEGY_BY_ID = {s.id: s for s in STRATEGIES}
