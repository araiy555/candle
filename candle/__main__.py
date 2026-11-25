from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load_config


def launch_login(cfg) -> str:
    """APIパスワードを用意して接続を確かめる。成功したパスワードを返す（失敗時は空文字）。

    一度成功したパスワードは暗号化して保存し、次回からは入力を省く。
    """
    import getpass
    import os

    from . import secretstore
    from .kabu_client import KabuApiError, KabuClient

    password = os.environ.get("KABU_API_PASSWORD", "")
    from_store = False
    if not password:
        password = secretstore.load()
        from_store = bool(password)
    for _ in range(5):
        if not password:
            print("本番用のAPIパスワードを入力して Enter を押してください（入力した文字は表示されません）")
            password = getpass.getpass("APIパスワード: ").strip()
            from_store = False
            if not password:
                continue
        try:
            KabuClient(cfg.rest_url, password).token()
        except KabuApiError as e:
            if e.status == 401:
                print("APIパスワードが違います。" + ("保存していたパスワードを削除しました。" if from_store else ""))
                secretstore.delete()
                password = ""
                continue
            print(e)
            return ""
        except Exception:
            print(f"kabuステーションに接続できません（ポート{cfg.port}）。")
            print("kabuステーションを起動してログインしてから、Enter を押してください。")
            try:
                input()
            except EOFError:
                return ""
            continue
        if not from_store and secretstore.save(password):
            print("APIパスワードを暗号化して保存しました。次回からは入力不要です。")
        print("kabuステーションに接続しました。")
        return password
    return ""


def run_app(cfg, open_browser: bool = False) -> int:
    """起動する。モードの切り替えと設定はブラウザの管理画面で行う。"""
    from .app import App
    from .collector import run_collector
    from .kabuwin import show_chart_async
    from .notify import Notifier
    from .web import start_web

    cfg.universe_size = 50   # 登録上限いっぱいまで監視する
    notifier = Notifier(cfg.notify_desktop, cfg.notify_sound, cfg.webhook_url,
                        cfg.notify_copy_code)
    app = App(cfg, notify=notifier.send, chart_switcher=show_chart_async)
    if not app.password:
        print("APIパスワードがありません。start.bat から起動してください。")
        return 1
    start_web(app, cfg.web_port, cfg.data_dir, cfg.db_path)
    url = f"http://localhost:{cfg.web_port}"
    print(f"管理画面: {url}")
    print("モードの切り替えと設定は管理画面で行います。起動直後は「通知のみ」です。")
    print("この画面を閉じると、プログラムが止まります。")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        asyncio.run(run_collector(cfg, app=app))
    except KeyboardInterrupt:
        pass
    return 0


def order_check(cfg) -> int:
    """検証用環境に、本番と同じ形の注文を送って応答を表示する。実際の注文にはならない。"""
    import getpass
    import json
    import os

    from .broker import KabuBroker
    from .kabu_client import KabuApiError, KabuClient

    pw = os.environ.get("KABU_API_PASSWORD_TEST") or getpass.getpass("検証用のAPIパスワード: ")
    client = KabuClient(f"http://{cfg.host}:18081/kabusapi", pw)
    broker = KabuBroker(client, cfg.live)
    try:
        client.token()
    except Exception as e:
        print(f"検証用環境（ポート18081）に接続できません: {e}")
        return 1
    print("検証用環境に接続しました。本番と同じ形の注文を送ります（実際の注文にはなりません）。\n")
    dummy = [("E20200924xxxxx", 100)]   # 検証用の仮の建玉番号
    steps = [("信用新規買い・成行", broker.buy_body("7203", 100)),
             ("信用返済売り・逆指値（建玉番号を指定）", broker.stop_body("7203", 100, 2000, dummy)),
             ("信用返済売り・成行（建玉番号を指定）", broker.close_body("7203", 100, dummy)),
             ("信用返済売り・成行（建玉番号の指定なし）", broker.close_body("7203", 100))]
    bad = 0
    for label, body in steps:
        try:
            res = client._request("POST", "/sendorder", json=body)
            if res.get("Result") == 0:
                print(f"[受付] {label}")
            else:
                bad += 1
                print(f"[拒否] {label}: {res}")
        except KabuApiError as e:
            if '"Code":1009001' in e.body.replace(" ", ""):
                # 検証用環境には建玉が無いので、返済注文はここで止まる。形式の誤りではない。
                print(f"[保留] {label}: 検証用環境に建玉が無いため、これ以上は確かめられません")
            else:
                bad += 1
                print(f"[拒否] {label}: {e}")
                print("       送った内容: " + json.dumps(body, ensure_ascii=False))
    try:
        client.cancel_order("20200529A01N06848002")
        print("[受付] 取消")
    except KabuApiError as e:
        if '"Code":4004001' in e.body.replace(" ", ""):
            print("[受付] 取消: 該当する注文が無いという応答（形式は正しい）")
        else:
            bad += 1
            print(f"[拒否] 取消: {e}")
    for label, call in (("注文照会", lambda: client.orders()),
                        ("建玉照会", lambda: client.positions(product=2))):
        try:
            print(f"[受付] {label}: {len(call())} 件")
        except Exception as e:
            bad += 1
            print(f"[拒否] {label}: {e}")
    print("\n形式の誤りは見つかりませんでした。" if not bad
          else "\n拒否された項目があります。上の内容を開発者に伝えてください。")
    return 0 if not bad else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="candle", description="kabuステーションAPI ローソク足システム")
    ap.add_argument("--config", help="設定ファイル（既定: config.toml）")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("notify-test", help="通知が届くかを試す")
    p = sub.add_parser("kabu-chart", help="kabuステーションのチャートを指定銘柄に切り替える（実験的）")
    p.add_argument("symbol")
    p.add_argument("--inspect", action="store_true", help="チャート画面の構造を表示する（調査用）")
    sub.add_parser("check", help="接続確認（トークン発行とランキング取得のみ）")
    sub.add_parser("collect", help="PUSH配信を受信して5分足を保存する")
    sub.add_parser("launch", help="start.bat 用: パスワードの用意と接続確認をしてから起動し、ブラウザを開く")
    sub.add_parser("forget-password", help="保存したAPIパスワードを削除する")
    sub.add_parser("run", help="起動する。モードの切り替えと設定はブラウザの管理画面で行う")
    sub.add_parser("watch", help="売買代金上位50銘柄を監視し、サインが出たら通知する（売買なし）")
    sub.add_parser("paper", help="テスト: 仮想売買と管理画面を起動する（実際の注文は出さない）")
    p = sub.add_parser("live", help="本番: 実際に発注する自動売買を起動する")
    p.add_argument("--yes", action="store_true", help="開始前の確認を省く")
    sub.add_parser("order-check", help="検証用環境（ポート18081）に注文を送り、形式が受け付けられるか確かめる")
    p = sub.add_parser("backtest", help="保存済みの5分足で戦略別バックテストを行う")
    p.add_argument("--source", help="データの出所で絞る（kabu_push / yahoo）")
    p = sub.add_parser("import-yahoo", help="Yahoo Financeから5分足を取り込む（予備検証用）")
    p.add_argument("symbols", nargs="*", help="銘柄コード（省略時は保存済みの監視銘柄）")
    p = sub.add_parser("replay", help="記録したJSONLから足を作り直す")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("bars", help="保存済みの足を表示する")
    p.add_argument("symbol")
    p.add_argument("-n", type=int, default=20)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    if args.cmd == "kabu-chart":
        from .kabuwin import show_chart
        ok, message = show_chart(args.symbol, args.inspect)
        print(message)
        return 0 if ok else 1

    if args.cmd == "notify-test":
        import time
        from .notify import Notifier
        Notifier(cfg.notify_desktop, cfg.notify_sound, cfg.webhook_url, cfg.notify_copy_code).send(
            "毛抜き底  7203 トヨタ（テスト）", "10:35 終値 3,010.0 / パターン安値 2,995.0", "7203")
        time.sleep(8)
        print("通知を送りました。画面右下に表示されたか確認してください。")
        return 0

    if args.cmd == "check":
        from .kabu_client import KabuClient
        from .kabu_client import KabuApiError
        client = KabuClient(cfg.rest_url, cfg.api_password)
        kind = "検証用" if cfg.port == 18081 else "本番用"
        try:
            client.token()
        except KabuApiError as e:
            if e.status == 401:
                print(f"APIパスワードが違います。いま接続しているのは{kind}（ポート{cfg.port}）です。")
                print(f"kabuステーションで設定した「{kind}」のAPIパスワードを入力してください。")
                print("本番用と検証用は別のパスワードです。切り替えは config.toml の port で行います"
                      "（本番 18080 / 検証 18081）。")
            else:
                print(e)
            return 1
        except Exception as e:
            print(f"kabuステーションに接続できません（ポート{cfg.port}）: {e}")
            print("kabuステーションを起動してログインし、APIを有効にしているか確認してください。")
            return 1
        print(f"トークン発行: OK ({cfg.rest_url})")
        rows = client.ranking(4, cfg.exchange_division)
        print(f"売買代金ランキング: {len(rows)} 件")
        for r in rows[:5]:
            print(f"  {r.get('No')}位 {r.get('Symbol')} {r.get('SymbolName')} "
                  f"{r.get('Turnover')}百万円")
        return 0

    if args.cmd == "collect":
        from .collector import run_collector
        try:
            asyncio.run(run_collector(cfg))
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "order-check":
        return order_check(cfg)

    if args.cmd == "forget-password":
        from . import secretstore
        print("保存していたAPIパスワードを削除しました。" if secretstore.delete()
              else "保存されたAPIパスワードはありません。")
        return 0

    if args.cmd == "launch":
        password = launch_login(cfg)
        if not password:
            return 1
        import os
        os.environ["KABU_API_PASSWORD"] = password
        return run_app(cfg, open_browser=True)

    if args.cmd == "run":
        return run_app(cfg)

    if args.cmd in ("paper", "watch", "live"):
        from datetime import time as dtime
        from .collector import run_collector
        from .engine import PaperEngine
        from .kabuwin import show_chart_async
        from .notify import Notifier
        from .patterns import STRATEGIES
        from .risk import RiskManager
        from .settings import TradeSettings
        from .web import start_web

        watch, live = args.cmd == "watch", args.cmd == "live"
        notifier = Notifier(cfg.notify_desktop, cfg.notify_sound, cfg.webhook_url,
                            cfg.notify_copy_code)
        cfg.universe_size = 50   # 登録上限いっぱいまで監視する
        enabled = set(cfg.watch_strategies if watch else cfg.trade_strategies)
        unknown = enabled - {s.id for s in STRATEGIES}
        if unknown:
            print(f"設定に不明な戦略IDがあります: {sorted(unknown)}")
            return 1
        if live:
            if cfg.port != 18080:
                print("本番モードは本番用ポート（18080）でのみ動かせます。config.toml の port を確認してください。")
                return 1
            if not args.yes:
                print("=" * 60)
                print("本番モードです。実際の資金で信用取引の注文を出します。")
                print("=" * 60)
                try:
                    answer = input("開始する場合は yes と入力して Enter: ")
                except EOFError:
                    answer = ""
                if answer.strip().lower() != "yes":
                    print("中止しました。")
                    return 1

        def factory(store):
            h, m = map(int, cfg.exit_after.split(":"))
            common = dict(slippage=cfg.slippage, exit_after=dtime(h, m), enabled=enabled,
                          notify=notifier.send, notify_ids=set(cfg.watch_strategies),
                          settings=TradeSettings.load(cfg.data_dir / "settings.json"))
            if live:
                from .broker import KabuBroker
                from .kabu_client import KabuClient
                from .live import LiveEngine
                # 発注用の接続は受信用とは別に持つ（応答待ちを短くして受信を止めないため）
                client = KabuClient(cfg.rest_url, cfg.api_password, timeout=5.0)
                engine = LiveEngine(store, RiskManager(cfg.risk), broker=KabuBroker(client, cfg.live),
                                    stop_method=cfg.live.stop_method, **common)
            else:
                engine = PaperEngine(store, RiskManager(cfg.risk), trade_enabled=not watch, **common)
            flag = cfg.data_dir / "auto_chart.txt"
            engine.on_auto_chart = show_chart_async
            engine.auto_chart = flag.exists() and flag.read_text().strip() == "1"
            start_web(engine, cfg.web_port, cfg.data_dir, cfg.db_path)
            print(f"モード: {engine.mode_label if engine.trade_enabled else '通知のみ'}")
            print(f"管理画面: http://localhost:{cfg.web_port}")
            return engine

        try:
            asyncio.run(run_collector(cfg, engine_factory=factory))
        except KeyboardInterrupt:
            pass
        return 0

    from .store import Store
    store = Store(cfg.db_path)
    if args.cmd == "backtest":
        from .backtest import format_report, run_backtest
        data = {}
        for sym in store.symbols_with_bars(cfg.bar_minutes):
            bars = store.load_bars(sym, cfg.bar_minutes, source=args.source)
            if bars:
                data[sym] = bars
        if not data:
            print("5分足がありません。collect で蓄積するか import-yahoo で取り込んでください。")
            return 1
        days = len({b.start.date() for bars in data.values() for b in bars})
        results = run_backtest(data, limits=cfg.risk, slippage=cfg.slippage)
        store.delete_trades("backtest")
        for trades in results.values():
            for t in trades:
                store.save_trade(t)
        print(format_report(results, days, len(data)))
    elif args.cmd == "import-yahoo":
        from .yahoo import fetch
        symbols = args.symbols
        if not symbols:
            wl = store.latest_watchlist()
            symbols = [r["symbol"] for r in wl[1]] if wl else []
        if not symbols:
            print("銘柄コードを指定してください。例: python -m candle import-yahoo 7203 6758")
            return 1
        for sym in symbols:
            try:
                bars = fetch(sym, minutes=cfg.bar_minutes)
            except Exception as e:
                print(f"{sym}: 取得失敗 ({e})")
                continue
            store.save_bars(bars, source="yahoo", replace=False)
            print(f"{sym}: {len(bars)} 本")
    elif args.cmd == "replay":
        from .collector import replay
        for f in args.files:
            with open(f, encoding="utf-8") as fh:
                pipe = replay(fh, store, cfg.bar_minutes)
            print(f"{f}: {pipe.messages} 件を再生しました")
    elif args.cmd == "bars":
        for b in store.load_bars(args.symbol, cfg.bar_minutes, args.n):
            print(f"{b.start:%Y-%m-%d %H:%M} O{b.open:g} H{b.high:g} L{b.low:g} C{b.close:g} "
                  f"V{b.volume:g}{'' if b.complete else ' *不完全'}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
