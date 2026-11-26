import json
import urllib.request
from datetime import datetime, timedelta

import pytest

from candle import squeeze_web
from candle.bars import JST
from candle.engine import PaperEngine
from candle.risk import RiskLimits, RiskManager
from candle.squeeze_log import SqueezeLog
from candle.store import Store
from candle.web import start_web

T0 = datetime(2026, 11, 25, 10, 0, 0, tzinfo=JST)


def m(price, cum, ask, bid, **extra):
    return {"CurrentPrice": price, "TradingVolume": cum, "Sell1": {"Price": ask, "Qty": 1000},
            "Buy1": {"Price": bid, "Qty": 800}, **extra}


def make(tmp_path, **kw):
    store = Store(tmp_path / "c.sqlite")
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"},
                    notify_ids={"squeeze"}, **kw)
    e.names = {"7203": "トヨタ", "6758": "ソニーG"}
    return e, store


def detect(e, sym="7203", start=T0, extra=None):
    """検知が出るまで配信を流し、(検知時刻, その時点の累積出来高) を返す。"""
    extra = extra or {}
    before = len(e.squeeze_log.active)
    e.on_board(sym, m(1000, 360_000, 1001, 1000, **extra), start)
    cum = 360_000
    for i in range(1, 20):
        cum += 2000
        e.on_board(sym, m(1001, cum, 1001, 1000, **extra), start + timedelta(seconds=5 * i))
        if len(e.squeeze_log.active) > before:
            return e.squeeze_log.active[-1].t0, cum
    raise AssertionError("検知されませんでした")


def test_rising_detection_is_tracked_for_five_minutes(tmp_path):
    e, store = make(tmp_path)
    t0, cum = detect(e, extra={"PreviousClose": 990.0, "ChangePreviousClose": 11.0,
                               "ChangePreviousClosePer": 1.11,
                               "Sell2": {"Price": 1002, "Qty": 500}, "Buy2": {"Price": 999, "Qty": 700}})
    def feed(points):
        nonlocal cum
        for sec, price in points:
            cum += 500
            e.on_board("7203", m(price, cum, price + 1, price - 1), t0 + timedelta(seconds=sec))

    feed(((8, 1002), (25, 1004), (50, 1003)))
    e.on_clock(t0 + timedelta(seconds=120))
    ev = squeeze_web.list_events(store_path(tmp_path))["events"][0]
    assert ev["status"] == "tracking" and "ret" in ev["horizons"]["60"]
    assert ev["horizons"]["180"] == {"pending": True}            # まだ来ていない時点は埋めない
    feed(((170, 1010), (290, 1008)))
    e.on_clock(t0 + timedelta(seconds=301))
    res = squeeze_web.list_events(store_path(tmp_path))
    (ev,) = res["events"]
    q, c = ev["acquired"], ev["computed"]
    assert ev["status"] == "done" and q["symbol"] == "7203" and q["name"] == "トヨタ"
    assert (q["price"], q["prev_close"], q["change"], q["change_pct"]) == (1001, 990.0, 11.0, 1.11)
    assert q["volume"] == cum - 2500 and q["max_price"] == 1010 and q["min_price"] == 1001
    prices = {h: ev["horizons"][h]["price"] for h in ("10", "30", "60", "180", "300")}
    assert prices == {"10": 1002, "30": 1004, "60": 1003, "180": 1010, "300": 1008}
    assert ev["horizons"]["10"]["age"] == 2.0                    # 10秒時点で最後の約定は2秒前
    assert ev["horizons"]["60"]["ret"] == pytest.approx((1003 - 1001) / 1001 * 100)
    assert c["max_up"] == pytest.approx(9 / 1001 * 100) and c["max_down"] == 0
    assert c["absorb_score"] == pytest.approx(14.0) and c["run"] == 7 and c["surge"] > 2
    conds = {x["key"]: x for x in ev["conditions"]}
    assert conds["absorb"]["ok"] and conds["run"]["ok"] and not conds["resist"]["ok"]
    assert "14,000株" in conds["absorb"]["text"] and "基準" in conds["resist"]["text"]   # 不成立の理由も残す
    agg = res["aggregate"]
    assert agg["total"] == 1 and agg["complete"] == 1
    assert agg["by_horizon"][2]["up"] == 1 and agg["by_hour"][0]["hour"] == 10

    d = squeeze_web.get_event(store_path(tmp_path), ev["id"])
    assert d["board"]["levels"]["Sell2"] == {"Price": 1002, "Qty": 500}
    rels = [t["rel"] for t in d["ticks"]]
    assert min(rels) == -35 and max(rels) == 290 and 0 in rels   # 検知前から検知後まで
    assert d["pre_coverage"] == 35 and d["tape"] == {"missing": "この起動方法では歩み値APIを使っていません"}
    assert [t["side"] for t in d["ticks"] if t["rel"] == 0] == [1]


def store_path(tmp_path):
    return tmp_path / "c.sqlite"


def test_falling_and_flat_detections_are_kept(tmp_path):
    e, _ = make(tmp_path)
    t0, cum = detect(e)
    for sec, price in ((8, 1000), (25, 998), (50, 996), (170, 990), (290, 985)):
        cum += 500
        e.on_board("7203", m(price, cum, price + 1, price - 1), t0 + timedelta(seconds=sec))
    t1, cum2 = detect(e, "6758", start=T0 + timedelta(minutes=70))   # 11時台。以後は約定なし
    e.on_clock(t1 + timedelta(seconds=301))
    res = squeeze_web.list_events(store_path(tmp_path))
    flat, down = res["events"]                                    # 新しい順
    assert down["horizons"]["300"]["ret"] < -1 and down["computed"]["max_down"] < -1
    assert flat["horizons"]["300"]["ret"] == 0 and flat["horizons"]["300"]["age"] == 300
    h300 = res["aggregate"]["by_horizon"][-1]
    assert (h300["up"], h300["down"], h300["flat"], h300["missing"]) == (0, 1, 1, 0)
    assert [r["hour"] for r in res["aggregate"]["by_hour"]] == [10, 11]


def test_gap_marks_later_horizons_missing_instead_of_guessing(tmp_path):
    e, _ = make(tmp_path)
    t0, cum = detect(e)
    e.on_board("7203", m(1003, cum + 500, 1004, 1002), t0 + timedelta(seconds=20))
    e.on_clock(t0 + timedelta(seconds=40))
    e.squeeze_log.on_gap("株価の受信が途切れたため、以降は記録できていません", t0 + timedelta(seconds=45))
    e.on_board("7203", m(1050, cum + 900, 1051, 1049), t0 + timedelta(seconds=100))   # 再接続後の値は使わない
    e.on_clock(t0 + timedelta(seconds=120))
    (ev,) = squeeze_web.list_events(store_path(tmp_path))["events"]
    assert ev["status"] == "incomplete" and ev["acquired"]["tracked_seconds"] == 45
    assert ev["horizons"]["10"]["price"] == 1001 and ev["horizons"]["30"]["price"] == 1003
    for h in ("60", "180", "300"):
        assert "受信が途切れた" in ev["horizons"][h]["missing"]
    assert ev["acquired"]["max_price"] == 1003                    # 途切れた後の1050は含めない
    agg = squeeze_web.list_events(store_path(tmp_path))["aggregate"]
    assert agg["incomplete"] == 1 and agg["by_horizon"][-1]["missing"] == 1
    assert agg["by_horizon"][-1]["n"] == 0 and agg["by_horizon"][-1]["avg"] is None


def test_restart_leaves_unfinished_tracking_as_incomplete(tmp_path):
    e, store = make(tmp_path)
    t0, _ = detect(e)
    e.on_clock(t0 + timedelta(seconds=15))
    SqueezeLog(store)                                             # 再起動に相当
    (ev,) = squeeze_web.list_events(store_path(tmp_path))["events"]
    assert ev["status"] == "incomplete" and "停止" in ev["gap_reason"]
    assert ev["horizons"]["10"]["price"] == 1001 and "停止" in ev["horizons"]["300"]["missing"]


def test_missing_fields_stay_missing(tmp_path):
    e, _ = make(tmp_path)
    t0 = T0
    e.on_board("7203", {"CurrentPrice": 1000, "TradingVolume": 360_000, "BidPrice": 1001, "AskPrice": 1000}, t0)
    cum = 360_000
    for i in range(1, 12):                                        # 気配の数量も板も前日比も届かない
        cum += 2000
        e.on_board("7203", {"CurrentPrice": 1001 if i % 3 else 1000, "TradingVolume": cum,
                            "BidPrice": 1001, "AskPrice": 1000}, t0 + timedelta(seconds=4 * i))
    events = squeeze_web.list_events(store_path(tmp_path))["events"]
    assert len(events) == 1
    ev = squeeze_web.get_event(store_path(tmp_path), events[0]["id"])
    assert ev["acquired"]["prev_close"] is None and ev["acquired"]["change_pct"] is None
    assert ev["board"] is None and ev["computed"]["absorb_score"] is None
    absorb = next(c for c in ev["conditions"] if c["key"] == "absorb")
    assert not absorb["ok"] and "受信できず" in absorb["text"]


def test_tape_from_api_is_filtered_to_the_window(tmp_path):
    rows = [{"Time": (T0 + timedelta(seconds=s)).isoformat(), "Price": 1000 + s / 100, "Volume": 100}
            for s in (-200, 0, 45, 200, 400)] + [{"Time": "??", "Price": 1, "Volume": 1}]
    e, _ = make(tmp_path, tape_fetcher=lambda symbol: rows)
    t0, _ = detect(e)
    assert t0 == T0 + timedelta(seconds=35)
    e.on_clock(t0 + timedelta(seconds=301))
    import time
    for _ in range(50):
        time.sleep(0.02)
        e.on_clock(t0 + timedelta(seconds=302))
        ev = squeeze_web.get_event(store_path(tmp_path), 1)
        if ev["tape"]:
            break
    assert [r["rel"] for r in ev["tape"]["rows"]] == [-35, 10, 165] and ev["tape"]["source_rows"] == 6

    def boom(symbol):
        raise RuntimeError("HTTP 400")
    e2, _ = make(tmp_path / "x", tape_fetcher=boom)
    t0, _ = detect(e2)
    e2.on_clock(t0 + timedelta(seconds=301))
    for _ in range(50):
        time.sleep(0.02)
        e2.on_clock(t0 + timedelta(seconds=302))
        ev = squeeze_web.get_event(tmp_path / "x" / "c.sqlite", 1)
        if ev["tape"]:
            break
    assert "取得に失敗" in ev["tape"]["missing"]


def test_web_pages_and_apis(tmp_path):
    e, _ = make(tmp_path)
    t0, _ = detect(e)
    e.on_clock(t0 + timedelta(seconds=301))
    srv = start_web(e, 0, tmp_path, store_path(tmp_path))
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    get = lambda p: urllib.request.urlopen(base + p).read().decode()
    try:
        assert "検知履歴" in get("/squeeze") and 'href="/squeeze"' in get("/")
        lst = json.loads(get("/api/squeeze/list"))
        assert len(lst["events"]) == 1 and lst["days"] == ["2026-11-25"]
        assert json.loads(get("/api/squeeze/list?from=2026-11-26&to=2026-11-26"))["events"] == []
        d = json.loads(get("/api/squeeze/event?id=1"))
        assert d["acquired"]["symbol"] == "7203" and len(d["ticks"]) >= 8
        for bad in ("/api/squeeze/event?id=999", "/api/squeeze/event?id=x", "/api/squeeze/list?from=1;drop"):
            with pytest.raises(Exception):
                get(bad)
        assert json.loads(get("/api/state")) is not None          # 既存の画面は影響を受けない
    finally:
        srv.shutdown()
    empty = squeeze_web.list_events(tmp_path / "none.sqlite")
    assert empty["events"] == [] and empty["aggregate"]["total"] == 0


def test_wall_events_are_recorded_separately_with_outcomes(tmp_path):
    from test_wall import asks_with_wall, board

    store = Store(tmp_path / "c.sqlite")
    sent = []
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"},
                    notify=lambda *a: sent.append(a), notify_ids={"wall_break"})
    e.names = {"7203": "トヨタ", "6758": "ソニーG"}
    assert e.walls is not None and e.flow is not None and not e.flow_notify

    def feed(sym, steps):
        cum = 100_000
        for sec, price, asks, vol in steps:
            cum += vol
            e.on_board(sym, {**board(price, asks), "TradingVolume": cum}, T0 + timedelta(seconds=sec))

    feed("7203", [(0, 998, asks_with_wall(999, 1003, 30000), 0),
                  (15, 1002, asks_with_wall(1002, 1003, 30000), 500),
                  (20, 1003, asks_with_wall(1003, 1003, 18000), 12000),
                  (25, 1003, asks_with_wall(1003, 1003, 8000), 10000),
                  (30, 1004, asks_with_wall(1004, 9999, 0), 8000),
                  (80, 1010, asks_with_wall(1011, 9999, 0), 3000)])
    feed("6758", [(0, 998, asks_with_wall(999, 1003, 30000), 0),
                  (15, 1002, asks_with_wall(1002, 1003, 30000), 500),
                  (20, 1002, asks_with_wall(1002, 1003, 5000), 300),
                  (25, 1004, asks_with_wall(1004, 9999, 0), 5000)])
    assert [a[0] for a in sent] == ["売り板突破  7203 トヨタ"]            # 通知は「吸収して突破」だけ
    assert "吸収して突破" in sent[0][1] and "30,000株" in sent[0][1]
    e.on_clock(T0 + timedelta(seconds=400))
    path = tmp_path / "c.sqlite"
    assert squeeze_web.list_events(path)["events"] == []                # スクイーズの一覧には混ざらない
    res = squeeze_web.list_events(path, kind="wall")
    assert res["outcomes"] == {"break": 1, "pulled": 1}                 # 突破しなかった分も残る
    brk = squeeze_web.list_events(path, kind="wall", outcome="break")["events"]
    assert len(brk) == 1 and brk[0]["kind"] == "wall" and brk[0]["acquired"]["symbol"] == "7203"
    assert brk[0]["metrics"]["executed_share"] == 1.0 and brk[0]["horizons"]["60"]["price"] == 1010
    d = squeeze_web.get_event(path, brk[0]["id"])
    assert any(c["key"] == "executed" and c["ok"] for c in d["conditions"]) and d["board"] is not None
    assert store.recent_signals("paper")[0]["strategy_id"] == "wall_break"
