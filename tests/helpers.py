from datetime import datetime, timedelta

from candle.bars import JST, Bar
from candle.patterns import expected_next


def make_bars(ohlc, symbol="T", start=(9, 20), day=24, minutes=5):
    """(始値, 高値, 安値, 終値) の並びから連続した足を作る。"""
    ts = datetime(2026, 11, day, *start, tzinfo=JST)
    out = []
    for o, h, l, c in ohlc:
        out.append(Bar(symbol, ts, minutes, o, h, l, c, 1000, 5, True))
        ts = expected_next(ts, minutes) or ts + timedelta(minutes=minutes)
    return out


# 6本続けて下げる前置き（終値 1060 → 1010）
DOWN = [(1070 - 10 * i, 1072 - 10 * i, 1058 - 10 * i, 1060 - 10 * i) for i in range(6)]
