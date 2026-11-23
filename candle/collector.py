"""PUSH配信を受信して分足を作り、保存する。発注は一切行わない。"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .bars import JST, Bar, BarBuilder
from .config import Config
from .kabu_client import KabuClient
from .store import Store
from .universe import select_universe

log = logging.getLogger(__name__)

# 記録・足生成に使う項目だけを残す（板全体は保存しない）
RAW_FIELDS = ("Symbol", "CurrentPrice", "CurrentPriceTime", "TradingVolume", "TradingValue",
              "VWAP", "HighPrice", "LowPrice", "BidPrice", "AskPrice")


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts.replace(tzinfo=JST) if ts.tzinfo is None else ts.astimezone(JST)


class Pipeline:
    """配信1件 → 足生成 → 保存。実接続と記録データの再生で共通に使う。"""

    def __init__(self, store: Store, minutes: int = 5, raw_dir: Path | None = None,
                 on_bar: Callable[[Bar], None] | None = None,
                 on_price: Callable[[str, float, datetime], None] | None = None):
        self.store = store
        self.builder = BarBuilder(minutes)
        self.raw_dir = raw_dir
        self.on_bar = on_bar
        self.on_price = on_price
        self.messages = 0
        self._raw_file = None
        self._raw_day = None

    def handle(self, msg: dict, recv: datetime | None = None) -> list[Bar]:
        self.messages += 1
        slim = {k: msg.get(k) for k in RAW_FIELDS}
        if self.raw_dir is not None:
            self._record(slim, recv or datetime.now(JST))
        ts = _parse_time(slim["CurrentPriceTime"])
        if ts is None or not slim["Symbol"]:
            return []
        bars = self.builder.on_tick(str(slim["Symbol"]), ts, slim["CurrentPrice"],
                                    slim["TradingVolume"], slim["HighPrice"], slim["LowPrice"])
        self._emit(bars)
        if self.on_price and slim["CurrentPrice"] is not None:
            self.on_price(str(slim["Symbol"]), float(slim["CurrentPrice"]), ts)
        return bars

    def flush(self, now: datetime | None = None) -> list[Bar]:
        bars = self.builder.flush(now or datetime.now(JST))
        self._emit(bars)
        return bars

    def finish(self) -> list[Bar]:
        bars = self.builder.close_all()
        self._emit(bars)
        if self._raw_file:
            self._raw_file.close()
            self._raw_file = None
        return bars

    def _emit(self, bars: list[Bar]) -> None:
        if not bars:
            return
        self.store.save_bars(bars)
        for b in bars:
            log.debug("足確定 %s %s O%s H%s L%s C%s V%s%s", b.symbol, b.start.strftime("%H:%M"),
                      b.open, b.high, b.low, b.close, b.volume, "" if b.complete else " (不完全)")
            if self.on_bar:
                self.on_bar(b)

    def _record(self, slim: dict, recv: datetime) -> None:
        day = recv.strftime("%Y%m%d")
        if day != self._raw_day:
            if self._raw_file:
                self._raw_file.close()
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            self._raw_file = open(self.raw_dir / f"{day}.jsonl", "a", encoding="utf-8")
            self._raw_day = day
        self._raw_file.write(json.dumps({"recv": recv.isoformat(), **slim},
                                        ensure_ascii=False) + "\n")


def replay(lines: Iterable[str], store: Store, minutes: int = 5) -> Pipeline:
    """記録したJSONLを再生して足を作り直す（検証・バックテスト用）。"""
    pipe = Pipeline(store, minutes)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        recv = _parse_time(msg.get("recv"))
        pipe.handle(msg, recv)
        if recv:
            pipe.flush(recv)
    pipe.finish()
    return pipe


async def run_collector(cfg: Config, stop: asyncio.Event | None = None,
                        engine_factory=None) -> None:
    """トークン取得 → 監視銘柄選定 → 登録 → PUSH受信。切断時は自動で再接続する。"""
    import websockets  # 実接続時のみ必要

    stop = stop or asyncio.Event()
    store = Store(cfg.db_path)
    engine = engine_factory(store) if engine_factory else None
    pipe = Pipeline(store, cfg.bar_minutes, cfg.data_dir / "raw" if cfg.record_raw else None,
                    on_bar=engine.on_bar if engine else None,
                    on_price=engine.on_price if engine else None)

    def feed(ok: bool) -> None:
        if engine:
            engine.risk.feed_ok = ok
    client = KabuClient(cfg.rest_url, cfg.api_password)

    async def flusher():
        while not stop.is_set():
            await asyncio.sleep(1)
            pipe.flush()
            if engine:
                engine.on_clock(datetime.now(JST))

    flush_task = asyncio.create_task(flusher())
    backoff = 1.0
    try:
        while not stop.is_set():
            try:
                await asyncio.to_thread(client.token)
                # SQLiteは作成したスレッドでのみ使えるため、ここは同期で呼ぶ（起動時のみ・短時間）
                symbols = select_universe(cfg, client, store)
                await asyncio.to_thread(client.unregister_all)
                await asyncio.to_thread(client.register, symbols)
                log.info("監視銘柄 %d 件を登録しました: %s", len(symbols), " ".join(symbols))
                async with websockets.connect(cfg.ws_url, ping_interval=None,
                                              max_size=None) as ws:
                    log.info("PUSH配信に接続しました (%s)", cfg.ws_url)
                    backoff = 1.0
                    if engine:
                        engine.watchlist = symbols
                        wl = store.latest_watchlist()
                        engine.names = {r["symbol"]: r["name"] for r in wl[1]} if wl else {}
                    feed(True)
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        pipe.handle(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 切断・kabuステーション未起動など
                feed(False)  # 切断中は新規の仮想買いを止める
                pipe.builder.mark_gap()
                log.warning("接続エラー: %s — %.0f秒後に再接続します", e, backoff)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
    finally:
        flush_task.cancel()
        pipe.finish()
        store.close()
        log.info("終了しました（受信 %d 件）", pipe.messages)
