from datetime import datetime, timedelta

from candle.bars import JST
from candle.engine import PaperEngine
from candle.flow import FlowDetector, FlowParams, best_quotes, session_seconds
from candle.risk import RiskLimits, RiskManager
from candle.store import Store

T0 = datetime(2026, 11, 25, 10, 0, 0, tzinfo=JST)   # 寄り付きから1時間後


def m(price, cum, ask, bid, ask_qty=1000, bid_qty=1000):
    return {"CurrentPrice": price, "TradingVolume": cum,
            "Sell1": {"Price": ask, "Qty": ask_qty}, "Buy1": {"Price": bid, "Qty": bid_qty}}


def run(det, steps, start=T0, base=360_000, sym="7203"):
    """steps: (秒, 約定値, 出来高, 売り気配, 買い気配)。最初に基準となる1件を入れる。"""
    det.update(sym, m(1000, base, 1001, 1000), start)
    cum, out = base, []
    for sec, price, vol, ask, bid in steps:
        cum += vol
        sig = det.update(sym, m(price, cum, ask, bid), start + timedelta(seconds=sec))
        if sig:
            out.append(sig)
    return out


def test_helpers():
    assert best_quotes({"BidPrice": 101, "BidQty": 300, "AskPrice": 100}) == (101.0, 300.0, 100.0)
    assert best_quotes({"Sell1": {"Price": 101, "Qty": 5}, "Buy1": {"Price": 100}}) == (101.0, 5.0, 100.0)
    assert session_seconds(T0) == 3600
    assert session_seconds(T0.replace(hour=12, minute=0)) == 9000       # 昼休みは数えない
    assert session_seconds(T0.replace(hour=13, minute=0)) == 9000 + 1800


def test_absorption_and_buy_run_trigger_signal():
    # 平均ペースは 100株/秒。40秒で16,000株が売り気配で買われ、売り気配は下がらない。
    steps = [(5 * i, 1001, 2000, 1001, 1000) for i in range(1, 9)]
    (sig,) = run(FlowDetector(), steps)
    assert sig.symbol == "7203" and sig.price == 1001 and sig.low == 1001
    assert any("売り板の吸収" in r for r in sig.reasons)
    assert any("買い約定の連続" in r for r in sig.reasons)
    assert "出来高 通常の" in sig.note


def test_no_signal_when_volume_is_normal_or_flow_is_selling():
    quiet = [(5 * i, 1001, 300, 1001, 1000) for i in range(1, 9)]       # 出来高が平常
    assert run(FlowDetector(), quiet) == []
    selling = [(5 * i, 1000, 2000, 1001, 1000) for i in range(1, 9)]    # 買い気配で売られている
    assert run(FlowDetector(), selling) == []
    falling = [(5 * i, 1001 - i, 2000, 1001 - i, 1000 - i) for i in range(1, 9)]
    assert run(FlowDetector(), falling) == []


def test_no_signal_after_price_already_jumped():
    steps = [(5 * i, 1000 + 2 * i, 2000, 1000 + 2 * i, 999 + 2 * i) for i in range(1, 9)]
    assert run(FlowDetector(), steps) == []                              # 期間の安値から0.4%超


def test_resilience_to_selling_counts_as_a_reason():
    # 売りが3割出ているが、買い気配も株価も下がらず、残りは売り気配で買われている
    steps = []
    for i in range(1, 11):
        sell = i % 3 == 0
        steps.append((4 * i, 1000 if sell else 1001, 2000, 1001, 1000))
    (sig,) = run(FlowDetector(FlowParams(run=99, buy_ratio=0.99)), steps)
    assert any("売り圧力への耐性" in r for r in sig.reasons)
    assert any("売り板の吸収" in r for r in sig.reasons)


def test_cooldown_and_opening_minutes_are_skipped():
    steps = [(5 * i, 1001, 2000, 1001, 1000) for i in range(1, 21)]
    assert len(run(FlowDetector(), steps)) == 1                          # 同じ銘柄は5分間1回
    early = datetime(2026, 11, 25, 9, 2, tzinfo=JST)
    assert run(FlowDetector(), steps[:8], start=early, base=12_000) == []


def test_engine_notifies_squeeze_independently_of_candle_patterns():
    store = Store(":memory:")
    sent = []
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"},
                    trade_enabled=True, notify=lambda *a: sent.append(a),
                    notify_ids={"tweezer_bottom", "squeeze"})
    e.names = {"7203": "トヨタ"}
    assert e.flow is not None and not e.flow_trades
    e.on_board("7203", m(1000, 360_000, 1001, 1000), T0)
    cum = 360_000
    for i in range(1, 9):
        cum += 2000
        e.on_board("7203", m(1001, cum, 1001, 1000), T0 + timedelta(seconds=5 * i))
    assert len(sent) == 1 and sent[0][0] == "スクイーズ  7203 トヨタ" and sent[0][2] == "7203"
    (row,) = store.recent_signals("paper")
    assert row["strategy_id"] == "squeeze" and row["action"] == "通知のみ" and "売り板の吸収" in row["note"]
    assert e.open == [] and not e.pending                         # 売買対象でなければ買わない
    off = PaperEngine(Store(":memory:"), RiskManager(RiskLimits()), enabled={"tweezer_bottom"},
                      notify_ids={"tweezer_bottom"})
    assert off.flow is None


def test_engine_can_trade_squeeze_when_enabled():
    store = Store(":memory:")
    rm = RiskManager(RiskLimits())
    rm.feed_ok = True
    e = PaperEngine(store, rm, enabled={"squeeze"}, slippage=0.0, notify_ids=set())
    e.update_settings({"stop_mode": "percent", "stop_value": 0.5})
    e.on_board("7203", m(1000, 360_000, 1001, 1000), T0)
    cum = 360_000
    for i in range(1, 9):
        cum += 2000
        e.on_board("7203", m(1001, cum, 1001, 1000), T0 + timedelta(seconds=5 * i))
    e.on_price("7203", 1001, T0 + timedelta(seconds=41))
    (t,) = e.open
    assert t.strategy_id == "squeeze" and round(t.stop, 3) == round(1001 * 0.995, 3)
    assert "売り板の吸収" in store.recent_signals("paper")[0]["note"]
