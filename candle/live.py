"""実際に発注する自動売買（第5段階）。

注文の流れ（1つの取引につき）:
  entering  買い注文を出して約定を待つ
  open      保有中。損切りの逆指値を証券会社側に置いている（または、このプログラムが監視）
  replacing トレーリングで損切りを引き上げるため、古い逆指値を取り消している最中
  exiting   決済中（逆指値を取り消してから成行で返済）

安全のための決まり:
- 買い注文の結果が分からない場合（通信エラーなど）は再送しない。新規の買いを止めて知らせる。
- 同じ銘柄を同時に2つ持たない（返済の対象を取り違えないため）。
- 逆指値の取消中など、証券会社側に損切りが無い間は、このプログラムが価格を見て成行で決済する。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .bars import JST
from .engine import PaperEngine
from .patterns import STRATEGY_BY_ID
from .trade import Trade, trigger_price

log = logging.getLogger(__name__)

POLL = timedelta(seconds=2)            # 注文状況を確認する間隔
ENTRY_TIMEOUT = timedelta(seconds=30)  # 買いがこの時間で約定しきらなければ残りを取り消す
REPLACE_GAP = timedelta(seconds=5)     # 逆指値の出し直しの最短間隔
BREACH_WAIT = timedelta(seconds=5)     # 逆指値が効くはずの価格になってから待つ時間
RETRY = timedelta(seconds=5)
RECONCILE = timedelta(seconds=60)
STATE_LABELS = {"entering": "買い注文中", "open": "保有中", "replacing": "損切り引き上げ中",
                "exiting": "決済中"}


class LiveEngine(PaperEngine):
    mode = "live"
    mode_label = "本番（実際に発注）"

    def __init__(self, *args, broker=None, stop_method: str = "server", **kwargs):
        super().__init__(*args, **kwargs)
        self.broker = broker
        self.stop_method = stop_method
        self._next: dict[int, datetime] = {}       # 取引ごとの次回確認時刻
        self._since: dict[int, datetime] = {}      # いまの状態になった時刻
        self._cancel_sent: set[tuple[int, str]] = set()
        self._breach: dict[int, datetime] = {}
        self._reason: dict[int, str] = {}
        self._last_replace: dict[int, datetime] = {}
        self._next_reconcile: datetime | None = None
        self._mismatch = 0

    def state_label(self, t: Trade) -> str:
        return STATE_LABELS.get(t.state, t.state)

    # --- 知らせる・記録する ---------------------------------------------------
    def _tell(self, title: str, body: str, symbol: str | None = None) -> None:
        log.warning("%s: %s", title, body)
        if self.notify:
            self.notify(title, body, symbol)

    def _label(self, t: Trade) -> str:
        return f"{t.symbol} {self.names.get(t.symbol) or ''}".strip()

    def _set(self, t: Trade, state: str, now: datetime) -> None:
        t.state = state
        self._since[t.id] = now
        self._next[t.id] = now
        self.store.save_trade(t)

    # --- 入力 -----------------------------------------------------------------
    def on_price(self, symbol: str, price: float, ts: datetime) -> None:
        self.prices[symbol] = price
        if self.day != ts.date():
            self.day = ts.date()
            self.risk.new_day()
        for t in [t for t in self.open if t.symbol == symbol and t.state in ("open", "replacing")]:
            if t.trail(price):
                self.store.save_trade(t)
                log.info("損切りライン引き上げ %s → %.1f", self._label(t), t.stop)
        for strategy_id, sig_time, low in self.pending.pop(symbol, []):
            self._enter(strategy_id, symbol, sig_time, low, price, ts)

    def on_clock(self, now: datetime) -> None:
        now = now.astimezone(JST)
        late = now.time() >= self.exit_after
        if late:
            self.pending.clear()
        for t in list(self.open):
            stale = t.entry_time.astimezone(JST).date() < now.date()
            if t.state in ("open", "replacing") and (late or stale or self.close_all):
                self._begin_exit(t, now, "手動決済" if self.close_all and not late else "時間決済")
        self.close_all = False
        for t in list(self.open):
            if now < self._next.get(t.id, now):
                continue
            self._next[t.id] = now + POLL
            try:
                self._step(t, now)
                self.warnings.pop(f"trade{t.id}", None)
            except Exception as e:
                self.warnings[f"trade{t.id}"] = f"{self._label(t)}: 注文の処理でエラー（{e}）"
                log.warning("注文処理エラー %s: %s", self._label(t), e)
                self._next[t.id] = now + RETRY
        self._reconcile(now)
        self.snapshot = self.build_snapshot(now)

    # --- 新規の買い -------------------------------------------------------------
    def _enter(self, strategy_id, symbol, sig_time, low, price, ts) -> None:
        stop = self.settings.initial_stop(price, low)
        if any(t.symbol == symbol for t in self.open):
            qty, why = 0, "同じ銘柄を保有中"
        else:
            qty, why = self._check(strategy_id, symbol, price, stop, ts)
        if qty > 0:
            try:
                order_id = self.broker.buy_market(symbol, qty)
            except Exception as e:
                # 受け付けられたかどうか分からない。二重発注を避けるため再送せず、新規を止める。
                qty, why = 0, "発注エラー"
                self.risk.halted = True
                self.warnings["entry"] = (f"{symbol} の買い注文でエラーが出たため、新規の買いを停止しました。"
                                          f"kabuステーションで注文と建玉を確認してください。（{e}）")
                self._tell("買い注文エラー・新規停止", f"{symbol}: {e}", symbol)
        self.store.add_signal(self.mode, strategy_id, symbol, sig_time, price, low, why)
        if qty <= 0:
            log.info("見送り %s %s: %s", strategy_id, symbol, why)
            return
        st = self.settings
        t = Trade(self.mode, strategy_id, symbol, sig_time, ts, price, qty, stop, stop, high=price,
                  trail_mode=st.trail_mode, trail_value=st.trail_value, pattern_low=low,
                  state="entering", entry_order=order_id)
        self.store.save_trade(t)
        self.open.append(t)
        self._since[t.id] = ts.astimezone(JST)
        log.info("買い注文 %s %d株 成行 注文番号%s", self._label(t), qty, order_id)

    # --- 取引ごとの進行 ---------------------------------------------------------
    def _step(self, t: Trade, now: datetime) -> None:
        if t.state == "entering":
            self._step_entering(t, now)
        elif t.state == "open":
            self._step_open(t, now)
        elif t.state == "replacing":
            self._step_replacing(t, now)
        elif t.state == "exiting":
            self._step_exiting(t, now)

    def _cancel_once(self, t: Trade, order_id: str) -> None:
        if (t.id, order_id) in self._cancel_sent:
            return
        self._cancel_sent.add((t.id, order_id))
        try:
            self.broker.cancel(order_id)
        except Exception as e:   # すでに約定・取消済みなら取消はエラーになる。照会で確かめる。
            log.info("取消は受け付けられませんでした %s: %s", order_id, e)

    def _step_entering(self, t: Trade, now: datetime) -> None:
        st = self.broker.order(t.entry_order)
        if not st.done:
            if now - self._since.get(t.id, now) > ENTRY_TIMEOUT:
                self._cancel_once(t, t.entry_order)
            return
        if st.cum_qty <= 0:
            self.open.remove(t)
            self.store.delete_trade(t.id)
            self._tell("買い注文が成立しませんでした", self._label(t), t.symbol)
            return
        t.qty = st.cum_qty
        t.holds = ",".join(f"{i}:{q}" for i, q in st.fills)
        t.entry_price = st.avg_price or t.entry_price
        t.entry_time = now
        t.high = t.entry_price
        t.initial_stop = t.stop = self.settings.initial_stop(t.entry_price, t.pattern_low or t.stop)
        try:
            t.tick_group = self.broker.tick_group(t.symbol)
        except Exception as e:
            log.info("呼値グループを取得できませんでした %s: %s", t.symbol, e)
        self._tell("買い約定", f"{self._label(t)} {t.qty}株 @{t.entry_price:,.1f} 損切り {t.stop:,.1f}",
                   t.symbol)
        if t.entry_price <= t.stop:
            self._set(t, "open", now)
            self._begin_exit(t, now, "初期損切り")
            return
        self._place_stop(t, now)
        self._set(t, "open", now)

    def _place_stop(self, t: Trade, now: datetime) -> None:
        """証券会社側に逆指値を置く。置けなければ、このプログラムの監視に切り替える。"""
        t.stop_order, t.server_stop = "", None
        if self.stop_method != "server":
            return
        trig = trigger_price(t.stop, t.tick_group)
        try:
            t.stop_order = self.broker.place_stop(t.symbol, t.qty - t.exit_qty, trig,
                                                  self._holds(t))
            t.server_stop = trig
            self._last_replace[t.id] = now
            log.info("逆指値 %s トリガ%.1f 注文番号%s", self._label(t), trig, t.stop_order)
        except Exception as e:
            self.warnings[f"stop{t.id}"] = (f"{self._label(t)}: 逆指値を出せませんでした。"
                                            f"このプログラムが価格を監視して決済します。（{e}）")
            self._tell("逆指値を出せませんでした", f"{self._label(t)}: プログラムの監視に切り替えます",
                       t.symbol)

    def _holds(self, t: Trade):
        """返済する建玉の指定。一部だけ決済済みのときは、どの建玉が残っているか分からないので指定しない。"""
        return t.hold_list() if t.exit_qty == 0 else None

    def _take_fills(self, t: Trade, st, price_hint: float | None) -> None:
        """売り注文の約定分を取引に反映する。"""
        if st.cum_qty > 0:
            t.exit_qty += st.cum_qty
            t.exit_value += st.cum_qty * (st.avg_price or price_hint or t.stop)

    def _finish_if_flat(self, t: Trade, now: datetime, reason: str) -> bool:
        if t.exit_qty < t.qty:
            return False
        t.close(now, t.exit_value / t.exit_qty, reason)
        t.state = ""
        self.store.save_trade(t)
        self.open.remove(t)
        self.risk.on_close(t)
        self.warnings.pop(f"stop{t.id}", None)
        self._tell("決済", f"{self._label(t)} {reason} @{t.exit_price:,.1f} 損益 {t.pnl():+,.0f}円",
                   t.symbol)
        return True

    def _stop_reason(self, t: Trade) -> str:
        return "初期損切り" if t.stop <= t.initial_stop + 1e-9 else "トレーリング"

    def _step_open(self, t: Trade, now: datetime) -> None:
        price = self.prices.get(t.symbol)
        if not t.stop_order:   # このプログラムが監視
            if price is not None and price < t.stop:
                self._begin_exit(t, now, self._stop_reason(t))
            return
        st = self.broker.order(t.stop_order)
        if st.done:
            self._take_fills(t, st, t.server_stop)
            t.stop_order, t.server_stop = "", None
            self.store.save_trade(t)
            if self._finish_if_flat(t, now, self._stop_reason(t)):
                return
            if st.cum_qty == 0:
                self.warnings[f"stop{t.id}"] = (f"{self._label(t)}: 逆指値が無効になりました。"
                                                "このプログラムが価格を監視して決済します。")
                self._tell("逆指値が無効になりました", self._label(t), t.symbol)
            return
        want = trigger_price(t.stop, t.tick_group)
        if price is not None and price < t.stop and want <= (t.server_stop or 0) + 1e-9:
            # 逆指値が効くはずの価格。少し待っても約定しなければ、取り消して成行で決済する。
            first = self._breach.setdefault(t.id, now)
            if now - first > BREACH_WAIT:
                self._begin_exit(t, now, self._stop_reason(t))
            return
        self._breach.pop(t.id, None)
        if want > (t.server_stop or 0) + 1e-9 and \
                now - self._last_replace.get(t.id, now - REPLACE_GAP) >= REPLACE_GAP:
            self._cancel_once(t, t.stop_order)
            self._set(t, "replacing", now)

    def _step_replacing(self, t: Trade, now: datetime) -> None:
        st = self.broker.order(t.stop_order)
        if not st.done:
            return
        self._take_fills(t, st, t.server_stop)
        t.stop_order, t.server_stop = "", None
        if self._finish_if_flat(t, now, self._stop_reason(t)):
            return
        price = self.prices.get(t.symbol)
        if price is not None and price < t.stop:
            self._set(t, "open", now)
            self._begin_exit(t, now, self._stop_reason(t))
            return
        self._place_stop(t, now)
        self._set(t, "open", now)

    def _begin_exit(self, t: Trade, now: datetime, reason: str) -> None:
        self._reason[t.id] = reason
        self._set(t, "exiting", now)
        log.info("決済開始 %s（%s）", self._label(t), reason)

    def _step_exiting(self, t: Trade, now: datetime) -> None:
        reason = self._reason.get(t.id, "決済")
        if t.stop_order:   # まず逆指値を取り消す
            self._cancel_once(t, t.stop_order)
            st = self.broker.order(t.stop_order)
            if not st.done:
                return
            self._take_fills(t, st, t.server_stop)
            t.stop_order, t.server_stop = "", None
            self.store.save_trade(t)
            if self._finish_if_flat(t, now, reason):
                return
        if t.exit_order:
            st = self.broker.order(t.exit_order)
            if not st.done:
                return
            self._take_fills(t, st, self.prices.get(t.symbol))
            t.exit_order = ""
            self.store.save_trade(t)
            if self._finish_if_flat(t, now, reason):
                return
            self.warnings[f"exit{t.id}"] = f"{self._label(t)}: 決済注文が成立しませんでした。再度発注します。"
            self._next[t.id] = now + RETRY
            return
        t.exit_order = self.broker.close_market(t.symbol, t.qty - t.exit_qty, self._holds(t))
        self.store.save_trade(t)
        self.warnings.pop(f"exit{t.id}", None)
        log.info("返済注文 %s %d株 成行 注文番号%s", self._label(t), t.qty - t.exit_qty, t.exit_order)

    # --- 証券会社の建玉との照合 -------------------------------------------------
    def _reconcile(self, now: datetime) -> None:
        if self._next_reconcile and now < self._next_reconcile:
            return
        self._next_reconcile = now + RECONCILE
        if any(t.state in ("entering", "exiting", "replacing") for t in self.open):
            return   # 注文の途中は一時的にずれるので見ない
        try:
            actual = self.broker.positions()
        except Exception as e:
            log.info("建玉を照会できませんでした: %s", e)
            return
        expected: dict[str, int] = {}
        for t in self.open:
            expected[t.symbol] = expected.get(t.symbol, 0) + t.qty - t.exit_qty
        # このプログラムが持っている銘柄だけを比べる（手動で持っている他の銘柄は対象外）
        diff = [f"{s}: 記録{expected[s]}株 / 実際{actual.get(s, 0)}株"
                for s in sorted(expected) if expected[s] != actual.get(s, 0)]
        if not diff:
            self._mismatch = 0
            self.warnings.pop("reconcile", None)
            return
        self._mismatch += 1
        self.warnings["reconcile"] = ("このプログラムの記録と実際の信用建玉が合いません（"
                                      + "、".join(diff) + "）。kabuステーションで確認してください。")
        if self._mismatch == 2:   # 2回続けて合わなければ新規を止める
            self.risk.halted = True
            self._tell("建玉の不一致・新規停止", "、".join(diff))
