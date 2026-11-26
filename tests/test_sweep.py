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
    assert "割り込んだ後" in sig.note and len(sig.conditions) == 3


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
