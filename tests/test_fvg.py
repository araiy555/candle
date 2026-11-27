from candle.fvg import FvgParams, detect, find_fvgs, poc, structure

from helpers import make_bars


def bars_from(rows, **kw):
    """(始値, 高値, 安値, 終値, 出来高) の並びから1分足を作る。"""
    bars = make_bars([r[:4] for r in rows], start=(9, 30), minutes=1, **kw)
    for b, r in zip(bars, rows):
        b.volume = r[4]
    return bars


# 上昇前のレンジ（1000前後、出来高は1001付近に集中）
RANGE = [(1000, 1002, 999, 1001, 5000) if i % 2 else (1001, 1002, 1000, 1000, 5000) for i in range(14)]
# 上昇：2本目が大陽線で、1本目の高値(1003)と3本目の安値(1006)の間にFVG（1003〜1006）
LEG = [(1000, 1003, 998, 1002, 3000), (1003, 1010, 1003, 1009, 9000), (1009, 1013, 1006, 1012, 6000),
       (1012, 1018, 1011, 1017, 5000), (1017, 1024, 1016, 1023, 5000), (1023, 1030, 1022, 1029, 5000)]
# 押し目：FVGの上端まで下げる
PULL = [(1029, 1029, 1022, 1023, 2000), (1023, 1024, 1016, 1017, 2000), (1017, 1018, 1010, 1011, 2000),
        (1011, 1012, 1005, 1007, 2000)]


def test_find_fvgs_and_poc():
    b = bars_from(RANGE + LEG)
    assert [(k - len(RANGE), lo, hi) for k, lo, hi in find_fvgs(b, len(RANGE), len(b) - 1, 0.0003)][0] == (1, 1003, 1006)
    level = poc(bars_from(RANGE))
    assert 999.5 <= level <= 1002                                 # 刻み（0.05%）の範囲で
    heavy = bars_from([(100, 101, 100, 101, 100), (110, 111, 110, 111, 9000), (120, 121, 120, 121, 100)])
    assert 110 <= poc(heavy) <= 111.1 and poc([]) is None


def test_hold_above_fvg_with_volume_signals_continuation():
    rows = RANGE + LEG + PULL + [(1007, 1014, 1006.5, 1013, 9000)]     # 出来高を伴う陽線で反発
    sig = detect(bars_from(rows))
    assert sig.kind == "hold" and (sig.bottom, sig.top) == (1003, 1006)
    assert (sig.leg_low, sig.leg_high, sig.pullback_low) == (998, 1030, 1005)
    assert sig.volume_ratio > 1.5 and "上昇継続" in sig.note and "割安ゾーン" in sig.note


def test_break_below_fvg_with_volume_points_to_range_poc():
    rows = RANGE + LEG + PULL + [(1007, 1008, 1000, 1001, 9000)]       # 出来高を伴ってFVGを下抜け
    sig = detect(bars_from(rows))
    assert sig.kind == "break" and 999.5 <= sig.poc <= 1002
    assert sig.poc_source == "上昇が始まる前のレンジ" and "POC" in sig.note and "下抜け" in sig.note


def test_no_signal_without_volume_or_without_pullback():
    assert detect(bars_from(RANGE + LEG + PULL + [(1007, 1014, 1006.5, 1013, 2000)])) is None   # 出来高が増えない
    assert detect(bars_from(RANGE + LEG + PULL + [(1007, 1008, 1000, 1001, 2000)])) is None
    assert detect(bars_from(RANGE + LEG)) is None                      # まだ高値を更新中
    far = RANGE + LEG + [(1029, 1029, 1022, 1023, 2000), (1023, 1028, 1022, 1027, 9000)]
    assert detect(bars_from(far)) is None                              # FVGまで押していない


def test_only_discount_zone_fvg_is_used():
    # FVGが上昇の上半分（割高ゾーン）にしかない場合は対象外
    leg = [(1000, 1003, 998, 1002, 3000), (1002, 1008, 1002, 1007, 5000), (1007, 1013, 1006, 1012, 5000),
           (1012, 1016, 1011, 1015, 5000), (1016, 1026, 1016, 1025, 9000), (1025, 1030, 1019, 1029, 6000)]
    assert [x for x in find_fvgs(bars_from(RANGE + leg), len(RANGE), len(RANGE) + 5, 0.0003)
            if x[1] >= 1016] != []
    pull = [(1029, 1029, 1020, 1021, 2000), (1021, 1022, 1018, 1019, 2000)]
    assert detect(bars_from(RANGE + leg + pull + [(1019, 1026, 1018.5, 1025, 9000)])) is None


def test_already_broken_fvg_is_not_reused():
    rows = RANGE + LEG + PULL + [(1007, 1008, 1000, 1001, 9000),       # 一度割れた
                                 (1001, 1003, 1000, 1002, 2000), (1002, 1010, 1002, 1009, 9000)]
    assert detect(bars_from(rows)) is None


def test_structure_for_chart_overlay():
    st = structure(bars_from(RANGE + LEG + PULL))
    assert (st["low"], st["high"], st["mid"]) == (998, 1030, 1014) and st["pullback"]
    (f,) = [f for f in st["fvgs"] if f["bottom"] == 1003]
    assert f["top"] == 1006 and f["discount"] and f["broken"] is None
    assert len(st["profile"]) > 10 and 998 <= st["leg_poc"] <= 1030
    assert 999.5 <= st["range_poc"] <= 1002
    broken = structure(bars_from(RANGE + LEG + PULL + [(1007, 1008, 1000, 1001, 9000)]))
    assert [f for f in broken["fvgs"] if f["bottom"] == 1003][0]["broken"] is not None
    assert structure(bars_from(RANGE)) is None                         # 上昇が無ければ何も出さない


def test_engine_notifies_fvg_once_and_never_trades_it():
    from candle.engine import PaperEngine
    from candle.risk import RiskLimits, RiskManager
    from candle.store import Store

    store = Store(":memory:")
    rm = RiskManager(RiskLimits())
    rm.feed_ok = True
    sent = []
    e = PaperEngine(store, rm, enabled={"tweezer_bottom"}, notify=lambda *a: sent.append(a),
                    notify_ids={"fvg_hold", "fvg_break"})
    e.names = {"T": "テスト"}
    bars = bars_from(RANGE + LEG + PULL + [(1007, 1014, 1006.5, 1013, 9000), (1013, 1016, 1012, 1015, 9500)])
    for b in bars:
        e.on_bar(b)
    assert [a[0] for a in sent] == ["FVG反発  T テスト"] and "上昇継続" in sent[0][1]   # 次の足では再通知しない
    (row,) = store.recent_signals("paper")
    assert row["strategy_id"] == "fvg_hold" and row["action"] == "通知のみ" and row["pattern_low"] == 1003
    assert e.open == [] and not e.pending
    assert e.chart_data("T")["structure"]["fvgs"][0]["bottom"] == 1003
    import json
    ((kind, metrics, price),) = store.conn.execute("SELECT kind, metrics, price FROM squeeze_events").fetchall()
    m = json.loads(metrics)                                            # 検知履歴にも残り、その後を追う
    assert kind == "fvg" and m["outcome"] == "hold" and m["bottom"] == 1003 and price == 1013

    sent.clear()
    e2 = PaperEngine(Store(":memory:"), rm, enabled={"tweezer_bottom"}, notify=lambda *a: sent.append(a),
                     notify_ids={"fvg_break"})
    for b in bars_from(RANGE + LEG + PULL + [(1007, 1014, 1006.5, 1013, 9000)], symbol="U"):
        e2.on_bar(b)
    assert sent == []                                                  # 通知対象でない種類は出さない
    for b in bars_from(RANGE + LEG + PULL + [(1007, 1008, 1000, 1001, 9000)], symbol="V"):
        e2.on_bar(b)
    assert [a[0] for a in sent] == ["FVG割れ  V"] and "POC" in sent[0][1]
