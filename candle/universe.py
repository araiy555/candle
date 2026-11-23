"""監視銘柄の選定。"""
from __future__ import annotations

import logging
from datetime import datetime

from .bars import JST
from .config import Config
from .kabu_client import KabuClient
from .store import Store

log = logging.getLogger(__name__)


def select_universe(cfg: Config, client: KabuClient, store: Store) -> list[str]:
    """売買代金上位から監視銘柄を決める。

    優先順: 設定の固定リスト → 当日のランキング → 前回保存した監視銘柄。
    ランキングは寄り付き前（7:53頃〜9:00過ぎ）は空になるため、前回分で代用する。
    """
    if cfg.symbols:
        return [s for s in cfg.symbols if s not in cfg.exclude][: cfg.universe_size]

    rows = []
    for r in client.ranking(4, cfg.exchange_division):
        sym = str(r.get("Symbol", ""))
        if not sym or sym in cfg.exclude or any(x["symbol"] == sym for x in rows):
            continue
        rows.append(dict(rank=len(rows) + 1, symbol=sym, name=r.get("SymbolName"),
                         turnover=r.get("Turnover")))
        if len(rows) >= cfg.universe_size:
            break

    if rows:
        store.save_watchlist(datetime.now(JST).date().isoformat(), rows)
        return [r["symbol"] for r in rows]

    prev = store.latest_watchlist()
    if prev:
        log.warning("ランキングが空のため %s の監視銘柄を使用します", prev[0])
        return [r["symbol"] for r in prev[1] if r["symbol"] not in cfg.exclude][: cfg.universe_size]
    raise RuntimeError(
        "ランキングが空で、保存済みの監視銘柄もありません。"
        "9時過ぎに再実行するか、config.toml の universe.symbols に銘柄を指定してください。")
