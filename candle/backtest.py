"""戦略別バックテスト（第3段階）。保存済みの5分足を使い、戦略ごとに独立して評価する。

約定の仮定:
- サイン確定の次の足の始値で買う（スリッページ分だけ不利に約定）。
- 同じ足の中で損切りと高値更新が両方起きた場合は、損切りが先に起きたとみなす（保守的）。
- 損切り価格を下回って始まった足は、その始値で約定したとみなす。
- 数量は1単元（100株）固定。資金上限は戦略の比較を歪めるため適用しない。
- 持ち越しはせず、指定時刻以降の足の終値で決済する。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import time

from .bars import Bar
from .patterns import STRATEGIES, Params, Strategy, contiguous_tail, expected_next
from .risk import RiskLimits
from .trade import Trade, summarize


def run_symbol(bars: list[Bar], strategy: Strategy, params: Params, limits: RiskLimits,
               slippage: float = 0.0005, exit_after: time = time(15, 20)) -> list[Trade]:
    trades: list[Trade] = []
    pos: Trade | None = None
    pending: tuple[Bar, float] | None = None  # (サイン足, パターン安値)
    for i, bar in enumerate(bars):
        prev = bars[i - 1] if i else None
        contiguous = prev is not None and expected_next(prev.start, prev.minutes) == bar.start

        if pending is not None:
            sig, low = pending
            pending = None
            if contiguous and bar.complete and bar.start.time() < exit_after:
                entry = bar.open * (1 + slippage)
                risk = entry - low
                if entry * limits.min_risk_pct <= risk <= entry * limits.max_risk_pct:
                    pos = Trade("backtest", strategy.id, bar.symbol, sig.end, bar.start, entry,
                                limits.unit, low, low, high=entry)

        if pos is not None:
            if bar.start.date() != pos.entry_time.date():   # データ欠落で日をまたいだ
                last = bars[i - 1]
                pos.close(last.end, last.close * (1 - slippage), "データ終端")
            elif bar.low < pos.stop:
                fill = min(pos.stop, bar.open) * (1 - slippage)
                reason = "初期損切り" if pos.stop == pos.initial_stop else "トレーリング"
                pos.close(bar.end, fill, reason)
            else:
                pos.trail(bar.high)
                last_of_day = i + 1 == len(bars) or bars[i + 1].start.date() != bar.start.date()
                if bar.start.time() >= exit_after or last_of_day:
                    pos.close(bar.end, bar.close * (1 - slippage), "時間決済")
            if not pos.is_open:
                trades.append(pos)
                pos = None

        if pos is None and bar.complete and bar.start.time() < exit_after:
            window = contiguous_tail(bars[max(0, i - 60):i + 1])
            # 当日の足だけで判定する
            window = [b for b in window if b.start.date() == bar.start.date()]
            low = strategy.detect(window, params) if window else None
            if low is not None:
                pending = (bar, low)
    return trades


def run_backtest(bars_by_symbol: dict[str, list[Bar]], params: Params | None = None,
                 limits: RiskLimits | None = None, slippage: float = 0.0005
                 ) -> dict[str, list[Trade]]:
    params, limits = params or Params(), limits or RiskLimits()
    out: dict[str, list[Trade]] = defaultdict(list)
    for strategy in STRATEGIES:
        for bars in bars_by_symbol.values():
            out[strategy.id].extend(run_symbol(bars, strategy, params, limits, slippage))
    return out


def format_report(results: dict[str, list[Trade]], days: int, symbols: int) -> str:
    def f(v, fmt):
        return "-" if v is None else format(v, fmt)

    lines = [f"対象: {symbols}銘柄 / {days}営業日（1単元固定・スリッページ込み）", "",
             f"{'戦略':<10}{'取引数':>6}{'勝率':>8}{'PF':>7}{'期待損益(円)':>12}{'期待値(R)':>10}"
             f"{'合計(円)':>12}{'平均保有(分)':>10}"]
    for s in STRATEGIES:
        st = summarize(results.get(s.id, []))
        lines.append(f"{s.name:<10}{st['trades']:>8}{f(st['win_rate'], '.1%'):>10}"
                     f"{f(st['pf'], '.2f'):>7}{f(st['expectancy'], ',.0f'):>14}"
                     f"{f(st['expectancy_r'], '+.2f'):>12}{st['total']:>14,.0f}"
                     f"{f(st['avg_hold_min'], '.0f'):>12}")
    return "\n".join(lines)
