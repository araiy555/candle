from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load_config


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
    sub.add_parser("watch", help="売買代金上位50銘柄を監視し、サインが出たら通知する（売買なし）")
    sub.add_parser("paper", help="仮想売買と管理画面を起動する（実際の注文は出さない）")
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

    if args.cmd in ("paper", "watch"):
        from datetime import time as dtime
        from .collector import run_collector
        from .engine import PaperEngine
        from .notify import Notifier
        from .patterns import STRATEGIES
        from .risk import RiskManager
        from .web import start_web

        watch = args.cmd == "watch"
        notifier = Notifier(cfg.notify_desktop, cfg.notify_sound, cfg.webhook_url, cfg.notify_copy_code)
        if watch:
            cfg.universe_size = 50   # 売買しないので予備枠は不要。登録上限いっぱいまで監視する
            enabled = set(cfg.watch_strategies)
            unknown = enabled - {s.id for s in STRATEGIES}
            if unknown:
                print(f"notify.strategies に不明な戦略IDがあります: {sorted(unknown)}")
                return 1
        else:
            enabled = {s.id for s in STRATEGIES} - set(cfg.disabled_strategies)

        def factory(store):
            h, m = map(int, cfg.exit_after.split(":"))
            engine = PaperEngine(store, RiskManager(cfg.risk), slippage=cfg.slippage,
                                 exit_after=dtime(h, m), enabled=enabled,
                                 trade_enabled=not watch, notify=notifier.send,
                                 notify_ids=set(cfg.watch_strategies))
            from .kabuwin import show_chart_async
            flag = cfg.data_dir / "auto_chart.txt"
            engine.on_auto_chart = show_chart_async
            engine.auto_chart = flag.exists() and flag.read_text().strip() == "1"
            start_web(engine, cfg.web_port, flag)
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
