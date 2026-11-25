import asyncio
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from candle.collector import Pipeline, _parse_time, replay, run_collector
from candle.config import Config
from candle.store import Store


def msg(sym, time_, price, vol, hi=None, lo=None, recv=None):
    d = {"Symbol": sym, "CurrentPrice": price, "CurrentPriceTime": f"2026-11-24T{time_}+09:00",
         "TradingVolume": vol, "HighPrice": hi, "LowPrice": lo, "Sell1": {"Price": 1}}
    if recv:
        d["recv"] = f"2026-11-24T{recv}+09:00"
    return d


def test_pipeline_records_and_replay_roundtrip(tmp_path):
    store = Store(":memory:")
    pipe = Pipeline(store, 5, tmp_path / "raw")
    for m in [msg("7203", "08:55:00", 3000, 0), msg("7203", "09:00:01", 3010, 1000),
              msg("7203", "09:03:00", 3040, 1600), msg("7203", "09:05:01", 3020, 2000),
              {"Symbol": "7203", "CurrentPrice": None, "CurrentPriceTime": None}]:
        # 受信時刻は実行時の時計に左右されないよう、配信の時刻に合わせて固定する
        pipe.handle(m, _parse_time(m["CurrentPriceTime"]) or _parse_time("2026-11-24T09:05:02+09:00"))
    pipe.finish()
    bars = store.load_bars("7203")
    assert [(b.open, b.high, b.low, b.close, b.volume) for b in bars] == [
        (3010, 3040, 3010, 3040, 1600), (3020, 3020, 3020, 3020, 400)]

    files = list((tmp_path / "raw").glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert "Sell1" not in json.loads(lines[0])            # 板は保存しない

    store2 = Store(":memory:")
    replay(lines, store2)
    assert store2.load_bars("7203") == bars


def test_replay_flushes_by_receive_time():
    store = Store(":memory:")
    lines = [json.dumps(m) for m in [
        msg("A", "08:55:00", 100, 0, recv="08:55:00"),
        msg("A", "09:01:00", 101, 10, recv="09:01:00"),
        msg("B", "09:20:00", 50, 0, recv="09:20:00")]]
    replay(lines, store)
    assert [b.start.strftime("%H:%M") for b in store.load_bars("A")] == ["09:00"]


class _MockKabu(BaseHTTPRequestHandler):
    calls: list = []

    def _send(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_POST(self):
        body = self._body()
        if self.path == "/kabusapi/token" and body == {"APIPassword": "pw"}:
            return self._send({"ResultCode": 0, "Token": "tok"})
        self._send({"Code": 4001007, "Message": "ログイン認証エラー"}, 401)

    def do_GET(self):
        if self.headers.get("X-API-KEY") != "tok":
            return self._send({}, 401)
        if self.path.startswith("/kabusapi/ranking"):
            _MockKabu.calls.append(self.path)
            return self._send({"Type": "4", "Ranking": [
                {"No": 1, "Symbol": "7203", "SymbolName": "トヨタ", "Turnover": 90000.0},
                {"No": 2, "Symbol": "1570", "SymbolName": "日経レバ", "Turnover": 80000.0},
                {"No": 3, "Symbol": "6758", "SymbolName": "ソニーG", "Turnover": 70000.0}]})
        self._send({}, 404)

    def do_PUT(self):
        body = self._body()
        _MockKabu.calls.append((self.path, body))
        self._send({"RegistList": (body or {}).get("Symbols", [])})

    def log_message(self, *a):
        pass


def test_collector_end_to_end_with_mock_server(tmp_path, monkeypatch):
    websockets = pytest.importorskip("websockets")
    monkeypatch.setenv("KABU_API_PASSWORD", "pw")
    _MockKabu.calls = []
    http = ThreadingHTTPServer(("127.0.0.1", 0), _MockKabu)
    threading.Thread(target=http.serve_forever, daemon=True).start()

    async def scenario():
        stop = asyncio.Event()

        async def push(ws):
            for m in [msg("7203", "08:55:00", 3000, 0), msg("7203", "09:00:01", 3010, 1000),
                      msg("7203", "09:04:00", 3050, 1700), msg("7203", "09:05:01", 3040, 2000)]:
                await ws.send(json.dumps(m))
            await asyncio.sleep(0.3)
            stop.set()
            await ws.wait_closed()

        async with websockets.serve(push, "127.0.0.1", 0) as server:
            ws_port = server.sockets[0].getsockname()[1]

            @dataclass
            class TestConfig(Config):
                @property
                def ws_url(self):
                    return f"ws://127.0.0.1:{ws_port}/kabusapi/websocket"

            cfg = TestConfig(host="127.0.0.1", port=http.server_address[1], universe_size=2,
                             exclude=["1570"], data_dir=tmp_path)
            await asyncio.wait_for(run_collector(cfg, stop), timeout=20)
            return cfg

    cfg = asyncio.run(scenario())
    http.shutdown()

    registered = [c for c in _MockKabu.calls if isinstance(c, tuple) and c[0].endswith("/register")]
    assert [s["Symbol"] for s in registered[0][1]["Symbols"]] == ["7203", "6758"]  # 除外と件数上限
    store = Store(cfg.db_path)
    bars = store.load_bars("7203")
    assert [(b.open, b.high, b.low, b.close, b.volume, b.complete) for b in bars] == [
        (3010, 3050, 3010, 3050, 1700, True), (3040, 3040, 3040, 3040, 300, True)]
    assert store.latest_watchlist()[1][1]["symbol"] == "6758"
    assert (tmp_path / "raw").exists()


def test_universe_always_includes_held_symbols(tmp_path):
    from candle.universe import select_universe

    class Client:
        def ranking(self, type_, division):
            return [{"Symbol": str(7000 + i), "SymbolName": f"N{i}", "Turnover": 1.0}
                    for i in range(60)]

    store = Store(":memory:")
    cfg = Config(universe_size=50, data_dir=tmp_path)
    got = select_universe(cfg, Client(), store, must=["9434", "7003"])
    assert got[:2] == ["9434", "7003"] and len(got) == 50 and len(set(got)) == 50
    assert "7049" not in got and "7048" in got                   # 圏外の保有銘柄の分だけ末尾を削る
    assert select_universe(cfg, Client(), store)[0] == "7000"


def test_open_symbols_and_known_names():
    from datetime import datetime
    from candle.bars import JST
    from candle.trade import Trade

    store = Store(":memory:")
    now = datetime(2026, 11, 25, 9, 35, tzinfo=JST)
    store.save_trade(Trade("paper", "tweezer_bottom", "9434", now, now, 238.8, 1500, 237.6, 237.6))
    done = Trade("paper", "tweezer_bottom", "7203", now, now, 100, 100, 99, 99)
    done.close(now, 101, "x")
    store.save_trade(done)
    store.save_trade(Trade("backtest", "hammer", "1111", now, now, 100, 100, 99, 99))
    assert store.open_symbols() == ["9434"]
    store.save_watchlist("2026-11-24", [dict(rank=1, symbol="9434", name="ＳＢ", turnover=1)])
    store.save_watchlist("2026-11-25", [dict(rank=1, symbol="7203", name="トヨタ", turnover=1)])
    assert store.known_names() == {"9434": "ＳＢ", "7203": "トヨタ"}
