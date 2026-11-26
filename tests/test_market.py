from datetime import datetime, timedelta

from candle.bars import JST
from candle.collector import market_sources
from candle.config import Config
from candle.engine import PaperEngine
from candle.market import MarketMonitor
from candle.risk import RiskLimits, RiskManager
from candle.store import Store

from helpers import DOWN, make_bars

T0 = datetime(2026, 11, 26, 14, 0, 0, tzinfo=JST)
NAMES = [str(7000 + i) for i in range(50)]


def feed(m, minutes, fut=(58000, 58000), topix=(3300, 3300), down=0):
    """minutes 分かけて、先物・TOPIX・50銘柄を始値から終値へ直線的に動かす。down は下げる銘柄数。"""
    steps = minutes * 6
    for i in range(steps + 1):
        now = T0 + timedelta(seconds=10 * i)
        f = i / steps
        m.on_price("F1", fut[0] + (fut[1] - fut[0]) * f, now)
        m.on_price("151", topix[0] + (topix[1] - topix[0]) * f, now)
        for k, s in enumerate(NAMES):
            m.on_price(s, 1000 * (1 - 0.004 * f) if k < down else 1000 * (1 + 0.002 * f), now)
    return T0 + timedelta(minutes=minutes)


def monitor():
    m = MarketMonitor()
    m.set_sources({"F1": "fut", "151": "topix"}, {"fut": "日経225先物 12月限", "topix": "TOPIX"}, {})
    return m


def test_levels_follow_futures_then_topix_then_breadth():
    m = monitor()
    now = feed(m, 6)
    st = m.state(now, NAMES)
    assert st["level"] == "normal" and st["breadth"]["pct"] == 0 and st["fut"]["available"]

    m = monitor()
    now = feed(m, 6, fut=(58000, 57700))                         # 先物だけ急落（-0.5%）
    st = m.state(now, NAMES)
    assert st["level"] == "caution" and st["label"] == "警戒" and not st["halt"]
    assert st["fut"]["falling"] and not st["topix"]["falling"]

    m = monitor()
    now = feed(m, 6, fut=(58000, 57700), topix=(3300, 3290), down=20)   # TOPIXも下落
    st = m.state(now, NAMES)
    assert st["level"] == "strong" and st["label"] == "警戒強化" and not st["halt"]
    assert st["breadth"]["pct"] == 40

    m = monitor()
    now = feed(m, 6, fut=(58000, 57700), topix=(3300, 3290), down=36)   # 50銘柄の72%が下落
    st = m.state(now, NAMES)
    assert st["level"] == "halt" and st["halt"] and st["breadth"]["down"] == 36


def test_breadth_alone_halts_even_if_futures_are_flat():
    m = monitor()
    now = feed(m, 6, down=40)                                    # 先物は横ばいでも個別株が総崩れ
    assert m.state(now, NAMES)["level"] == "halt"


def test_futures_alone_never_halts():
    m = monitor()
    now = feed(m, 6, fut=(58000, 57000), topix=(3300, 3250), down=10)
    st = m.state(now, NAMES)
    assert st["level"] == "strong" and not st["halt"]            # 一部の銘柄に引っ張られた下げでは止めない


def test_halt_is_held_for_five_minutes_after_recovery():
    m = monitor()
    now = feed(m, 6, down=40)
    assert m.halted(now, NAMES)
    for i in range(1, 80):                                       # 全銘柄が戻す
        t = now + timedelta(seconds=10 * i)
        for s in NAMES:
            m.on_price(s, 1000 + i, t)
        m.on_price("F1", 58000, t)
        m.on_price("151", 3300, t)
    st = m.state(now + timedelta(seconds=120), NAMES)
    assert st["halt"] and st["held"]                             # 条件が外れても5分は停止
    assert not m.state(now + timedelta(seconds=10 * 79), NAMES)["halt"]


def test_missing_sources_are_reported_not_guessed():
    m = MarketMonitor()
    m.set_sources({"151": "topix"}, {"fut": "日経225先物", "topix": "TOPIX"},
                  {"fut": "先物の銘柄コードを取得できませんでした（HTTP 400）"})
    st = m.state(T0, NAMES)
    assert not st["fut"]["available"] and "取得できません" in st["fut"]["reason"]
    assert st["topix"]["reason"] == "まだ受信していません"
    assert not st["breadth"]["available"] and "20 件以上" in st["breadth"]["reason"]
    assert st["level"] == "normal" and not st["halt"]            # データが無いだけでは止めない
    m.on_price("151", 3300, T0)
    assert "5分前の値" in m.state(T0 + timedelta(seconds=30), NAMES)["topix"]["reason"]
    assert "受信していません" in m.state(T0 + timedelta(minutes=5), NAMES)["topix"]["reason"]


def test_market_sources_resolves_future_and_handles_failure():
    class Client:
        def future_symbol(self, code, month):
            assert (code, month) == ("NK225", 0)
            return {"Symbol": "166120018", "SymbolName": "日経平均先物 26/12"}

    items, symbols, labels, problems = market_sources(Config(), Client())
    assert items == [{"Symbol": "166120018", "Exchange": 2}, {"Symbol": "151", "Exchange": 1}]
    assert symbols == {"166120018": "fut", "151": "topix"} and labels["fut"] == "日経平均先物 26/12"
    assert problems == {}

    class Broken:
        def future_symbol(self, code, month):
            raise RuntimeError("HTTP 400")

    items, symbols, _, problems = market_sources(Config(), Broken())
    assert items == [{"Symbol": "151", "Exchange": 1}] and "fut" in problems
    items, _, _, problems = market_sources(Config(market_future="", market_topix=""), Broken())
    assert items == [] and set(problems) == {"fut", "topix"}


def engine():
    rm = RiskManager(RiskLimits())
    rm.feed_ok = True
    e = PaperEngine(Store(":memory:"), rm, enabled={"tweezer_bottom"}, slippage=0.0)
    e.market.set_sources({"F1": "fut", "151": "topix"}, {}, {})
    e.watchlist = list(NAMES)
    return e


def test_engine_blocks_new_buys_while_market_is_halted_but_still_manages_positions():
    e = engine()
    bars = make_bars(DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)], "7000", start=(14, 0), day=26)
    for b in bars:
        e.on_bar(b)
    t0 = bars[-1].end
    e.on_price("7000", 1006, t0)                                 # 通常時は買う
    assert len(e.open) == 1
    # その後、50銘柄中40銘柄が5分かけて下げる
    for i in range(37):
        now = t0 + timedelta(seconds=10 * i)
        for k, s in enumerate(NAMES[1:], start=1):
            e.on_price(s, 1000 * (1 - 0.004 * i / 36) if k <= 40 else 1000.0, now)
    now = t0 + timedelta(seconds=360)
    assert e.market_state(now)["halt"]
    e.pending["7049"].append(("tweezer_bottom", now, 995.0))
    e.on_price("7049", 1000.0, now)
    assert len(e.open) == 1 and e.store.recent_signals("paper")[0]["action"] == "地合い悪化で新規停止中"
    e.on_price("7000", 990, now)                                 # 保有中の損切りは通常どおり
    assert e.open == []
    e.on_clock(now)
    assert e.snapshot["market"]["level"] == "halt" and e.snapshot["market"]["breadth"]["down"] >= 40


def test_futures_and_topix_are_never_traded_or_shown_as_stocks():
    e = engine()
    sent = []
    e.notify = lambda *a: sent.append(a)
    e.on_price("F1", 58000, T0)
    e.on_price("151", 3300, T0)
    assert "F1" not in e.prices and "151" not in e.prices
    for b in make_bars(DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)], "F1", start=(14, 0), day=26):
        e.on_bar(b)                                              # 先物の足で毛抜き底の形が出ても無視
    assert sent == [] and not e.pending
    e.on_board("F1", {"CurrentPrice": 58000, "TradingVolume": 1000}, T0)
    assert e.flow is None or "F1" not in e.flow._states


def test_breadth_threshold_zero_disables_the_halt():
    e = engine()
    e.update_settings({"mkt_breadth": 0})
    for i in range(37):
        now = T0 + timedelta(seconds=10 * i)
        for s in NAMES:
            e.on_price(s, 1000 * (1 - 0.004 * i / 36), now)
    st = e.market_state(T0 + timedelta(seconds=360))
    assert st["breadth"]["pct"] == 100 and not st["halt"]
