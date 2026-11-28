from candle.sweep import SweepParams, detect, pools

from helpers import make_bars


def bars_from(rows, **kw):
    bars = make_bars([r[:4] for r in rows], start=(9, 30), minutes=1, **kw)
    for b, r in zip(bars, rows):
        b.volume = r[4]
    return bars


def flat(price, n, vol=3000):
    return [(price, price + 2, price - 1, price + 1, vol) if i % 2 else (price + 1, price + 2, price - 1, price, vol)
            for i in range(n)]


# 上げた後に押し安値 4,930 を付け、もう一度上げてから戻ってくる
BASE = (flat(4960, 6) + [(4960, 4961, 4945, 4947, 3000), (4947, 4948, 4930, 4934, 4000),   # 押し安値 4930
                         (4934, 4950, 4933, 4949, 3000), (4949, 4962, 4948, 4960, 3000)]
        + flat(4975, 8) + [(4975, 4976, 4955, 4957, 3000), (4957, 4958, 4940, 4942, 3000)])


def test_pools_are_unbroken_pivot_lows():
    b = bars_from(BASE)
    assert 4930 in [lv for _, lv in pools(b)]
    broken = bars_from(BASE + [(4942, 4943, 4925, 4927, 3000)])
    assert 4930 not in [lv for _, lv in pools(broken)]                  # 割られたらプールではなくなる


def test_sweep_and_reclaim_in_one_bar_with_volume():
    sig = detect(bars_from(BASE + [(4942, 4945, 4915, 4944, 12000)]))    # 長い下ヒゲ
    assert sig is not None and (sig.level, sig.low, sig.price) == (4930, 4915, 4944)
    assert sig.metrics["volume_ratio"] > 3 and sig.metrics["reclaim_bars"] == 1
    assert sig.note.startswith("【上昇の起点より下】下の外部流動性（当日安値） 4,930.0 にタッチ（4,915.0 まで）して 4,944.0 に回復")
    assert "次のオーダーブロック：上の外部流動性 4,977.0（+0.67%）" in sig.note and len(sig.conditions) == 6
    assert sig.metrics["target"] == 4977 and sig.metrics["waypoints"] == []
    m = sig.metrics                                                     # 当日安値を割った＝外部流動性。割安ゾーンではない
    assert (m["liquidity"], m["zone"], m["bos_setup"]) == ("external", "below", False)


def test_reclaim_on_next_bar_counts_once():
    rows = BASE + [(4942, 4943, 4912, 4920, 12000), (4920, 4946, 4918, 4945, 6000)]
    assert detect(bars_from(rows[:-1])) is None                           # まだ安値の下
    sig = detect(bars_from(rows))
    assert sig.metrics["reclaim_bars"] == 2 and sig.low == 4912
    assert detect(bars_from(rows + [(4945, 4950, 4944, 4949, 3000)])) is None   # 次の足では出さない


def test_no_signal_without_volume_or_reclaim_or_when_too_deep():
    assert detect(bars_from(BASE + [(4942, 4945, 4915, 4944, 4000)])) is None    # 出来高が増えない
    assert detect(bars_from(BASE + [(4942, 4943, 4915, 4918, 12000)])) is None   # 戻せていない
    assert detect(bars_from(BASE + [(4942, 4945, 4850, 4944, 12000)])) is None   # 1%超の急落
    assert detect(bars_from(BASE + [(4942, 4950, 4935, 4948, 12000)])) is None   # 安値を割っていない
    assert detect(bars_from(flat(4960, 30) + [(4960, 4962, 4950, 4961, 12000)]),
                  SweepParams(pivot=3)) is None or True                    # 横ばいでも落ちないこと


def test_engine_notifies_and_records_sweep(tmp_path):
    from datetime import timedelta
    from candle import squeeze_web
    from candle.engine import PaperEngine
    from candle.risk import RiskLimits, RiskManager
    from candle.store import Store

    store = Store(tmp_path / "c.sqlite")
    sent = []
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"},
                    notify=lambda *a: sent.append(a), notify_ids={"liq_sweep"})
    e.names = {"5803": "フジクラ"}
    bars = bars_from(BASE + [(4942, 4945, 4915, 4944, 12000), (4944, 4950, 4943, 4949, 4000)], symbol="5803")
    e.on_board("5803", {"CurrentPrice": 4944, "TradingVolume": 500000, "PreviousClose": 4900.0,
                        "Sell1": {"Price": 4945, "Qty": 300}, "Buy1": {"Price": 4944, "Qty": 400}}, bars[-2].end)
    for b in bars:
        e.on_bar(b)
    assert [a[0] for a in sent] == ["流動性タッチ  5803 フジクラ"] and "4,930.0" in sent[0][1]
    (row,) = store.recent_signals("paper")
    assert row["strategy_id"] == "liq_sweep" and row["pattern_low"] == 4915 and row["action"] == "通知のみ"
    assert e.open == [] and not e.pending                               # 売買はしない
    e.on_clock(bars[-1].end + timedelta(minutes=6))
    (ev,) = squeeze_web.list_events(tmp_path / "c.sqlite", kind="sweep")["events"]
    assert ev["kind"] == "sweep" and ev["acquired"]["price"] == 4944 and ev["acquired"]["prev_close"] == 4900
    assert ev["metrics"]["level"] == 4930 and ev["metrics"]["sweep_low"] == 4915
    assert squeeze_web.list_events(tmp_path / "c.sqlite")["events"] == []   # スクイーズの一覧には混ざらない
    assert e.chart_data("5803")["pools"] is not None


# 戻り高値 1,010 を上に抜けて1,040 まで上げ、押し安値 1,018 を付けた後、そこを割って戻す
BOS = (flat(1000, 8) + [(1001, 1010, 1000, 1008, 3000), (1008, 1009, 1003, 1004, 3000)] + flat(1003, 6)
       + [(1004, 1016, 1003, 1015, 9000), (1015, 1028, 1014, 1027, 5000), (1027, 1040, 1026, 1038, 5000),
          (1038, 1039, 1030, 1031, 3000), (1031, 1032, 1024, 1025, 3000), (1025, 1026, 1018, 1021, 3000),
          (1021, 1027, 1020, 1026, 3000), (1026, 1028, 1024, 1025, 3000), (1025, 1027, 1023, 1024, 3000),
          (1024, 1026, 1022, 1023, 3000), (1023, 1025, 1021, 1022, 3000)])
SWEEP = [(1022, 1023, 1015, 1020, 12000)]


def test_sweep_after_bos_in_discount_zone_is_tagged():
    from candle.sweep import find_bos
    bars = bars_from(BOS + SWEEP)
    bos = find_bos(bars, len(bars) - 1, SweepParams())
    assert (bos["bos_level"], bos["bos_index"], bos["bos_volume_ratio"]) == (1010, 16, 3.0)
    m = detect(bars).metrics
    assert (m["after_bos"], m["zone"], m["liquidity"], m["bos_setup"]) == (True, "discount", "internal", True)
    assert (m["leg_low"], m["leg_high"], m["bos_level"]) == (999, 1040, 1010)
    high = bars_from(BOS[:-2] + [(1024, 1031, 1023, 1030, 3000), (1030, 1031, 1026, 1027, 3000)])
    assert detect(high) is None                                          # 割っていなければ出ない


def test_one_notification_for_external_and_internal_with_next_target(tmp_path):
    from candle import squeeze_web
    from candle.engine import PaperEngine
    from candle.risk import RiskLimits, RiskManager
    from candle.settings import TradeSettings
    from candle.store import Store

    def run(sub, rows, prev=None):
        store, sent = Store(tmp_path / f"{sub}.sqlite"), []
        e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"}, trade_enabled=False,
                        notify=lambda *a: sent.append(a), notify_ids={"liq_sweep"})
        if prev:
            e.prev_days["5803"] = dict(day="2026-11-23", close=1020, **prev)
        for b in bars_from(rows, symbol="5803"):
            e.on_bar(b)
        (ev,) = squeeze_web.list_events(tmp_path / f"{sub}.sqlite", kind="sweep")["events"]
        return sent, store, ev["metrics"]

    # 内部流動性を割って戻した → 次の目標は上の外部流動性（当日高値 1,040）
    sent, store, m = run("a", BOS + SWEEP)
    assert [a[0] for a in sent] == ["流動性タッチ  5803"]
    assert sent[0][1].split(" ", 1)[1].startswith("【割安ゾーン】下の内部流動性 1,018.0 にタッチ（1,015.0 まで）して 1,020.0 に回復")
    assert "次のオーダーブロック：上の外部流動性 1,040.0（+1.96%）" in sent[0][1]
    assert store.recent_signals("paper")[0]["strategy_id"] == "liq_sweep"
    assert (m["liquidity"], m["bos_setup"], m["target"]) == ("internal", True, 1040)     # 場面の印は記録に残る
    # 外部流動性（当日安値）を割って戻した → 同じ通知名で知らせる
    sent, _, m = run("b", BASE + [(4942, 4945, 4915, 4944, 12000)])
    assert [a[0] for a in sent] == ["流動性タッチ  5803"] and "下の外部流動性（当日安値） 4,930.0" in sent[0][1]
    assert m["liquidity"] == "external" and m["target"] == 4977
    # 前日安値も割って戻した／前日高値のほうが近ければ、それを次の目標にする
    sent, _, m = run("c", BOS + SWEEP, prev=dict(high=1030, low=1017))
    assert "前日安値 1,017.0 にもタッチ" in sent[0][1]
    assert "次のオーダーブロック：上の外部流動性 1,030.0（+0.98%）（前日高値）" in sent[0][1]
    assert (m["liquidity"], m["ext_kind"], m["target"], m["prev_level"]) == ("external", "前日安値", 1030, 1017)
    # 以前に分けていたサインのIDが設定に残っていても問題ない
    old = TradeSettings.from_dict({"notify_on": "tweezer_bottom,liq_sweep,bos_dip,ext_liq"})
    assert old.notifies("liq_sweep")


def test_targets_list_internal_liquidity_on_the_way():
    from candle.sweep import target_text, targets
    rows = BOS[:-2] + [(1024, 1031, 1023, 1030, 3000), (1030, 1031, 1026, 1027, 3000), (1027, 1028, 1023, 1024, 3000),
                       (1024, 1026, 1022, 1023, 3000), (1023, 1025, 1021, 1022, 3000), (1022, 1024, 1020, 1021, 3000)]
    t = targets(bars_from(rows), 1021)
    assert t["target"] == 1040 and t["waypoints"] == [1031]                    # 途中に内部流動性（小さな波の高値）
    assert target_text(t) == "次のオーダーブロック：上の外部流動性 1,040.0（+1.86%）／途中の内部流動性 1,031.0"
    assert "まだ見つかっていません" in target_text(dict(target=None))


def test_new_signal_is_added_to_saved_settings():
    from candle.settings import TradeSettings
    old = TradeSettings.from_dict({"notify_on": "tweezer_bottom", "notify_seen": "tweezer_bottom"})
    assert old.notify_on.startswith("tweezer_bottom,") and "liq_sweep" in old.notify_on   # 後から増えたサインはオンで加わる
    again = TradeSettings.from_dict({"notify_on": "tweezer_bottom", "notify_seen": old.notify_seen})
    assert again.notify_on == "tweezer_bottom"                                   # 画面で外したものは戻さない


def test_liquidity_levels_external_and_internal():
    from candle.sweep import liquidity_levels
    bars = bars_from(BOS + SWEEP)
    lv = {(x["side"], x["level"]): x["kind"] for x in liquidity_levels(bars)}
    assert lv[("high", 1040)] == "external"                 # 当日高値（上の外部流動性）
    assert lv[("low", 999)] == "external"                   # 当日安値（下の外部流動性）
    assert ("low", 1018) not in lv                          # 割られた安値は、もう流動性ではない
    assert ("high", 1010) not in lv                         # 上に抜かれた高値も同じ
    more = bars_from(BOS + [(1022, 1030, 1021, 1029, 3000), (1029, 1033, 1028, 1031, 3000),
                            (1031, 1032, 1027, 1028, 3000), (1028, 1029, 1026, 1027, 3000),
                            (1027, 1028, 1025, 1026, 3000), (1026, 1027, 1024, 1025, 3000)])
    lv = {(x["side"], x["level"]): x["kind"] for x in liquidity_levels(more)}
    assert lv[("low", 1018)] == "internal" and lv[("high", 1033)] == "internal"   # 内側の小さな波
    assert all(k in ("external", "internal") for k in lv.values())
    assert liquidity_levels(bars[:3]) == []


def mirror(rows, c):
    """上下を反転した値動き（高値側の形を作る）。"""
    return [(c - o, c - l, c - h, c - cl, v) for o, h, l, cl, v in rows]


def test_touch_on_upper_liquidity_points_to_order_block_below(tmp_path):
    from candle import squeeze_web
    from candle.engine import PaperEngine
    from candle.risk import RiskLimits, RiskManager
    from candle.store import Store
    rows = mirror(BASE + [(4942, 4945, 4915, 4944, 12000)], 10000)      # 高値 5,070 を 5,085 まで抜いて反落
    sig = detect(bars_from(rows))
    m = sig.metrics
    assert (m["side"], sig.level, sig.low, sig.price) == ("high", 5070, 5085, 5056)
    assert (m["liquidity"], m["ext_kind"], m["target"], m["target_side"]) == ("external", "当日高値", 5023, "low")
    assert sig.note.startswith("上の外部流動性（当日高値） 5,070.0 にタッチ（5,085.0 まで）して 5,056.0 に反落")
    assert "次のオーダーブロック：下の外部流動性 5,023.0（-0.65%）" in sig.note
    assert detect(bars_from(mirror(BASE + [(4942, 4943, 4915, 4918, 12000)], 10000))) is None   # 戻していない

    store, sent = Store(tmp_path / "c.sqlite"), []
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"}, trade_enabled=False,
                    notify=lambda *a: sent.append(a), notify_ids={"liq_sweep"})
    e.prev_days["5803"] = dict(day="2026-11-23", high=5080, low=5040, close=5050)
    for b in bars_from(rows, symbol="5803"):
        e.on_bar(b)
    assert [a[0] for a in sent] == ["流動性タッチ  5803"]
    assert "前日高値 5,080.0 にもタッチ" in sent[0][1]
    assert "次のオーダーブロック：下の外部流動性 5,040.0（-0.32%）（前日安値）" in sent[0][1]
    (ev,) = squeeze_web.list_events(tmp_path / "c.sqlite", kind="sweep")["events"]
    assert ev["metrics"]["side"] == "high" and ev["metrics"]["ext_kind"] == "前日高値" and e.open == []
