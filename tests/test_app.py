import json
import urllib.request
from datetime import timedelta

from candle.app import App
from candle.config import Config
from candle.live import LiveEngine
from candle.store import Store
from candle.web import start_web

from helpers import DOWN, make_bars

TWEEZER = DOWN + [(1010, 1011, 995, 998), (998, 1008, 995, 1006)]


def make_app(tmp_path, monkeypatch, **cfg):
    monkeypatch.setenv("KABU_API_PASSWORD", "pw")
    sent = []
    app = App(Config(data_dir=tmp_path, **cfg), notify=lambda *a: sent.append(a))
    app.setup(Store(":memory:"))
    app.set_universe(["7203"], {"7203": "トヨタ"}, 5)
    app.set_feed(True)
    return app, sent


def feed(app, sym="7203"):
    bars = make_bars(TWEEZER, sym)
    for b in bars:
        app.on_bar(b)
    return bars[-1].end


def test_starts_in_watch_mode_and_never_trades(tmp_path, monkeypatch):
    app, sent = make_app(tmp_path, monkeypatch)
    assert app.mode == "watch" and not app.engine.trade_enabled
    t0 = feed(app)
    app.on_price("7203", 1006, t0)
    assert len(sent) == 1 and app.engine.open == []


def test_switch_to_paper_keeps_history_and_trades(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    bars = make_bars(TWEEZER[:-1], "7203")
    for b in bars:
        app.on_bar(b)
    assert app.request_mode("paper") == (True, "")
    assert app.mode == "watch"                                   # 切り替えは次の定期処理で行う
    app.on_clock(bars[-1].end)
    assert app.mode == "paper" and app.engine.trade_enabled and app.engine.risk.feed_ok
    assert len(app.engine.history["7203"]) == len(bars)          # 足を引き継ぐ
    last = make_bars(TWEEZER, "7203")[-1]
    app.on_bar(last)                                             # 切り替え直後の足でも判定できる
    app.on_price("7203", 1006, last.end)
    assert len(app.engine.open) == 1
    ok, msg = app.request_mode("watch")
    assert not ok and "決済" in msg                              # 保有中は切り替えられない
    app.engine.close_all = True
    app.on_clock(last.end + timedelta(seconds=5))
    assert app.request_mode("watch") == (True, "")
    app.on_clock(last.end + timedelta(seconds=6))
    assert app.mode == "watch"


def test_live_mode_only_on_production_port(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch, port=18081)
    ok, msg = app.request_mode("live")
    assert not ok and "18080" in msg
    app2, _ = make_app(tmp_path, monkeypatch)
    assert app2.request_mode("live")[0]
    app2.on_clock(make_bars(TWEEZER)[-1].end)
    assert app2.mode == "live" and isinstance(app2.engine, LiveEngine)
    assert app2.engine.broker.client._password == "pw" and app2.engine.watchlist == ["7203"]
    assert app2.request_mode("bogus")[0] is False


def test_web_mode_switch_endpoint(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path, monkeypatch)
    t0 = make_bars(TWEEZER)[-1].end
    app.on_clock(t0)
    srv = start_web(app, 0, tmp_path)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    post = lambda path: json.load(urllib.request.urlopen(
        urllib.request.Request(base + path, method="POST")))
    try:
        st = json.load(urllib.request.urlopen(base + "/api/state"))
        assert st["can_switch"] and st["mode_key"] == "watch" and st["trading"] is False
        assert post("/api/mode?m=paper") == {"ok": True, "message": ""}
        assert json.load(urllib.request.urlopen(base + "/api/state"))["switching"] is True
        app.on_clock(t0 + timedelta(seconds=1))
        st = json.load(urllib.request.urlopen(base + "/api/state"))
        assert st["mode_key"] == "paper" and st["trading"] is True and st["switching"] is False
        assert post("/api/mode?m=nope")["ok"] is False
    finally:
        srv.shutdown()


def test_launch_login_uses_saved_password_and_replaces_a_wrong_one(tmp_path, monkeypatch):
    import getpass

    from candle import __main__ as main, secretstore
    from candle.kabu_client import KabuApiError, KabuClient

    saved = {"pw": "old"}
    monkeypatch.delenv("KABU_API_PASSWORD", raising=False)
    monkeypatch.setattr(secretstore, "load", lambda: saved["pw"])
    monkeypatch.setattr(secretstore, "save", lambda p: saved.update(pw=p) or True)
    monkeypatch.setattr(secretstore, "delete", lambda: saved.update(pw="") or True)

    def token(self):
        if self._password != "good":
            raise KabuApiError(401, "ログイン認証エラー")
        return "tok"

    monkeypatch.setattr(KabuClient, "token", token)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "good")
    assert main.launch_login(Config(data_dir=tmp_path)) == "good"   # 古い保存分は捨てて入力し直す
    assert saved["pw"] == "good"
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": (_ for _ in ()).throw(AssertionError))
    assert main.launch_login(Config(data_dir=tmp_path)) == "good"   # 2回目は入力なし


def test_secretstore_is_unavailable_off_windows():
    from candle import secretstore
    assert secretstore.load() == "" and secretstore.save("x") is False
