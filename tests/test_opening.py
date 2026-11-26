from datetime import datetime, timedelta

from candle.bars import JST
from candle.opening import OpenParams, OpenTracker, bid_levels, board_tick

T0 = datetime(2026, 11, 27, 9, 5, 0, tzinfo=JST)


def board(price, ask, wall=(997, 80000), opening=995, high=1004, tick=1.0, base=2000, cum=100000):
    """ask から上に売り板10本、ask の1ティック下から買い板10本。wall=(価格, 数量) だけ厚くする。"""
    m = {"CurrentPrice": price, "OpeningPrice": opening, "HighPrice": high, "TradingVolume": cum,
         "PreviousClose": 990.0}
    for i in range(10):
        m[f"Sell{i + 1}"] = {"Price": ask + i * tick, "Qty": base}
        bp = ask - (i + 1) * tick
        m[f"Buy{i + 1}"] = {"Price": bp, "Qty": wall[1] if wall and bp == wall[0] else base}
    return m


def run(steps, params=None):
    tr, out = OpenTracker(params), []
    for sec, msg in steps:
        sig = tr.update("7203", msg, T0 + timedelta(seconds=sec))
        if sig:
            out.append(sig)
    return out


def test_helpers():
    m = board(1000, 1000)
    assert board_tick(m) == 1.0 and bid_levels(m)[0] == (999.0, 2000.0)
    assert board_tick(board(1000, 1000, wall=(998.5, 80000), tick=0.5)) == 0.5
    assert board_tick({"Sell1": {"Price": 1000, "Qty": 1}}) is None


def test_signal_with_thick_bid_three_ticks_below():
    (sig,) = run([(0, board(1000, 1000)), (2, board(1000, 1000)), (4, board(1000, 1000)),
                  (10, board(1000, 1000))])                       # 同じ板では2回出さない
    assert sig.time == T0 + timedelta(seconds=4)                  # 3秒以上、板に出ていてから
    assert (sig.price, sig.target, sig.low) == (1000, 1007, 996)  # 買い／+7ティック／厚い板の1ティック下
    m = sig.metrics
    assert (m["wall_price"], m["wall_qty"], m["ratio"], m["wall_below"]) == (997, 80000, 40.0, 3)
    assert m["pullback_ticks"] == 4 and m["outcome"] is None
    assert "目標 1,007.0円（+7ティック）" in sig.note and "厚い買い板 997.0円に 80,000株" in sig.note


def test_finer_tick_is_taken_from_the_board():
    b = board(1000, 1000, wall=(998.5, 80000), high=1002, tick=0.5)
    (sig,) = run([(0, b), (5, b)])
    assert (sig.target, sig.low) == (1003.5, 998.0)


def test_no_signal_cases():
    two = lambda b: run([(0, b), (5, b)])
    assert two(board(1000, 1000, wall=(996, 80000))) == []            # 厚い板が4ティック下
    assert two(board(1000, 1000, wall=(997, 6000))) == []             # 3倍では厚いとはいえない
    assert two(board(1000, 1000, wall=None)) == []
    assert two(board(1000, 1000, opening=1001)) == []                 # 始値より下（日足が陰線）
    assert two(board(1000, 1000, high=1001)) == []                    # 高値から1ティックしか押していない
    assert two(board(1000, 1000, opening=998, high=1000)) == []       # 上に抜けていない
    assert two(board(100, 100, wall=(97, 80000), opening=95, high=104)) == []   # 金額が小さい
    b = board(1000, 1000)
    tr = OpenTracker()
    late = datetime(2026, 11, 27, 10, 0, 0, tzinfo=JST)
    assert tr.update("7203", b, late) is None and tr.update("7203", b, late + timedelta(seconds=5)) is None
    # 厚い板が一瞬だけ出て消える（見せ板）
    assert run([(0, board(1000, 1000)), (1, board(1000, 1000, wall=None)), (2, board(1000, 1000)),
                (4, board(1000, 1000))]) == []
    assert len(run([(0, b), (5, b), (60, b), (130, b)], OpenParams())) == 2   # 120秒あければ再度知らせる


def engine(tmp_path, **kw):
    from candle.engine import PaperEngine
    from candle.risk import RiskLimits, RiskManager
    from candle.store import Store
    store, sent = Store(tmp_path / "c.sqlite"), []
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"}, trade_enabled=False,
                    notify=lambda *a: sent.append(a), notify_ids={"open_dip"}, **kw)
    e.names = {"7203": "トヨタ"}
    return e, store, sent


def test_engine_notifies_in_quiet_open_and_records_result(tmp_path):
    from candle import squeeze_web
    e, store, sent = engine(tmp_path)
    assert e.settings.quiet_open_min == 15                            # 待つ時間の中でも、これは知らせる
    e.on_board("7203", board(1000, 1000), T0)
    e.on_board("7203", board(1000, 1000, cum=100500), T0 + timedelta(seconds=5))
    assert [a[0] for a in sent] == ["寄り付き押し目  7203 トヨタ"] and "目標 1,007.0円" in sent[0][1]
    (row,) = store.recent_signals("paper")
    assert row["strategy_id"] == "open_dip" and row["pattern_low"] == 996 and row["action"] == "通知のみ"
    assert e.open == [] and not e.pending
    db = tmp_path / "c.sqlite"
    (ev,) = squeeze_web.list_events(db, kind="open")["events"]
    assert ev["metrics"]["outcome"] is None and ev["metrics"]["target"] == 1007
    e.on_board("7203", board(1004, 1005, wall=None, cum=101000), T0 + timedelta(seconds=20))
    e.on_board("7203", board(1007, 1008, wall=None, high=1007, cum=102000), T0 + timedelta(seconds=40))
    res = squeeze_web.list_events(db, kind="open")
    m = res["events"][0]["metrics"]
    assert (m["outcome"], m["outcome_label"], m["result_seconds"]) == ("target", "目標に到達", 35.0)
    assert res["outcomes"] == {"target": 1} and e.open_active == []
    assert squeeze_web.list_events(db)["events"] == []                # スクイーズの一覧には混ざらない


def test_engine_stop_result_and_notify_switch(tmp_path):
    from candle import squeeze_web
    e, store, sent = engine(tmp_path)
    e.on_board("7203", board(1000, 1000), T0)
    e.on_board("7203", board(1000, 1000), T0 + timedelta(seconds=5))
    e.on_board("7203", board(996, 997, wall=None, cum=120000), T0 + timedelta(seconds=30))
    (ev,) = squeeze_web.list_events(tmp_path / "c.sqlite", kind="open")["events"]
    assert ev["metrics"]["outcome"] == "stop"
    e2, _, sent2 = engine(tmp_path / "x")
    e2.update_settings({"notify_on": "tweezer_bottom"})               # 画面でオフにすれば知らせない
    e2.on_board("7203", board(1000, 1000), T0)
    e2.on_board("7203", board(1000, 1000), T0 + timedelta(seconds=5))
    assert sent2 == [] and e2.store.recent_signals("paper") == []
    e3, _, sent3 = engine(tmp_path / "y")
    e3.on_board("7203", board(400, 400, wall=(397, 200000), opening=395, high=404), T0)
    e3.on_board("7203", board(400, 400, wall=(397, 200000), opening=395, high=404), T0 + timedelta(seconds=5))
    assert sent3 == []                                                # 株価の下限より安い銘柄は対象外
