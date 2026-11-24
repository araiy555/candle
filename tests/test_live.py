from datetime import datetime, timedelta

import pytest

from candle.bars import JST
from candle.broker import KabuBroker, OrderStatus, parse_order
from candle.live import LiveEngine
from candle.risk import RiskLimits, RiskManager
from candle.settings import TradeSettings
from candle.store import Store
from candle.trade import Trade, tick_size, trigger_price

from helpers import DOWN, make_bars

TWEEZER = DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)]


class FakeBroker:
    """注文を受け付けて、テストから約定・取消を操作できる擬似の証券会社。"""

    def __init__(self):
        self.orders = {}
        self.sent = []
        self.n = 0
        self.fail = set()          # 'buy' | 'stop' | 'close' を入れると発注が例外になる
        self.auto_fill = {"buy": None, "close": None}   # 価格を入れると即約定
        self.held = {}
        self.holds_seen = []

    def _new(self, kind, symbol, qty, trigger=None):
        if kind in self.fail:
            raise RuntimeError(f"{kind} rejected")
        self.n += 1
        oid = f"O{self.n}"
        self.orders[oid] = dict(kind=kind, symbol=symbol, qty=qty, trigger=trigger, done=False,
                                cum=0, price=None)
        self.sent.append((kind, symbol, qty, trigger))
        if self.auto_fill.get(kind) is not None:
            self.fill(oid, self.auto_fill[kind])
        return oid

    def buy_market(self, symbol, qty):
        return self._new("buy", symbol, qty)

    def close_market(self, symbol, qty, holds=None):
        self.holds_seen.append(holds)
        return self._new("close", symbol, qty)

    def place_stop(self, symbol, qty, trigger, holds=None):
        self.holds_seen.append(holds)
        return self._new("stop", symbol, qty, trigger)

    def cancel(self, oid):
        o = self.orders[oid]
        if o["done"]:
            raise RuntimeError("already done")
        o["done"] = True

    def fill(self, oid, price, qty=None):
        o = self.orders[oid]
        o.update(done=True, cum=qty or o["qty"], price=price)
        sign = 1 if o["kind"] == "buy" else -1
        self.held[o["symbol"]] = self.held.get(o["symbol"], 0) + sign * o["cum"]

    def order(self, oid):
        o = self.orders[oid]
        fills = ((f"E-{oid}", o["cum"]),) if o["kind"] == "buy" and o["cum"] else ()
        return OrderStatus(o["done"], o["cum"], o["price"], fills)

    def positions(self):
        return {s: q for s, q in self.held.items() if q > 0}

    def tick_group(self, symbol):
        return "10000"

    def last(self, kind):
        return [k for k, o in self.orders.items() if o["kind"] == kind][-1]


def make(settings=None, stop_method="server", **limits):
    store = Store(":memory:")
    rm = RiskManager(RiskLimits(**limits))
    rm.feed_ok = True
    broker = FakeBroker()
    told = []
    e = LiveEngine(store, rm, enabled={"tweezer_bottom"}, settings=settings, broker=broker,
                   stop_method=stop_method, notify=lambda *a: told.append(a), notify_ids=set())
    return e, broker, store, told


def signal(e, sym="7203"):
    bars = make_bars(TWEEZER, sym)
    for b in bars:
        e.on_bar(b)
    return bars[-1].end


def test_tick_and_trigger_prices():
    assert tick_size(2999) == 1 and tick_size(3001) == 5 and tick_size(999, "10003") == 0.1
    assert trigger_price(995) == 994 and trigger_price(995, "10003") == 994.9
    assert trigger_price(3003) == 3000 and trigger_price(3000) == 2999
    assert trigger_price(995.3, "10003") == 995.2


def test_order_bodies_are_margin_daytrade_orders():
    b = KabuBroker(None)
    buy = b.buy_body("7203", 100)
    assert (buy["Side"], buy["CashMargin"], buy["MarginTradeType"], buy["FrontOrderType"],
            buy["Exchange"], buy["Qty"], buy["Price"]) == ("2", 2, 3, 10, 9, 100, 0)
    stop = b.stop_body("7203", 100, 994)
    assert (stop["Side"], stop["CashMargin"], stop["FrontOrderType"]) == ("1", 3, 30)
    assert stop["ReverseLimitOrder"] == {"TriggerSec": 1, "TriggerPrice": 994, "UnderOver": 1,
                                         "AfterHitOrderType": 1, "AfterHitPrice": 0}
    close = b.close_body("7203", 100)
    assert (close["Side"], close["CashMargin"], close["FrontOrderType"]) == ("1", 3, 10)
    assert "ClosePositionOrder" in close and "ClosePositionOrder" in stop


def test_parse_order_average_fill():
    row = {"State": 5, "CumQty": 300, "Details": [
        {"RecType": 1, "Qty": 300, "Price": 0}, {"RecType": 8, "Qty": 100, "Price": 1000},
        {"RecType": 8, "Qty": 200, "Price": 1003}]}
    st = parse_order(row)
    assert st.done and st.cum_qty == 300 and st.avg_price == pytest.approx(1002)
    assert parse_order({"State": 3, "CumQty": 0, "Details": []}) == OrderStatus(False, 0, None)
    assert parse_order({"State": 5, "CumQty": 0, "Details": [{"RecType": 6}]}).cum_qty == 0


def test_entry_fill_places_server_stop_then_stop_hit():
    e, br, store, told = make()
    t0 = signal(e)
    e.on_price("7203", 1006, t0 + timedelta(seconds=1))
    assert br.sent == [("buy", "7203", 400, None)]            # 50万円の上限 → 400株
    (t,) = e.open
    assert t.state == "entering"
    e.on_clock(t0 + timedelta(seconds=2))
    assert t.state == "entering" and len(br.sent) == 1          # 約定するまで逆指値は出さない
    br.fill(t.entry_order, 1007)
    e.on_clock(t0 + timedelta(seconds=5))
    assert t.state == "open" and t.entry_price == 1007 and t.stop == 995
    assert br.sent[-1] == ("stop", "7203", 400, 994) and t.server_stop == 994
    br.fill(t.stop_order, 993.5)                                # 証券会社側で損切りが約定
    e.on_clock(t0 + timedelta(seconds=8))
    assert e.open == [] and len(br.sent) == 2                    # 追加の売り注文は出さない
    (done,) = store.load_trades("live")
    assert done.exit_reason == "初期損切り" and done.exit_price == 993.5
    assert e.risk.realized_today == pytest.approx((993.5 - 1007) * 400)
    assert any("決済" in a[0] for a in told)


def test_trailing_replaces_server_stop():
    e, br, _, _ = make()
    br.auto_fill["buy"] = 1007
    t0 = signal(e)
    e.on_price("7203", 1006, t0 + timedelta(seconds=1))
    e.on_clock(t0 + timedelta(seconds=3))
    (t,) = e.open
    first_stop = t.stop_order
    e.on_price("7203", 1032, t0 + timedelta(seconds=20))        # R=12、+2R到達 → 損切りは+1R
    assert t.stop == pytest.approx(1019)
    e.on_clock(t0 + timedelta(seconds=21))
    assert t.state == "replacing" and br.orders[first_stop]["done"]   # 古い逆指値を取消
    e.on_clock(t0 + timedelta(seconds=24))
    assert t.state == "open" and t.stop_order != first_stop
    assert br.sent[-1] == ("stop", "7203", 400, 1018) and t.server_stop == 1018


def test_stop_hit_while_replacing_does_not_double_sell():
    e, br, _, _ = make()
    br.auto_fill["buy"] = 1007
    t0 = signal(e)
    e.on_price("7203", 1006, t0 + timedelta(seconds=1))
    e.on_clock(t0 + timedelta(seconds=3))
    (t,) = e.open
    old = t.stop_order
    br.cancel = lambda oid: br.fill(oid, 994)                    # 取消より先に約定していた
    e.on_price("7203", 1032, t0 + timedelta(seconds=20))
    e.on_clock(t0 + timedelta(seconds=21))
    e.on_clock(t0 + timedelta(seconds=24))
    assert e.open == [] and [k for k, *_ in br.sent].count("stop") == 1
    assert br.orders[old]["cum"] == 400 and br.held["7203"] == 0


def test_time_exit_cancels_stop_then_closes_at_market():
    e, br, store, _ = make()
    br.auto_fill["buy"] = 1007
    t0 = signal(e)
    e.on_price("7203", 1006, t0 + timedelta(seconds=1))
    e.on_clock(t0 + timedelta(seconds=3))
    (t,) = e.open
    stop_id = t.stop_order
    late = t0.replace(hour=15, minute=20)
    e.prices["7203"] = 1010
    e.on_clock(late)
    assert t.state == "exiting" and br.orders[stop_id]["done"]
    assert br.sent[-1] == ("close", "7203", 400, None)
    br.fill(t.exit_order, 1009)
    e.on_clock(late + timedelta(seconds=3))
    assert e.open == [] and store.load_trades("live")[0].exit_reason == "時間決済"
    assert store.load_trades("live")[0].exit_price == 1009


def test_buy_error_halts_and_never_retries():
    e, br, store, told = make()
    br.fail.add("buy")
    t0 = signal(e)
    e.on_price("7203", 1006, t0 + timedelta(seconds=1))
    assert e.open == [] and e.risk.halted and "entry" in e.warnings
    assert store.recent_signals("live")[0]["action"] == "発注エラー"
    br.fail.clear()
    signal(e, "6758")
    e.on_price("6758", 1006, t0 + timedelta(seconds=2))
    assert br.sent == [] and store.recent_signals("live")[0]["action"] == "緊急停止中"


def test_rejected_entry_is_removed():
    e, br, store, _ = make()
    t0 = signal(e)
    e.on_price("7203", 1006, t0 + timedelta(seconds=1))
    br.cancel(e.open[0].entry_order)                              # 約定せずに終了
    e.on_clock(t0 + timedelta(seconds=3))
    assert e.open == [] and store.load_trades("live") == []


def test_stop_placement_failure_falls_back_to_program_monitoring():
    e, br, _, told = make()
    br.auto_fill["buy"] = 1007
    br.fail.add("stop")
    t0 = signal(e)
    e.on_price("7203", 1006, t0 + timedelta(seconds=1))
    e.on_clock(t0 + timedelta(seconds=3))
    (t,) = e.open
    assert t.state == "open" and t.stop_order == "" and any("逆指値" in w for w in e.warnings.values())
    e.on_price("7203", 994, t0 + timedelta(seconds=10))          # 損切りラインを下回った
    e.on_clock(t0 + timedelta(seconds=11))
    e.on_clock(t0 + timedelta(seconds=14))
    assert br.sent[-1] == ("close", "7203", 400, None)


def test_selected_stop_and_percent_trailing_modes():
    s = TradeSettings(stop_mode="percent", stop_value=0.5, trail_mode="percent", trail_value=0.3)
    e, br, _, _ = make(settings=s, stop_method="client")
    br.auto_fill["buy"] = 1000
    t0 = signal(e)
    e.on_price("7203", 1000, t0 + timedelta(seconds=1))
    e.on_clock(t0 + timedelta(seconds=3))
    (t,) = e.open
    assert t.stop == pytest.approx(995) and t.trail_mode == "percent"   # 買値の0.5%下
    e.on_price("7203", 1010, t0 + timedelta(seconds=10))
    assert t.stop == pytest.approx(1010 * 0.997)                          # 高値の0.3%下に追従
    e.on_price("7203", 1005, t0 + timedelta(seconds=12))
    assert t.stop == pytest.approx(1010 * 0.997)                          # 下げても戻さない
    assert all(k != "stop" for k, *_ in br.sent)                          # client方式は逆指値を出さない


def test_settings_validation_and_yen_mode():
    s = TradeSettings.from_dict({"stop_mode": "yen", "stop_value": "8", "trail_mode": "yen",
                                 "trail_value": 5, "daily_max_loss": "10000"})
    assert s.initial_stop(1000, 990) == 992
    t = Trade("paper", "x", "A", None, None, 1000, 100, 992, 992, high=1000,
              trail_mode="yen", trail_value=5)
    assert t.trail(1001) and t.stop == 996 and not t.trail(999)
    none = Trade("paper", "x", "A", None, None, 1000, 100, 992, 992, high=1000, trail_mode="none")
    assert not none.trail(1100) and none.stop == 992
    with pytest.raises(ValueError):
        TradeSettings.from_dict({"stop_mode": "percent", "stop_value": 0})
    with pytest.raises(ValueError):
        TradeSettings.from_dict({"per_trade_cap": 2_000_000, "total_cap": 1_000_000})


def test_only_one_position_per_symbol_and_daily_loss_limit():
    e, br, store, _ = make()
    br.auto_fill["buy"] = 1007
    t0 = signal(e)
    e.on_price("7203", 1006, t0 + timedelta(seconds=1))
    e.pending["7203"].append(("tweezer_bottom", t0, 995))
    e.on_price("7203", 1006, t0 + timedelta(seconds=2))
    assert [k for k, *_ in br.sent].count("buy") == 1
    assert store.recent_signals("live")[0]["action"] == "同じ銘柄を保有中"
    e.risk.realized_today = -10_000                              # 1日の最大損失（既定1万円）
    signal(e, "6758")
    e.on_price("6758", 1006, t0 + timedelta(seconds=3))
    assert store.recent_signals("live")[0]["action"] == "1日の最大損失に到達"


def test_restart_resumes_open_live_trade():
    e, br, store, _ = make()
    br.auto_fill["buy"] = 1007
    t0 = signal(e)
    e.on_price("7203", 1006, t0 + timedelta(seconds=1))
    e.on_clock(t0 + timedelta(seconds=3))
    e2 = LiveEngine(store, e.risk, enabled={"tweezer_bottom"}, broker=br)
    (t,) = e2.open
    assert t.state == "open" and t.stop_order and t.server_stop == 994
    br.fill(t.stop_order, 994)
    e2.on_clock(t0 + timedelta(seconds=10))
    assert e2.open == []


def test_reconcile_mismatch_warns_then_halts():
    e, br, _, _ = make()
    br.auto_fill["buy"] = 1007
    t0 = signal(e)
    e.on_price("7203", 1006, t0 + timedelta(seconds=1))
    e.on_clock(t0 + timedelta(seconds=3))
    br.held["9999"] = 100                                        # 手動で持っている別銘柄は無視
    e.on_clock(t0 + timedelta(seconds=70))
    assert "reconcile" not in e.warnings
    br.held["7203"] = 0                                          # 外で決済されていた
    e.on_clock(t0 + timedelta(seconds=140))
    assert "reconcile" in e.warnings and not e.risk.halted
    e.on_clock(t0 + timedelta(seconds=210))
    assert e.risk.halted


def test_kabu_broker_over_http(monkeypatch):
    """KabuBroker が実際のHTTPで正しい宛先・形式の要求を出すこと（擬似サーバーで確認）。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from candle.kabu_client import KabuApiError, KabuClient

    calls = []

    class H(BaseHTTPRequestHandler):
        def _send(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else None

        def do_POST(self):
            body = self._body()
            if self.path.endswith("/token"):
                return self._send({"Token": "tok"})
            calls.append(("POST", self.path, body))
            if body.get("Qty") == 999:
                return self._send({"Code": 4002001, "Message": "パラメータ不正"}, 400)
            self._send({"Result": 0, "OrderId": f"ID{len(calls)}"})

        def do_PUT(self):
            calls.append(("PUT", self.path, self._body()))
            self._send({"Result": 0, "OrderId": "x"})

        def do_GET(self):
            calls.append(("GET", self.path, None))
            if "/orders" in self.path:
                return self._send([{"ID": "ID1", "State": 5, "CumQty": 100, "Details": [
                    {"RecType": 8, "Qty": 100, "Price": 2500.5, "ExecutionID": "E77"}]}])
            if "/positions" in self.path:
                return self._send([{"Symbol": "7203", "Side": "2", "LeavesQty": 100},
                                   {"Symbol": "7203", "Side": "2", "LeavesQty": 200},
                                   {"Symbol": "9984", "Side": "1", "LeavesQty": 100}])
            self._send({"PriceRangeGroup": "10003"})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = KabuClient(f"http://127.0.0.1:{srv.server_address[1]}/kabusapi", "pw",
                            min_interval=0)
        b = KabuBroker(client)
        assert b.buy_market("7203", 100) == "ID1"
        assert calls[-1][:2] == ("POST", "/kabusapi/sendorder") and calls[-1][2]["CashMargin"] == 2
        b.place_stop("7203", 100, 2490)
        assert calls[-1][2]["ReverseLimitOrder"]["TriggerPrice"] == 2490
        b.cancel("ID1")
        assert calls[-1] == ("PUT", "/kabusapi/cancelorder", {"OrderId": "ID1"})
        assert b.order("ID1") == OrderStatus(True, 100, 2500.5, (("E77", 100),))
        assert "id=ID1" in calls[-1][1]
        assert b.order("NOPE") == OrderStatus(False)
        assert b.positions() == {"7203": 300}                    # 買建だけを合計する
        assert b.tick_group("7203") == "10003" and calls[-1][1] == "/kabusapi/symbol/7203@1"
        with pytest.raises(KabuApiError):
            b.buy_market("7203", 999)                            # 拒否は例外になる
    finally:
        srv.shutdown()


def test_repay_targets_the_position_opened_by_this_trade():
    b = KabuBroker(None)
    body = b.stop_body("7203", 400, 994, [("E1", 100), ("E2", 300)])
    assert body["ClosePositions"] == [{"HoldID": "E1", "Qty": 100}, {"HoldID": "E2", "Qty": 300}]
    assert "ClosePositionOrder" not in body
    assert b.close_body("7203", 100, [("E1", 100), ("E2", 300)])["ClosePositions"] == [
        {"HoldID": "E1", "Qty": 100}]
    assert b.close_body("7203", 400, [("E1", 100)])["ClosePositionOrder"] == 0   # 足りなければ指定しない
    e, br, _, _ = make()
    br.auto_fill["buy"] = 1007
    t0 = signal(e)
    e.on_price("7203", 1006, t0 + timedelta(seconds=1))
    e.on_clock(t0 + timedelta(seconds=3))
    assert e.open[0].holds == "E-O1:400" and br.holds_seen == [[("E-O1", 400)]]


def test_sell_falls_back_when_position_id_is_rejected():
    from candle.kabu_client import KabuApiError

    class C:
        def __init__(self):
            self.bodies = []

        def send_order(self, body):
            self.bodies.append(body)
            if "ClosePositions" in body:
                raise KabuApiError(400, "建玉が選択されていません。")
            return "OK1"

    c = C()
    assert KabuBroker(c).close_market("7203", 100, [("E1", 100)]) == "OK1"
    assert len(c.bodies) == 2 and c.bodies[1]["ClosePositionOrder"] == 0
