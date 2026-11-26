import json
import urllib.request
from datetime import datetime, time, timedelta

from candle.backtest import format_report, run_backtest, run_symbol
from candle.bars import JST
from candle.engine import PaperEngine
from candle.patterns import STRATEGY_BY_ID, Params
from candle.risk import RiskLimits, RiskManager
from candle.store import Store
from candle.trade import Trade, summarize
from candle.web import start_web
from candle.yahoo import parse_chart

from helpers import DOWN, make_bars

T0 = datetime(2026, 11, 24, 10, 0, tzinfo=JST)
HAMMER = DOWN + [(1004, 1005.5, 985, 1005)]


def trade(entry=1000.0, stop=990.0, qty=100, sid="hammer", sym="T"):
    return Trade("paper", sid, sym, T0, T0, entry, qty, stop, stop, high=entry)


def test_trailing_steps_by_r():
    t = trade()
    assert not t.trail(1009) and t.stop == 990          # +1R未満は動かさない
    assert t.trail(1010) and t.stop == 1000             # +1R → 建値
    assert t.trail(1025) and t.stop == 1010             # +2R → +1R
    assert t.trail(1030) and t.stop == 1020             # +3R → +2R
    assert t.trail(1075) and t.stop == 1060             # 以降も追従
    assert not t.trail(1040) and t.stop == 1060         # 下げても戻さない
    t.close(T0 + timedelta(minutes=30), 1060, "トレーリング")
    assert t.pnl() == 6000 and t.r_multiple() == 6


def test_summarize_stats():
    ts = [trade(), trade(), trade()]
    for t, px in zip(ts, (1030, 990, 990)):
        t.close(T0 + timedelta(minutes=10), px, "x")
    s = summarize(ts + [trade()])                        # 保有中の1件は数えない
    assert s["trades"] == 3 and abs(s["win_rate"] - 1 / 3) < 1e-9
    assert s["pf"] == 1.5 and round(s["expectancy"]) == 333 and round(s["expectancy_r"], 2) == 0.33
    assert summarize([])["pf"] is None


def test_risk_limits():
    rm = RiskManager(RiskLimits(total_cap=300_000, per_symbol_cap=200_000, daily_max_loss=5_000))
    assert rm.check_entry("hammer", "A", 1000, 990, []) == (0, "API切断中")
    rm.feed_ok = True
    assert rm.check_entry("hammer", "A", 1000, 990, []) == (200, "entry")
    held = [trade(sym="A", qty=200)]
    assert rm.check_entry("hammer", "A", 1000, 990, held)[1] == "同一戦略で保有中"
    # 別戦略でも同一銘柄の上限は共有する
    assert rm.check_entry("tweezer_bottom", "A", 1000, 990, held)[1] == "資金上限を超える"
    # 別銘柄は総額上限の残り（10万円）まで
    assert rm.check_entry("hammer", "B", 1000, 990, held) == (100, "entry")
    assert rm.check_entry("hammer", "C", 6000, 5950, [])[1] == "資金上限を超える"   # 1単元が買えない
    assert rm.check_entry("hammer", "B", 1000, 999.5, [])[1] == "損切り幅が小さすぎる"
    assert rm.check_entry("hammer", "B", 1000, 950, [])[1] == "損切り幅が大きすぎる"
    rm.realized_today = -5_000
    assert rm.check_entry("hammer", "B", 1000, 990, [])[1] == "1日の最大損失に到達"
    rm.realized_today, rm.halted = 0, True
    assert rm.check_entry("hammer", "B", 1000, 990, [])[1] == "緊急停止中"


def bt(after, **kw):
    return run_symbol(make_bars(HAMMER + after, start=(9, 0)), STRATEGY_BY_ID["hammer"], Params(),
                      RiskLimits(), slippage=0.0, **kw)


def test_backtest_initial_stop():
    (t,) = bt([(1006, 1008, 1000, 1002), (1002, 1003, 980, 982)])
    assert t.entry_price == 1006 and t.initial_stop == 985       # 次の足の始値で買う
    assert t.exit_price == 985 and t.exit_reason == "初期損切り"


def test_backtest_gap_below_stop_fills_at_open():
    (t,) = bt([(1006, 1008, 1000, 1002), (975, 978, 970, 972)])
    assert t.exit_price == 975


def test_backtest_trailing_exit():
    # R=21。+2R(1048)到達で損切りは+1R(1027)へ。その後の下落で決済。
    (t,) = bt([(1006, 1030, 1005, 1028), (1028, 1050, 1027, 1048), (1048, 1049, 1020, 1022)])
    assert t.exit_reason == "トレーリング" and t.exit_price == 1027 and t.r_multiple() == 1


def test_backtest_time_exit_and_no_late_entry():
    (t,) = bt([(1006, 1008, 1000, 1002), (1002, 1004, 1001, 1003)], exit_after=time(9, 40))
    assert t.exit_reason == "時間決済" and t.exit_price == 1003
    assert bt([(1006, 1008, 1000, 1002)], exit_after=time(9, 30)) == []   # 時刻以降は買わない


def test_backtest_report_runs_all_strategies():
    res = run_backtest({"T": make_bars(HAMMER + [(1006, 1008, 1000, 1002), (1002, 1003, 980, 982)])})
    assert len(res["hammer"]) == 1 and set(res) == set(STRATEGY_BY_ID)
    assert "ハンマー" in format_report(res, 1, 1)


def engine(**limits):
    store = Store(":memory:")
    rm = RiskManager(RiskLimits(**limits))
    rm.feed_ok = True
    return PaperEngine(store, rm, slippage=0.0), store


def feed_hammer(e, sym="T"):
    bars = make_bars(HAMMER, sym)
    for b in bars:
        e.on_bar(b)
    return bars[-1].end


def test_paper_entry_trail_and_exit():
    e, store = engine()
    t0 = feed_hammer(e)
    e.on_price("T", 1006, t0 + timedelta(seconds=3))            # 次の約定で仮想買い
    (t,) = e.open
    assert (t.strategy_id, t.qty, t.entry_price, t.stop) == ("hammer", 400, 1006, 985)
    e.on_price("T", 1048, t0 + timedelta(minutes=5))            # +2R → 損切りを+1Rへ
    assert t.stop == 1027
    e.on_clock(t0 + timedelta(minutes=6))
    pos = e.snapshot["positions"][0]
    assert pos["stop"] == 1027 and pos["initial_stop"] == 985 and pos["pnl"] == 42 * 400
    e.on_price("T", 1026, t0 + timedelta(minutes=8))
    assert e.open == [] and e.risk.realized_today == 20 * 400
    (done,) = store.load_trades("paper")
    assert done.exit_reason == "トレーリング"
    e.on_clock(t0 + timedelta(minutes=9))
    stats = {s["strategy"]: s for s in e.snapshot["stats"]}
    assert stats["ハンマー"]["trades"] == 1 and stats["毛抜き底"]["trades"] == 0
    assert e.snapshot["signals"][0]["action"] == "entry"


def test_paper_blocks_when_halted_and_time_exit():
    e, store = engine()
    e.risk.halted = True
    t0 = feed_hammer(e)
    e.on_price("T", 1006, t0)
    assert e.open == [] and store.recent_signals("paper")[0]["action"] == "緊急停止中"
    e.risk.halted = False
    feed_hammer(e, "U")
    e.on_price("U", 1006, t0)
    assert len(e.open) == 1
    e.on_clock(t0.replace(hour=15, minute=20))                  # 持ち越さない
    assert e.open == [] and store.load_trades("paper")[0].exit_reason == "時間決済"


def test_paper_restores_open_positions_after_restart():
    e, store = engine()
    t0 = feed_hammer(e)
    e.on_price("T", 1006, t0)
    e2 = PaperEngine(store, e.risk, slippage=0.0)
    assert len(e2.open) == 1 and e2.open[0].id == e.open[0].id


def test_web_serves_state_and_kill_switch():
    e, _ = engine()
    e.on_clock(T0)
    srv = start_web(e, 0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert "candle" in urllib.request.urlopen(base + "/").read().decode()
        assert json.load(urllib.request.urlopen(base + "/api/state"))["halted"] is False
        urllib.request.urlopen(urllib.request.Request(base + "/api/halt", method="POST"))
        assert e.risk.halted is True
        bad = urllib.request.Request(base + "/api/resume", method="POST",
                                     headers={"Origin": "http://evil.example"})
        try:
            urllib.request.urlopen(bad)
        except Exception:
            pass
        assert e.risk.halted is True                             # 他サイトからの操作は拒否
    finally:
        srv.shutdown()


def test_yahoo_parse_filters_off_session():
    ts = lambda h, m: int(datetime(2026, 11, 24, h, m, tzinfo=JST).timestamp())
    payload = {"chart": {"result": [{"timestamp": [ts(9, 0), ts(9, 5), ts(15, 30), ts(9, 10)],
               "indicators": {"quote": [{"open": [1, 2, 3, None], "high": [2, 3, 4, None],
                                         "low": [0.5, 1, 2, None], "close": [1.5, 2.5, 3.5, None],
                                         "volume": [100, 200, 300, None]}]}}]}}
    bars = parse_chart("7203", payload)
    assert [b.start.strftime("%H:%M") for b in bars] == ["09:00", "09:05"]


def test_watch_mode_notifies_only_selected_strategy_without_trading():
    store = Store(":memory:")
    rm = RiskManager(RiskLimits())
    rm.feed_ok = True
    sent = []
    e = PaperEngine(store, rm, enabled={"tweezer_bottom"}, trade_enabled=False,
                    notify=lambda title, body, symbol: sent.append((title, body, symbol)),
                    notify_ids={"tweezer_bottom"})
    e.names = {"7203": "トヨタ"}
    feed_hammer(e, "6758")                                       # 別の形では通知しない
    assert sent == []
    bars = make_bars(DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)], "7203")
    for b in bars:
        e.on_bar(b)
    assert sent == [("毛抜き底  7203 トヨタ", "10:00 終値 1,006.0 / パターン安値 995.0", "7203")]
    e.on_price("7203", 1007, bars[-1].end)
    assert e.open == [] and store.load_trades("paper") == []     # 売買はしない
    assert store.recent_signals("paper")[0]["action"] == "通知のみ"
    e.on_clock(bars[-1].end)
    assert e.snapshot["mode"] == "通知のみ" and e.snapshot["signals"][0]["name"] == "トヨタ"


def test_same_signal_is_notified_only_once_even_after_restart():
    store = Store(":memory:")
    rm = RiskManager(RiskLimits())
    sent = []
    mk = lambda: PaperEngine(store, rm, enabled={"tweezer_bottom"}, trade_enabled=False,
                             notify=lambda *a: sent.append(a), notify_ids={"tweezer_bottom"})
    bars = make_bars(DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)], "7203")
    e = mk()
    for b in bars:
        e.on_bar(b)
    e.on_bar(bars[-1])                                           # 同じ足が再度届いても通知しない
    assert len(sent) == 1
    e2 = mk()                                                    # 再起動して同じ足を再生
    for b in bars:
        e2.on_bar(b)
    assert len(sent) == 1 and len(store.recent_signals("paper")) == 1


def test_no_signal_from_unconfirmed_or_incomplete_bar():
    store = Store(":memory:")
    sent = []
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"},
                    trade_enabled=False, notify=lambda *a: sent.append(a))
    bars = make_bars(DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)], "7203")
    bars[-1].complete = False                                    # 欠落を含む足では判定しない
    for b in bars:
        e.on_bar(b)
    assert sent == []


def test_history_is_restored_from_saved_bars_after_restart():
    store = Store(":memory:")
    bars = make_bars(DOWN + [(1010, 1011, 995, 998)], "7203")
    store.save_bars(bars)
    sent = []
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"},
                    trade_enabled=False, notify=lambda *a: sent.append(a))
    e.load_history(["7203"], 5, today=bars[0].start.date())
    assert len(e.chart_data("7203")["bars"]) == 7 and e.prices["7203"] == 998
    nxt = make_bars(DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)], "7203")[-1]
    e.on_bar(nxt)                                                # 再起動直後の足でも判定できる
    assert len(sent) == 1


def test_auto_chart_switch_only_when_enabled():
    store = Store(":memory:")
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"}, trade_enabled=False)
    switched = []
    e.on_auto_chart = switched.append
    bars = make_bars(DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)], "7203")
    for b in bars:
        e.on_bar(b)
    assert switched == []                                        # 既定では切り替えない
    e.auto_chart = True
    for b in make_bars(DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)], "6758"):
        e.on_bar(b)
    assert switched == ["6758"]


def test_health_reports_whether_detection_is_running():
    e, _ = engine()
    e.watchlist = ["T", "U"]
    assert e.health(T0)["ready"] == 0 and e.health(T0)["last_tick"] is None
    bars = make_bars(DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)], "T")
    for b in bars:
        e.on_bar(b)
    t0 = bars[-1].end
    e.on_price("T", 1006, t0)
    h = e.health(t0)
    assert h["ready"] == 1 and h["watching"] == 2 and h["bars_today"] == 8   # 判定には8本必要
    assert h["signals_today"] >= 1 and h["market_open"] and h["last_tick"]   # 複数の戦略が同じ足で成立しうる


def test_max_hold_time_closes_stagnant_position():
    e, store = engine()
    t0 = feed_hammer(e)
    e.on_price("T", 1006, t0)
    e.on_clock(t0 + timedelta(minutes=50))
    assert len(e.open) == 1                                      # 既定（0）では時間で決済しない
    e.update_settings({"max_hold_min": 15})
    e.on_clock(t0 + timedelta(minutes=14))
    assert len(e.open) == 1
    e.on_clock(t0 + timedelta(minutes=15))
    assert e.open == [] and store.load_trades("paper")[0].exit_reason == "保有時間切れ"


def test_manual_close_of_one_position_from_dashboard():
    e, store = engine()
    t0 = feed_hammer(e, "T")
    e.on_price("T", 1006, t0)
    feed_hammer(e, "U")
    e.on_price("U", 1006, t0)
    a, b = e.open
    srv = start_web(e, 0)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/close?id={a.id}"
        urllib.request.urlopen(urllib.request.Request(url, method="POST"))
    finally:
        srv.shutdown()
    e.on_clock(t0 + timedelta(seconds=5))
    assert e.open == [b] and e.close_ids == set()                # 押した1件だけ決済
    done = [t for t in store.load_trades("paper") if not t.is_open]
    assert len(done) == 1 and done[0].exit_reason == "手動決済"
    assert e.snapshot["positions"][0]["id"] == b.id


def test_next_raise_shows_when_the_stop_will_move():
    t = trade(entry=595.9, stop=592.9)                           # R = 3.0
    lvl, new = t.next_raise()
    assert round(lvl, 1) == 598.9 and round(new, 1) == 595.9     # +1Rで建値へ
    t.trail(597.0)
    assert round(t.next_raise()[0], 1) == 598.9 and t.stop == 592.9
    t.trail(599.0)
    assert round(t.stop, 1) == 595.9
    lvl, new = t.next_raise()
    assert round(lvl, 1) == 601.9 and round(new, 1) == 598.9     # 次は+2Rで+1Rへ
    assert Trade("paper", "x", "A", T0, T0, 100, 100, 99, 99, trail_mode="percent",
                 trail_value=0.5).next_raise() is None


def test_no_signals_during_quiet_period_after_the_open():
    store = Store(":memory:")
    sent = []
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"},
                    trade_enabled=False, notify=lambda *a: sent.append(a))
    assert e.settings.quiet_open_min == 15
    shape = DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)]
    for b in make_bars(shape, "A", start=(9, 0), minutes=1):      # 9:08 に成立 → 通知しない
        e.on_bar(b)
    assert sent == [] and store.recent_signals("paper") == []
    for b in make_bars(shape, "B", start=(9, 8), minutes=1):      # 9:16 に成立 → 通知する
        e.on_bar(b)
    assert len(sent) == 1
    e.update_settings({"quiet_open_min": 0})
    for b in make_bars(shape, "C", start=(9, 0), minutes=1):
        e.on_bar(b)
    assert len(sent) == 2
    assert not e.in_quiet_open(T0.replace(hour=12, minute=31))    # 後場の寄り付きは対象外


def test_vwap_is_shown_but_not_used_as_a_condition():
    from candle.collector import Pipeline
    store = Store(":memory:")
    rm = RiskManager(RiskLimits())
    rm.feed_ok = True
    e = PaperEngine(store, rm, enabled={"hammer"}, slippage=0.0)
    pipe = Pipeline(store, 1, on_bar=e.on_bar, on_price=e.on_price, on_board=e.on_board)
    msg = lambda t, price, cum, vwap: {"Symbol": "T", "CurrentPrice": price, "TradingVolume": cum,
                                       "CurrentPriceTime": f"2026-11-24T{t}+09:00", "VWAP": vwap,
                                       "ChangePreviousClosePer": -1.2, "HighPrice": 1100}
    pipe.handle(msg("09:59:50", 1000, 100, 1050.0))
    pipe.handle(msg("10:00:10", 1001, 200, 1049.5))
    pipe.handle(msg("10:01:05", 1002, 300, 1049.0))
    (bar,) = store.load_bars("T", 1)
    assert bar.vwap == 1049.5                                    # 足の確定時点のVWAPを保存
    assert e.vwap("T") == 1049.0 and e.quotes["T"]["HighPrice"] == 1100
    assert e.chart_data("T")["quote"]["VWAP"] == 1049.0 and e.chart_data("T")["bars"][0]["vwap"] == 1049.5
    t0 = feed_hammer(e, "T")                                     # VWAPより下でもサインは出て買う
    e.on_price("T", 1006, t0)
    assert len(e.open) == 1 and store.recent_signals("paper")[0]["vwap"] == 1049.0
    e.on_clock(t0)
    assert e.snapshot["positions"][0]["vwap"] == 1049.0


def test_engine_applies_min_drop_setting_and_records_the_decline():
    store = Store(":memory:")
    sent = []
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"},
                    trade_enabled=False, notify=lambda *a: sent.append(a))
    assert e.settings.min_drop_pct == 0.3
    flat = [(594.6 - 0.05 * i, 594.7 - 0.05 * i, 594.3 - 0.05 * i, 594.5 - 0.05 * i) for i in range(6)]
    flat += [(594.2, 594.3, 594.0, 594.1), (594.1, 594.4, 594.0, 594.3)]
    for b in make_bars(flat, "8766"):
        e.on_bar(b)
    assert sent == []                                            # 横ばいの中の形は通知しない
    for b in make_bars(DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)], "7203"):
        e.on_bar(b)
    assert len(sent) == 1 and store.recent_signals("paper")[0]["drop_pct"] > 5
    e.update_settings({"min_drop_pct": 0})
    for b in make_bars(flat, "9433"):
        e.on_bar(b)
    assert len(sent) == 2 and round(store.recent_signals("paper")[0]["drop_pct"], 2) == 0.12


def test_position_size_from_share_of_buying_power():
    e, store = engine()
    e.update_settings({"size_mode": "percent", "per_trade_pct": 20, "total_pct": 100})
    assert e.risk.limits.per_symbol_cap == 500_000 and "sizing" in e.warnings   # 余力が不明なら金額の上限のまま
    e.buying_power = 3_300_000                                    # 信用新規可能額
    e.apply_sizing()
    assert e.risk.limits.per_symbol_cap == 660_000 and e.risk.limits.total_cap == 3_300_000
    assert "sizing" not in e.warnings
    t0 = feed_hammer(e)
    e.on_price("T", 1006, t0)
    assert e.open[0].qty == 600                                   # 66万円 ÷ 1,006円 → 600株
    e.on_clock(t0)
    assert e.snapshot["buying_power"] == 3_300_000 and e.snapshot["limits"]["per_symbol_cap"] == 660_000
    e.update_settings({"size_mode": "yen"})
    assert e.risk.limits.per_symbol_cap == 500_000
    import pytest
    with pytest.raises(ValueError):
        e.update_settings({"size_mode": "percent", "per_trade_pct": 50, "total_pct": 30})
    with pytest.raises(ValueError):
        e.update_settings({"total_pct": 150})


def test_live_capacity_adds_back_open_positions():
    from candle.live import LiveEngine
    from test_live import FakeBroker
    store = Store(":memory:")
    rm = RiskManager(RiskLimits())
    e = LiveEngine(store, rm, enabled={"tweezer_bottom"}, broker=FakeBroker())
    e.buying_power = 2_000_000
    e.open.append(trade(entry=1000.0, qty=1000))                  # すでに100万円分を建てている
    assert e.capacity() == 3_000_000                              # 余力は減っているので足し戻す
    e.update_settings({"size_mode": "percent", "per_trade_pct": 10, "total_pct": 50})
    assert e.risk.limits.total_cap == 1_500_000 and e.risk.limits.per_symbol_cap == 300_000
