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
    assert [x["minutes"] for x in s] == [1, 5, 15, 60] and s[1]["trend"] == "up" and s[1]["label"] == "上昇"
    assert s[3]["name"] == "1時間足"
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
    assert [f["minutes"] for f in five["frames"]] == [1, 5, 15, 60] and five["liquidity"] is not None
    assert e.htf("T") == mtf.code(bars, 1)


def test_direction_is_added_to_notifications_and_watchlist():
    from candle.engine import PaperEngine
    from candle.risk import RiskLimits, RiskManager
    from candle.store import Store
    sent = []
    e = PaperEngine(Store(":memory:"), RiskManager(RiskLimits()), enabled={"tweezer_bottom"},
                    trade_enabled=False, notify=lambda *a: sent.append(a))
    bars = wave([1000, 1030, 1012, 1048, 1027, 1066, 1045, 1080], per=8)
    for b in bars:
        e.on_bar(b)
    assert e.direction_text("T").startswith("方向：5分足 上昇")
    e._notify("テスト  T", "本文", "T")
    assert sent[-1][1].startswith("本文　方向：5分足 上昇") and sent[-1][2] == "T"
    assert e.direction_text("X") == ""                                     # 足が無い銘柄には付けない
    e.watchlist = ["T"]
    e.on_clock(bars[-1].end)
    assert e.snapshot["watchlist"][0]["frames"][0] == {"minutes": 5, "trend": "up"}


def test_hourly_frame_uses_past_days_and_its_external_liquidity(tmp_path):
    from datetime import timedelta
    from candle.engine import PaperEngine
    from candle.risk import RiskLimits, RiskManager
    from candle.store import Store
    store = Store(tmp_path / "c.sqlite")
    # 過去3日分：1,000 → 1,060 → 1,010（安値 1,008）→ 1,050。数日間の安値 1,008 が下の外部流動性
    pts = [1000, 1060, 1040, 1055, 1010, 1030, 1020, 1050, 1035, 1045]
    for d, seg in zip((19, 20, 23), (pts[0:4], pts[3:7], pts[6:10])):
        rows, p0 = [], seg[0]
        for q in seg[1:]:
            for k in range(1, 101):
                c = p0 + (q - p0) * k / 100
                o = rows[-1][3] if rows else p0
                rows.append((o, max(o, c) + 1, min(o, c) - 1, c))
            p0 = q
        store.save_bars(make_bars(rows, "T", start=(9, 0), day=d, minutes=1))
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"}, trade_enabled=False)
    today = make_bars([(1045, 1046, 1040, 1041)] * 30, "T", start=(9, 0), day=24, minutes=1)
    e.load_history(["T"], 1, today=today[0].start.date())
    assert len(e.hourly_past["T"]) == 18 and e.hourly_past["T"][0].minutes == 60        # 3日 × 6本
    for b in today:
        e.on_bar(b)
    hx = e.hourly_external("T")
    assert min(x["level"] for x in hx if x["side"] == "low") == 999                      # 数日間の安値
    assert any(x["side"] == "high" and x["level"] == 1061 for x in hx)
    hour = e.chart_data("T", 60)
    assert hour["minutes"] == 60 and len(hour["bars"]) == 19 and hour["htf_liquidity"] == []
    one = e.chart_data("T")
    assert one["frames"][-1]["name"] == "1時間足" and one["frames"][-1]["bars"] == 19
    assert {x["level"] for x in one["htf_liquidity"]} >= {999, 1061}
    assert e.htf("T").split(",")[-1].startswith("60:")
