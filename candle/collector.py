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
from .kabu_client import KabuApiError, KabuClient
from .store import Store
from .universe import select_universe

log = logging.getLogger(__name__)

# 記録・足生成に使う項目だけを残す（板全体は保存しない）
RAW_FIELDS = ("Symbol", "CurrentPrice", "CurrentPriceTime", "TradingVolume", "TradingValue",
              "VWAP", "HighPrice", "LowPrice", "BidPrice", "AskPrice", "BidQty", "AskQty")


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
                 on_price: Callable[[str, float, datetime], None] | None = None,
                 on_board: Callable[[str, dict, datetime], None] | None = None):
        self.store = store
        self.builder = BarBuilder(minutes)
        self.raw_dir = raw_dir
        self.on_bar = on_bar
        self.on_price = on_price
        self.on_board = on_board
        self.messages = 0
        self._raw_file = None
        self._raw_day = None

    def handle(self, msg: dict, recv: datetime | None = None) -> list[Bar]:
        self.messages += 1
        slim = {k: msg.get(k) for k in RAW_FIELDS}
        if self.raw_dir is not None:
            self._record(slim, recv or datetime.now(JST))
        if self.on_board and slim["Symbol"]:
            self.on_board(str(slim["Symbol"]), msg, recv or datetime.now(JST))
        ts = _parse_time(slim["CurrentPriceTime"])
        if ts is None or not slim["Symbol"]:
            return []
        bars = self.builder.on_tick(str(slim["Symbol"]), ts, slim["CurrentPrice"],
                                    slim["TradingVolume"], slim["HighPrice"], slim["LowPrice"],
                                    slim["VWAP"])
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


def market_sources(cfg: Config, client) -> tuple[list[dict], dict, dict, dict]:
    """地合いの判定に使う先物・TOPIXの登録内容を用意する。

    戻り値: (登録する項目, 銘柄コード→種類, 種類→表示名, 種類→取得できない理由)
    """
    items, symbols, labels, problems = [], {}, {"fut": "日経225先物", "topix": "TOPIX"}, {}
    if cfg.market_future:
        try:
            info = client.future_symbol(cfg.market_future, 0)
            sym = str(info["Symbol"])
            items.append({"Symbol": sym, "Exchange": cfg.market_future_exchange})
            symbols[sym] = "fut"
            labels["fut"] = info.get("SymbolName") or labels["fut"]
        except Exception as e:
            problems["fut"] = f"先物の銘柄コードを取得できませんでした（{e}）"
    else:
        problems["fut"] = "設定で使わないことになっています"
    if cfg.market_topix:
        items.append({"Symbol": cfg.market_topix, "Exchange": 1})
        symbols[cfg.market_topix] = "topix"
    else:
        problems["topix"] = "設定で使わないことになっています"
    return items, symbols, labels, problems


class _EngineSink:
    """エンジン1つをそのまま使う場合の受け口（コマンドでモードを指定して起動したとき）。"""

    def __init__(self, cfg: Config, engine):
        self.cfg, self.engine = cfg, engine

    def on_bar(self, bar) -> None:
        if self.engine:
            self.engine.on_bar(bar)

    def on_price(self, symbol, price, ts) -> None:
        if self.engine:
            self.engine.on_price(symbol, price, ts)

    def on_clock(self, now) -> None:
        if self.engine:
            self.engine.on_clock(now)

    def on_board(self, symbol, msg, now) -> None:
        if self.engine:
            self.engine.on_board(symbol, msg, now)

    def set_feed(self, ok: bool) -> None:
        if self.engine:
            self.engine.set_feed(ok)

    def set_universe(self, symbols, names, minutes) -> None:
        if self.engine:
            self.engine.watchlist, self.engine.names = symbols, names
            self.engine.load_history(symbols, minutes)

    @property
    def password(self) -> str:
        return self.cfg.api_password

    def login_failed(self) -> None:
        pass

    def set_client(self, client) -> None:
        pass

    def set_market(self, symbols, labels, problems) -> None:
        if self.engine:
            self.engine.market.set_sources(symbols, labels, problems)

    def report(self, key: str, message: str | None) -> None:
        """接続や処理の問題を画面に出す（message が None なら解消）。"""
        if self.engine:
            if message:
                self.engine.warnings[key] = message
            else:
                self.engine.warnings.pop(key, None)


async def run_collector(cfg: Config, stop: asyncio.Event | None = None,
                        engine_factory=None, app=None) -> None:
    """トークン取得 → 監視銘柄選定 → 登録 → PUSH受信。切断時は自動で再接続する。

    app を渡すと、パスワードの入力とモードの切り替えをブラウザから行える。
    """
    import websockets  # 実接続時のみ必要

    stop = stop or asyncio.Event()
    store = Store(cfg.db_path)
    if app is not None:
        app.setup(store)
        sink = app
    else:
        sink = _EngineSink(cfg, engine_factory(store) if engine_factory else None)
    pipe = Pipeline(store, cfg.bar_minutes, cfg.data_dir / "raw" if cfg.record_raw else None,
                    on_bar=sink.on_bar, on_price=sink.on_price, on_board=sink.on_board)
    feed = sink.set_feed

    async def flusher():
        while not stop.is_set():
            await asyncio.sleep(1)
            pipe.flush()
            try:
                sink.on_clock(datetime.now(JST))
            except Exception:
                log.exception("定期処理でエラーが発生しました")

    flush_task = asyncio.create_task(flusher())
    backoff = 1.0
    errors = 0
    try:
        while not stop.is_set():
            try:
                while not sink.password and not stop.is_set():   # ブラウザでの入力を待つ
                    await asyncio.sleep(0.5)
                if stop.is_set():
                    break
                client = KabuClient(cfg.rest_url, sink.password)
                sink.set_client(client)   # 発注や歩み値の取得も同じ接続（同じトークン）を使う
                try:
                    await asyncio.to_thread(client.token)
                except KabuApiError as e:
                    if e.status == 401:
                        sink.login_failed()
                    raise
                # SQLiteは作成したスレッドでのみ使えるため、ここは同期で呼ぶ（起動時のみ・短時間）
                # 先物・TOPIX（地合いの判定用）の分だけ、個別株の枠を空ける
                extras, mkt_symbols, mkt_labels, mkt_problems = await asyncio.to_thread(
                    market_sources, cfg, client)
                size = cfg.universe_size
                cfg.universe_size = max(1, min(size, 50 - len(extras)))
                try:
                    symbols = select_universe(cfg, client, store, must=store.open_symbols())
                finally:
                    cfg.universe_size = size
                await asyncio.to_thread(client.unregister_all)
                await asyncio.to_thread(client.register, symbols)
                if extras:
                    try:
                        done = await asyncio.to_thread(client.register_items, extras)
                        got = {str(d.get("Symbol")) for d in done}
                        for sym, key in list(mkt_symbols.items()):
                            if sym not in got:
                                mkt_problems[key] = "登録できませんでした（kabuステーションが受け付けませんでした）"
                                del mkt_symbols[sym]
                    except Exception as e:
                        for key in set(mkt_symbols.values()):
                            mkt_problems[key] = f"登録できませんでした（{e}）"
                        mkt_symbols = {}
                sink.set_market(mkt_symbols, mkt_labels, mkt_problems)
                log.info("監視銘柄 %d 件を登録しました: %s", len(symbols), " ".join(symbols))
                async with websockets.connect(cfg.ws_url, ping_interval=None,
                                              max_size=None) as ws:
                    log.info("PUSH配信に接続しました (%s)", cfg.ws_url)
                    backoff = 1.0
                    sink.set_universe(symbols, store.known_names(), cfg.bar_minutes)
                    feed(True)
                    sink.report("feed", None)
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        try:
                            pipe.handle(json.loads(raw))
                        except Exception as e:
                            # 1件の処理の失敗で受信全体を止めない。原因は画面とログに残す。
                            errors += 1
                            if errors <= 5 or errors % 500 == 0:
                                log.exception("配信の処理でエラーが発生しました（%d回目）", errors)
                            sink.report("handle", f"配信の処理でエラーが発生しています（{errors}回）: "
                                                  f"{type(e).__name__}: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 切断・kabuステーション未起動など
                feed(False)  # 切断中は新規の仮想買いを止める
                pipe.builder.mark_gap()
                log.warning("接続エラー: %s — %.0f秒後に再接続します", e, backoff)
                sink.report("feed", f"kabuステーションに接続できていません: {type(e).__name__}: {e}")
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
