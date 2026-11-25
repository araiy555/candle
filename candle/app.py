"""ブラウザから操作するための本体。モード（通知のみ・テスト・本番）を動かしたまま切り替える。

起動直後は必ず「通知のみ」。本番への切り替えは、ブラウザで明示的に操作したときだけ行う。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, time as dtime

from .config import Config
from .engine import PaperEngine
from .patterns import STRATEGIES
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
        self.engine.on_clock(now)

    def set_feed(self, ok: bool) -> None:
        self.engine.risk.feed_ok = ok

    def set_universe(self, symbols: list[str], names: dict[str, str], minutes: int) -> None:
        self.engine.watchlist, self.engine.names = symbols, names
        self.engine.load_history(symbols, minutes)

    def login_failed(self) -> None:
        self.password = ""
        self.login_error = "APIパスワードが違います。本番用のAPIパスワードを入力してください。"

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

    # --- エンジンの作り直し -----------------------------------------------------
    def _build(self, mode: str, old: PaperEngine | None) -> PaperEngine:
        cfg = self.cfg
        h, m = map(int, cfg.exit_after.split(":"))
        ids = {s.id for s in STRATEGIES}
        enabled = set(cfg.watch_strategies if mode == "watch" else cfg.trade_strategies) & ids
        common = dict(slippage=cfg.slippage, exit_after=dtime(h, m), enabled=enabled,
                      notify=self.notify, notify_ids=set(cfg.watch_strategies),
                      settings=TradeSettings.load(cfg.data_dir / "settings.json"))
        risk = RiskManager(cfg.risk)
        if mode == "live":
            from .broker import KabuBroker
            from .kabu_client import KabuClient
            from .live import LiveEngine
            client = KabuClient(cfg.rest_url, self.password, timeout=5.0)
            engine = LiveEngine(self.store, risk, broker=KabuBroker(client, cfg.live),
                                stop_method=cfg.live.stop_method, **common)
        else:
            engine = PaperEngine(self.store, risk, trade_enabled=mode != "watch", **common)
        engine.on_auto_chart = self.chart_switcher
        flag = cfg.data_dir / "auto_chart.txt"
        engine.auto_chart = flag.exists() and flag.read_text().strip() == "1"
        if old is not None:   # 受信済みの足や価格は引き継ぐ（切り替えで判定を途切れさせない）
            engine.history, engine.prices = old.history, old.prices
            engine.watchlist, engine.names = old.watchlist, old.names
            engine.auto_chart = old.auto_chart
            engine.risk.feed_ok = old.risk.feed_ok
            if engine.flow is not None and old.flow is not None:
                engine.flow = old.flow   # 直近の約定の流れも引き継ぐ
            engine.snapshot = {}
        return engine
