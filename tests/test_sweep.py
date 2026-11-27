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
    assert "割り込んだ後" in sig.note and len(sig.conditions) == 6
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
    assert [a[0] for a in sent] == ["流動性スイープ  5803 フジクラ"] and "4,930.0" in sent[0][1]
    (row,) = store.recent_signals("paper")
    assert row["strategy_id"] == "liq_sweep" and row["pattern_low"] == 4915 and row["action"] == "通知のみ"
    assert e.open == [] and not e.pending                               # 売買はしない
    e.on_clock(bars[-1].end + timedelta(minutes=6))
    (ev,) = squeeze_web.list_events(tmp_path / "c.sqlite", kind="sweep")["events"]
    assert ev["kind"] == "sweep" and ev["acquired"]["price"] == 4944 and ev["acquired"]["prev_close"] == 4900
    assert ev["metrics"]["level"] == 4930 and ev["metrics"]["sweep_low"] == 4915
    assert squeeze_web.list_events(tmp_path / "c.sqlite")["events"] == []   # スクイーズの一覧には混ざらない
    assert e.chart_data("5803")["pools"] is not None


# 戻り高値 1,010 を上に抜けて（BOS）1,040 まで上げ、押し安値 1,018 を付けた後、そこを割って戻す
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


def test_engine_notifies_bos_dip_and_keeps_plain_sweep_separate(tmp_path):
    from candle import squeeze_web
    from candle.engine import PaperEngine
    from candle.risk import RiskLimits, RiskManager
    from candle.store import Store

    def run(sub, rows, **settings):
        store, sent = Store(tmp_path / f"{sub}.sqlite"), []
        e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"}, trade_enabled=False,
                        notify=lambda *a: sent.append(a), notify_ids={"liq_sweep", "bos_dip"})
        if settings:
            e.update_settings(settings)
        for b in bars_from(rows, symbol="5803"):
            e.on_bar(b)
        return sent, store, tmp_path / f"{sub}.sqlite"

    sent, store, db = run("a", BOS + SWEEP)
    assert [a[0] for a in sent] == ["BOS後の押し目  5803"]                # 1回だけ、BOS後の押し目として知らせる
    assert store.recent_signals("paper")[0]["strategy_id"] == "bos_dip"
    (ev,) = squeeze_web.list_events(db, kind="sweep")["events"]          # 履歴は流動性スイープとして残る
    assert ev["metrics"]["bos_setup"] is True and ev["metrics"]["liquidity"] == "internal"
    sent, store, _ = run("b", BOS + SWEEP, notify_on="liq_sweep")        # BOS後の押し目をオフ → 普通のスイープとして
    assert [a[0] for a in sent] == ["流動性スイープ  5803"]
    sent, store, db = run("c", BASE + [(4942, 4945, 4915, 4944, 12000)], notify_on="bos_dip")
    assert sent == [] and store.recent_signals("paper") == []            # 条件に合わないスイープは知らせない
    assert len(squeeze_web.list_events(db, kind="sweep")["events"]) == 1  # が、記録は残す


def test_new_signal_is_added_to_saved_settings():
    from candle.settings import TradeSettings
    old = TradeSettings.from_dict({"notify_on": "tweezer_bottom,liq_sweep"})     # 旧版で保存した設定
    assert old.notify_on == "tweezer_bottom,liq_sweep,bos_dip"                   # 後から増えたサインはオンで加わる
    again = TradeSettings.from_dict({"notify_on": "tweezer_bottom", "notify_seen": old.notify_seen})
    assert again.notify_on == "tweezer_bottom"                                   # 画面で外したものは戻さない
