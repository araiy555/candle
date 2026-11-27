from candle import mtf

from helpers import make_bars


def wave(points, per=3):
    """折れ線 points を、1区間 per 本の1分足にする（高値・安値は終値の±1）。"""
    rows, p = [], points[0]
    for q in points[1:]:
        for k in range(1, per + 1):
            c = p + (q - p) * k / per
            o = rows[-1][3] if rows else p
            rows.append((o, max(o, c) + 1, min(o, c) - 1, c))
        p = q
    return make_bars(rows, "T", start=(9, 0), minutes=1)


def test_aggregate_builds_longer_bars():
    bars = make_bars([(100 + i, 102 + i, 99 + i, 101 + i) for i in range(12)], "T", start=(9, 0), minutes=1)
    for b in bars:
        b.volume = 10
    five = mtf.aggregate(bars, 5)
    assert [(b.start.minute, b.open, b.high, b.low, b.close, b.volume) for b in five] == [
        (0, 100, 106, 99, 105, 50), (5, 105, 111, 104, 110, 50), (10, 110, 113, 109, 112, 20)]
    assert five[0].minutes == 5 and len(mtf.aggregate(bars, 15)) == 1


def test_trend_from_swing_highs_and_lows():
    assert mtf.trend(wave([100, 110, 104, 116, 109, 122, 115])) == "up"        # 高値も安値も切り上げ
    assert mtf.trend(wave([122, 112, 118, 106, 113, 100, 106])) == "down"      # 高値も安値も切り下げ
    assert mtf.trend(wave([100, 110, 102, 109, 101, 111, 103])) == "range"
    assert mtf.trend(wave([100, 110])) is None                                 # 波が足りない


def test_summary_and_engine_chart_frames():
    from candle.engine import PaperEngine
    from candle.risk import RiskLimits, RiskManager
    from candle.store import Store
    bars = wave([1000, 1030, 1012, 1048, 1027, 1066, 1045, 1080], per=8)
    s = mtf.summary(bars, 1)
    assert [x["minutes"] for x in s] == [1, 5, 15] and s[1]["trend"] == "up" and s[1]["label"] == "上昇"
    assert mtf.code(bars, 1).startswith("5:up,15:")
    store = Store(":memory:")
    store.save_bars(make_bars([(990, 1005, 985, 1000)] * 3, "T", start=(9, 0), day=23, minutes=1))
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"})
    e.load_history(["T"], 1, today=bars[0].start.date())
    assert e.prev_days["T"] == dict(day="2026-11-23", high=1005, low=985, close=1000)
    for b in bars:
        e.on_bar(b)
    one, five = e.chart_data("T"), e.chart_data("T", 5)
    assert one["minutes"] == 1 and len(one["bars"]) == 56 and one["prev_day"]["high"] == 1005
    assert five["minutes"] == 5 and five["base_minutes"] == 1 and len(five["bars"]) == 12
    assert [f["minutes"] for f in five["frames"]] == [1, 5, 15] and five["liquidity"] is not None
    assert e.htf("T") == mtf.code(bars, 1)
