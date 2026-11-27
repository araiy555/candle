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
    for b in bars:
        e.on_bar(b)
    assert e.prev_days["T"] == dict(day="2026-11-23", high=1005, low=985, close=1000)
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
    for b in today:
        e.on_bar(b)
    assert len(e.hourly_past["T"]) == 18 and e.hourly_past["T"][0].minutes == 60        # 3日 × 6本
    hx = e.hourly_external("T")
    assert min(x["level"] for x in hx if x["side"] == "low") == 999                      # 数日間の安値
    assert any(x["side"] == "high" and x["level"] == 1061 for x in hx)
    hour = e.chart_data("T", 60)
    assert hour["minutes"] == 60 and len(hour["bars"]) == 19
    one = e.chart_data("T")
    assert one["frames"][-1]["name"] == "1時間足" and one["frames"][-1]["bars"] == 19
    ext = {(x["side"], x["level"]) for x in one["liquidity"] if x["kind"] == "external"}
    assert ("low", 999) in ext and ("high", 1061) in ext            # 数日間の高値・安値も、同じ「外部流動性」として出る
    assert e.htf("T").split(",")[-1].startswith("60:")


def test_chart_shows_last_trading_day_when_today_has_no_bars(tmp_path):
    from datetime import date
    from candle.engine import PaperEngine
    from candle.risk import RiskLimits, RiskManager
    from candle.store import Store
    store = Store(tmp_path / "c.sqlite")
    store.save_bars(make_bars([(1000 + i, 1003 + i, 999 + i, 1001 + i) for i in range(40)], "T", start=(9, 0), day=26, minutes=1))
    store.save_bars(make_bars([(1100 + i, 1103 + i, 1099 + i, 1101 + i) for i in range(40)], "T", start=(9, 0), day=27, minutes=1))
    sent = []
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"}, trade_enabled=False,
                    notify=lambda *a: sent.append(a))
    e.load_history(["T"], 1, today=date(2026, 11, 28))                  # 土曜。本日の足は無い
    c = e.chart_data("T")
    assert (c["shown_day"], c["live"], len(c["bars"])) == ("2026-11-27", False, 40)
    assert c["prev_day"]["day"] == "2026-11-26" and len(e.history["T"]) == 0 and sent == []
    assert len(e.chart_data("T", 60)["bars"]) == 2                      # 26日と27日の1時間足
    first = make_bars([(1150, 1152, 1149, 1151)], "T", start=(9, 0), day=30, minutes=1)[0]
    e.on_bar(first)                                                     # 月曜の最初の足が来た
    c = e.chart_data("T")
    assert (c["shown_day"], c["live"], len(c["bars"])) == ("2026-11-30", True, 1)
    assert c["prev_day"] == dict(day="2026-11-27", high=1142, low=1099, close=1140)
    assert len(e.chart_data("T", 60)["bars"]) == 3 and "T" not in e.last_day


def test_hourly_bars_follow_the_session_and_trend_turns_on_a_break():
    day = make_bars([(100, 101, 99, 100)] * 330, "T", start=(9, 0), minutes=1)       # 9:00〜15:30
    hours = [(b.start.hour, b.start.minute) for b in mtf.aggregate(day, 60)]
    assert hours == [(9, 0), (10, 0), (11, 0), (12, 30), (13, 30), (14, 30)]
    down = wave([122, 112, 118, 106, 113, 100, 106])                                 # 切り下げ → 下降
    assert mtf.trend(down) == "down"
    turned = wave([122, 112, 118, 106, 113, 100, 106, 120])                          # 最後の波の高値 113 を上に抜けた
    assert mtf.trend(turned) == "up"
    assert mtf.trend(wave([100, 110, 104, 116, 109, 122, 115, 100])) == "down"       # 逆も同じ


def test_imported_history_gives_upper_external_liquidity(tmp_path):
    """自分で受信した数日分より上に高値が無くても、取り込んだ過去の日足・1時間足から上の外部流動性を出す。"""
    import time
    from datetime import date, datetime
    from candle.app import App
    from candle.bars import JST, Bar
    from candle.config import Config
    from candle.store import Store
    from candle.yahoo import parse_history
    cfg = Config(data_dir=tmp_path)
    app = App(cfg)
    store = Store(tmp_path / "c.sqlite")
    app.setup(store)
    # 自分で受信した分：前日 14,300〜14,715、当日は 14,900 まで上げている（数日間の高値を更新中）
    store.save_bars(make_bars([(14500, 14715, 14300, 14310)] * 30, "6861", start=(9, 0), day=26, minutes=1))
    today = make_bars([(14850 + i, 14860 + i, 14840 + i, 14855 + i) for i in range(40)], "6861", start=(9, 0), day=27, minutes=1)
    e = app.engine
    e.load_history(["6861"], 1, today=date(2026, 11, 27))
    for b in today:
        e.on_bar(b)
    assert [x for x in e.chart_data("6861")["liquidity"] if x["side"] == "high" and x["level"] > 14900] == []

    def fake(symbol, minutes):                       # 過去の日足：9月に 15,800 の高値、その後は下
        if minutes == 60:
            return []
        highs = [15200, 15500, 15800, 15400, 15000, 14800, 14600, 14900, 14700, 14500, 14600, 14400, 14700, 14600]
        return [Bar(symbol, datetime(2026, 9, 1 + i, 9, 0, tzinfo=JST), 1440, h - 150, h, h - 300, h - 100, 1e6, 0, True)
                for i, h in enumerate(highs)]
    app.fetch_history(["6861"], fetcher=fake, pause=0)
    for _ in range(50):
        if not app._hist_queue.empty():
            break
        time.sleep(0.02)
    app._apply_history()
    ups = sorted(x["level"] for x in e.chart_data("6861")["liquidity"] if x["side"] == "high" and x["kind"] == "external")
    assert 15800 in ups                                                # 上の外部流動性として出る
    assert 15800 in {x["level"] for x in e.chart_data("6861", 60)["liquidity"] if x["side"] == "high"}
    assert (tmp_path / "history_fetched.json").exists() and len(e.daily["6861"]) == 14
    payload = {"chart": {"result": [{"timestamp": [1790553600, 1790640000], "indicators": {"quote": [{
        "open": [100, None], "high": [110, 1], "low": [95, 1], "close": [105, 1], "volume": [1000, 0]}]}}]}}
    (bar,) = parse_history("T", payload, 1440)                         # 値の欠けた日は捨てる
    assert (bar.minutes, bar.high, bar.start.hour) == (1440, 110, 9)
