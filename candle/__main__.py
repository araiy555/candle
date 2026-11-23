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
    sub.add_parser("check", help="接続確認（トークン発行とランキング取得のみ）")
    sub.add_parser("collect", help="PUSH配信を受信して5分足を保存する")
    p = sub.add_parser("replay", help="記録したJSONLから足を作り直す")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("bars", help="保存済みの足を表示する")
    p.add_argument("symbol")
    p.add_argument("-n", type=int, default=20)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    if args.cmd == "check":
        from .kabu_client import KabuClient
        client = KabuClient(cfg.rest_url, cfg.api_password)
        client.token()
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

    from .store import Store
    store = Store(cfg.db_path)
    if args.cmd == "replay":
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
