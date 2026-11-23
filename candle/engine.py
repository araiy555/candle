"""リアルタイム仮想売買（第4段階）。実際の注文は一切出さない。

足の確定ごとに全戦略を独立に判定し、サインが出たら次の約定価格で仮想的に買う。
損切り・トレーリング・時間決済は配信価格で判定する。
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import datetime, time

from .bars import JST, Bar
from .patterns import STRATEGIES, STRATEGY_BY_ID, Params, contiguous_tail
from .risk import RiskManager
from .store import Store
from .trade import Trade, summarize

log = logging.getLogger(__name__)
MODE = "paper"


class PaperEngine:
    def __init__(self, store: Store, risk: RiskManager, params: Params | None = None,
                 slippage: float = 0.0005, exit_after: time = time(15, 20),
                 enabled: set[str] | None = None):
        self.store, self.risk = store, risk
        self.params = params or Params()
        self.slippage, self.exit_after = slippage, exit_after
        self.strategies = [s for s in STRATEGIES if enabled is None or s.id in enabled]
        self.history: dict[str, deque[Bar]] = defaultdict(lambda: deque(maxlen=80))
        self.prices: dict[str, float] = {}
        self.watchlist: list[str] = []
        self.pending: dict[str, list[tuple[str, datetime, float]]] = defaultdict(list)
        self.open: list[Trade] = store.load_trades(MODE, open_only=True)
        self.day = None
        self.snapshot: dict = {}
        today = datetime.now(JST).date()
        risk.realized_today = sum(t.pnl() for t in store.load_trades(MODE)
                                  if t.exit_time and t.exit_time.astimezone(JST).date() == today)

    # --- 入力 ---------------------------------------------------------------
    def on_bar(self, bar: Bar) -> None:
        h = self.history[bar.symbol]
        if h and h[-1].start.date() != bar.start.date():
            h.clear()
        h.append(bar)
        if not bar.complete or bar.start.time() >= self.exit_after:
            return
        window = contiguous_tail(list(h))
        for s in self.strategies:
            low = s.detect(window, self.params)
            if low is not None:
                self.pending[bar.symbol].append((s.id, bar.end, low))
                log.info("サイン %s %s %s 安値%s", s.name, bar.symbol,
                         bar.end.strftime("%H:%M"), low)

    def on_price(self, symbol: str, price: float, ts: datetime) -> None:
        self.prices[symbol] = price
        if self.day != ts.date():
            self.day = ts.date()
            self.risk.new_day()
        for t in [t for t in self.open if t.symbol == symbol]:
            if price < t.stop:
                reason = "初期損切り" if t.stop == t.initial_stop else "トレーリング"
                self._close(t, ts, price, reason)
            elif t.trail(price):
                self.store.save_trade(t)
                log.info("損切り引き上げ %s %s → %s", t.strategy_id, symbol, t.stop)
        for strategy_id, sig_time, low in self.pending.pop(symbol, []):
            self._enter(strategy_id, symbol, sig_time, low, price, ts)

    def on_clock(self, now: datetime) -> None:
        now = now.astimezone(JST)
        late = now.time() >= self.exit_after
        if late:
            self.pending.clear()
        for t in list(self.open):
            price = self.prices.get(t.symbol)
            stale = t.entry_time.astimezone(JST).date() < now.date()  # 停止中に持ち越した分
            if price is not None and (late or stale):
                self._close(t, now, price, "時間決済")
        self.snapshot = self.build_snapshot(now)

    # --- 売買 ---------------------------------------------------------------
    def _enter(self, strategy_id, symbol, sig_time, low, price, ts) -> None:
        entry = price * (1 + self.slippage)
        unreal = sum(t.pnl(self.prices.get(t.symbol, t.entry_price)) for t in self.open)
        if ts.time() >= self.exit_after:
            qty, why = 0, "時間外"
        else:
            qty, why = self.risk.check_entry(strategy_id, symbol, entry, low, self.open, unreal)
        self.store.add_signal(MODE, strategy_id, symbol, sig_time, price, low, why)
        if qty <= 0:
            log.info("見送り %s %s: %s", strategy_id, symbol, why)
            return
        t = Trade(MODE, strategy_id, symbol, sig_time, ts, entry, qty, low, low, high=entry)
        self.store.save_trade(t)
        self.open.append(t)
        log.info("仮想買い %s %s %d株 @%.1f 損切り%.1f", strategy_id, symbol, qty, entry, low)

    def _close(self, t: Trade, ts: datetime, price: float, reason: str) -> None:
        t.close(ts, price * (1 - self.slippage), reason)
        self.store.save_trade(t)
        self.open.remove(t)
        self.risk.on_close(t)
        log.info("仮想決済 %s %s %s 損益%+.0f円", t.strategy_id, t.symbol, reason, t.pnl())

    # --- 管理画面用 ---------------------------------------------------------
    def build_snapshot(self, now: datetime) -> dict:
        trades = self.store.load_trades(MODE)
        by_strategy = defaultdict(list)
        for t in trades:
            by_strategy[t.strategy_id].append(t)
        name = lambda sid: STRATEGY_BY_ID[sid].name if sid in STRATEGY_BY_ID else sid
        positions = []
        for t in self.open:
            px = self.prices.get(t.symbol)
            positions.append(dict(
                strategy=name(t.strategy_id), symbol=t.symbol, qty=t.qty,
                signal_time=t.signal_time.isoformat(), entry_price=t.entry_price, price=px,
                initial_stop=t.initial_stop, stop=t.stop,
                pnl=None if px is None else t.pnl(px),
                r=None if px is None else t.r_multiple(px),
                hold_min=(now - t.entry_time).total_seconds() / 60))
        history = [dict(strategy=name(t.strategy_id), symbol=t.symbol, qty=t.qty,
                        entry_time=t.entry_time.isoformat(), entry_price=t.entry_price,
                        exit_time=t.exit_time.isoformat(), exit_price=t.exit_price,
                        reason=t.exit_reason, pnl=t.pnl(), r=t.r_multiple())
                   for t in reversed(trades) if not t.is_open][:200]
        signals = self.store.recent_signals(MODE, 50)
        for s in signals:
            s["strategy"] = name(s.pop("strategy_id"))
        return dict(
            time=now.isoformat(), mode="仮想売買", halted=self.risk.halted,
            feed_ok=self.risk.feed_ok, realized_today=self.risk.realized_today,
            limits=vars(self.risk.limits),
            exposure=sum(t.entry_price * t.qty for t in self.open),
            watchlist=[dict(symbol=s, price=self.prices.get(s)) for s in self.watchlist],
            positions=positions, signals=signals, history=history,
            stats=[dict(strategy=s.name, **summarize(by_strategy.get(s.id, [])))
                   for s in STRATEGIES])
