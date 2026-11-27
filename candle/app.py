"""ブラウザから操作するための本体。モード（通知のみ・テスト・本番）を動かしたまま切り替える。

起動直後は必ず「通知のみ」。本番への切り替えは、ブラウザで明示的に操作したときだけ行う。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, time as dtime

from .config import Config
from .engine import PaperEngine
from .patterns import NOTIFY_CHOICES, STRATEGIES
from .risk import RiskManager
from .settings import TradeSettings

log = logging.getLogger(__name__)
MODES = {"watch": "通知のみ", "paper": "テスト（仮想売買）", "live": "本番（実際に発注）"}


class App:
    def __init__(self, cfg: Config, notify=None, chart_switcher=None):
        self.cfg = cfg
        self.notify = notify
        self.chart_switcher = chart_switcher
        self.password = os.environ.get("KABU_API_PASSWORD", "")
        self.login_error = ""
        self.mode = "watch"
        self.want: str | None = None    # ブラウザから要求された切り替え先
        self.engine: PaperEngine | None = None
        self.store = None
        self.client = None              # 受信側と共有するAPI接続
        self.problems: dict[str, str] = {}
        self._tape_lock = threading.Lock()
        self.wallet: tuple[float | None, str] = (None, "未取得")   # (信用新規可能額, 状況)
        self._wallet_at: datetime | None = None
        self._wallet_wait = 60.0      # 次に余力を取りに行くまでの秒数
        self._hist_queue: queue.Queue = queue.Queue()   # 取り込んだ過去データ（別スレッド → 受信スレッド）
        self._hist_started = False

    # --- 受信側（collector）から呼ばれる ---------------------------------------
    def setup(self, store) -> None:
        self.store = store
        self.engine = self._build("watch", None)

    def on_bar(self, bar) -> None:
        self.engine.on_bar(bar)

    def on_price(self, symbol, price, ts) -> None:
        self.engine.on_price(symbol, price, ts)

    def on_board(self, symbol, msg, now) -> None:
        self.engine.on_board(symbol, msg, now)

    def on_clock(self, now: datetime) -> None:
        if self.want and self.want != self.mode:
            self.engine = self._build(self.want, self.engine)
            self.mode = self.want
            log.info("モードを切り替えました: %s", MODES[self.mode])
        self.want = None
        self._refresh_wallet(now)
        self._apply_history()
        self.engine.buying_power, self.engine.buying_power_note = self.wallet
        self.engine.on_clock(now)

    def set_market(self, symbols, labels, problems) -> None:
        self.engine.market.set_sources(symbols, labels, problems)

    def set_feed(self, ok: bool) -> None:
        self.engine.set_feed(ok)

    def set_universe(self, symbols: list[str], names: dict[str, str], minutes: int) -> None:
        self.engine.watchlist, self.engine.names = symbols, names
        self.engine.load_history(symbols, minutes)
        self.fetch_history(symbols)

    # --- 過去データ（1時間足・日足）の取り込み ---------------------------------
    def fetch_history(self, symbols: list[str], fetcher=None, pause: float = 0.6) -> None:
        """上位足の流動性を見るために、過去の1時間足と日足を1日1回だけ取り込む（別スレッド）。

        kabuステーションAPIには過去の足を取る手段が無いので、公開されている株価データを使う。
        取れなくても動作には影響しない（自分で受信した分だけで表示する）。
        """
        if self._hist_started or not self.cfg.fetch_history:
            return
        if fetcher is None and os.environ.get("CANDLE_NO_HISTORY"):
            return                                  # テスト中は外へ取りに行かない
        self._hist_started = True
        mark = self.cfg.data_dir / "history_fetched.json"
        try:
            done = json.loads(mark.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            done = {}
        today = datetime.now().strftime("%Y-%m-%d")
        todo = [s for s in symbols if done.get(s) != today and s.isdigit()]

        def work():
            from .yahoo import fetch_history as default
            get, failed = fetcher or default, 0
            for sym in todo:
                try:
                    self._hist_queue.put((sym, get(sym, 60), get(sym, 1440)))
                    done[sym] = today
                    failed = 0
                except Exception as e:            # 取れない銘柄・通信できない場合は飛ばす
                    failed += 1
                    log.info("過去データを取れませんでした %s: %s", sym, e)
                    if failed >= 5:
                        self.report("history", "過去の株価データを取り込めませんでした"
                                               "（上位足の流動性は、受信した分だけで表示します）")
                        break
                time.sleep(pause)
            try:
                mark.parent.mkdir(parents=True, exist_ok=True)
                mark.write_text(json.dumps(done), encoding="utf-8")
            except OSError:
                pass

        if todo:
            threading.Thread(target=work, daemon=True).start()

    def _apply_history(self) -> None:
        """取り込んだ過去データを保存して、表示に反映する（データベースは受信スレッドからだけ触る）。"""
        for _ in range(5):
            try:
                sym, hourly, daily = self._hist_queue.get_nowait()
            except queue.Empty:
                return
            self.store.save_bars(hourly, source="history", replace=False)
            self.store.save_bars(daily, source="history", replace=False)
            self.engine.rebuild_history(sym)

    def login_failed(self) -> None:
        # 起動時に確認済みのパスワードなので消さない（消すと再接続できなくなる）。画面に知らせて再試行する。
        self.login_error = "APIパスワードが受け付けられませんでした。kabuステーションの状態を確認してください。"

    def set_client(self, client) -> None:
        """受信側が作った接続を共有する。

        kabuステーションはトークンを発行し直すと前のトークンが無効になるため、
        発注や歩み値の取得で別のトークンを作らないようにする。
        """
        self.client = client
        broker = getattr(self.engine, "broker", None)
        if broker is not None:
            broker.client = client

    def report(self, key: str, message: str | None) -> None:
        if message:
            self.problems[key] = message
        else:
            self.problems.pop(key, None)
        self._show_problems()

    def _show_problems(self) -> None:
        if self.engine is not None:
            for k in ("feed", "handle"):
                self.engine.warnings.pop(k, None)
            self.engine.warnings.update(self.problems)

    # --- ブラウザ側（web）から呼ばれる -----------------------------------------
    def login(self, password: str) -> None:
        self.login_error = ""
        self.password = password.strip()

    def request_mode(self, mode: str) -> tuple[bool, str]:
        if mode not in MODES:
            return False, "不明なモードです"
        e = self.engine
        if e is None:
            return False, "まだ起動中です"
        if mode == self.mode:
            return True, ""
        if e.trade_enabled and e.open:
            return False, "保有中のポジションがあります。先に「全ポジションを決済」を押して、決済が終わってから切り替えてください。"
        if mode == "live" and self.cfg.port != 18080:
            return False, "本番は本番用ポート（18080）でのみ使えます。config.toml の port を確認してください。"
        self.want = mode
        return True, ""

    def _refresh_wallet(self, now: datetime) -> None:
        """信用の買付余力を1分ごとに取得する（受信を止めないよう別スレッドで）。"""
        if self.client is None or (self._wallet_at and (now - self._wallet_at).total_seconds() < self._wallet_wait):
            return
        self._wallet_at = now

        def work(client=self.client):
            try:
                value = client.wallet_margin()
                self.wallet = (value, f"{now:%H:%M} 取得") if value is not None else \
                    (None, "応答に金額が含まれていません")
                self._wallet_wait = 60
            except Exception as e:
                # 失敗のたびにトークンを取り直すことになるので、次に試すまで30分あける
                self._wallet_wait = 1800
                hint = "信用口座が無いか、kabuステーション側で余力を取得できない状態です。" if "401" in str(e) else ""
                self.wallet = (None, f"取得できません。{hint}30分後にもう一度試します（{e}）")

        threading.Thread(target=work, daemon=True).start()

    def _fetch_tape(self, symbol: str) -> list[dict]:
        """歩み値APIから当日の約定を取る（スクイーズ検知の記録用。秒間2件の制限あり）。"""
        if self.client is None:
            raise RuntimeError("APIに接続していません")
        with self._tape_lock:          # 秒間2件の制限があるため、間隔を空けて1件ずつ
            time.sleep(0.6)
            return self.client.time_and_sales(symbol)

    # --- エンジンの作り直し -----------------------------------------------------
    def _build(self, mode: str, old: PaperEngine | None) -> PaperEngine:
        cfg = self.cfg
        h, m = map(int, cfg.exit_after.split(":"))
        ids = {s.id for s in STRATEGIES}
        enabled = set(cfg.watch_strategies if mode == "watch" else cfg.trade_strategies) & ids
        common = dict(slippage=cfg.slippage, exit_after=dtime(h, m), enabled=enabled,
                      notify=self.notify, notify_ids=set(cfg.watch_strategies) | set(NOTIFY_CHOICES),
                      settings=TradeSettings.load(cfg.data_dir / "settings.json"),
                      squeeze_log=old.squeeze_log if old is not None else None,
                      market=old.market if old is not None else None,
                      tape_fetcher=self._fetch_tape)
        risk = RiskManager(cfg.risk)
        if mode == "live":
            from .broker import KabuBroker
            from .kabu_client import KabuClient
            from .live import LiveEngine
            client = self.client or KabuClient(cfg.rest_url, self.password, timeout=5.0)
            engine = LiveEngine(self.store, risk, broker=KabuBroker(client, cfg.live),
                                stop_method=cfg.live.stop_method, **common)
        else:
            engine = PaperEngine(self.store, risk, trade_enabled=mode != "watch", **common)
        engine.on_auto_chart = self.chart_switcher
        flag = cfg.data_dir / "auto_chart.txt"
        engine.auto_chart = flag.exists() and flag.read_text().strip() == "1"
        if old is not None:   # 受信済みの足や価格は引き継ぐ（切り替えで判定を途切れさせない）
            engine.history, engine.prices = old.history, old.prices
            for name in ("prev_days", "hourly_past", "last_day", "daily", "_own_past", "_history_ref"):
                setattr(engine, name, getattr(old, name))     # 過去の足・上位足の流動性も引き継ぐ
            engine.watchlist, engine.names = old.watchlist, old.names
            engine.quotes = old.quotes
            engine.buying_power, engine.buying_power_note = self.wallet
            engine.apply_sizing()
            engine.auto_chart = old.auto_chart
            engine.risk.feed_ok = old.risk.feed_ok
            if engine.flow is not None and old.flow is not None:
                engine.flow = old.flow   # 直近の約定の流れも引き継ぐ
            if engine.walls is not None and old.walls is not None:
                engine.walls = old.walls   # 追跡中の売り板も引き継ぐ
            if engine.opening is not None and old.opening is not None:
                engine.opening, engine.open_active = old.opening, old.open_active
            engine.last_tick, engine.last_bar = old.last_tick, old.last_bar
            engine.warnings.update(self.problems)
            engine.snapshot = {}
        return engine
