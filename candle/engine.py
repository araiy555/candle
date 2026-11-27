"""リアルタイム仮想売買（第4段階）。実際の注文は一切出さない。

足の確定ごとに全戦略を独立に判定し、サインが出たら次の約定価格で仮想的に買う。
損切り・トレーリング・時間決済は配信価格で判定する。
"""
from __future__ import annotations

import logging
from dataclasses import replace
from collections import defaultdict, deque
from datetime import datetime, time

from .bars import JST, SESSIONS, Bar, in_continuous
from types import SimpleNamespace

from . import fvg, mtf, opening, sweep
from .flow import FlowDetector, FlowParams
from .market import MarketMonitor
from .patterns import (BOS_STRATEGY, EXT_STRATEGY, FLOW_STRATEGY, FVG_STRATEGIES, OPEN_STRATEGY, STRATEGIES, STRATEGY_BY_ID, SWEEP_STRATEGY,
                       WALL_STRATEGIES, Params, contiguous_tail, decline)
from .wall import WallTracker
from .risk import RiskManager
from .settings import TradeSettings
from .squeeze_log import SqueezeLog
from .store import Store
from .trade import Trade, summarize

log = logging.getLogger(__name__)
# 画面に表示する銘柄情報（すべて配信で受信した値。判定には使わない）
QUOTE_FIELDS = ("CurrentPrice", "PreviousClose", "ChangePreviousClose", "ChangePreviousClosePer",
                "VWAP", "OpeningPrice", "HighPrice", "LowPrice", "TradingVolume", "TradingValue")


class PaperEngine:
    mode = "paper"
    mode_label = "テスト（仮想売買）"

    def __init__(self, store: Store, risk: RiskManager, params: Params | None = None,
                 slippage: float = 0.0005, exit_after: time = time(15, 20),
                 enabled: set[str] | None = None, trade_enabled: bool = True,
                 notify=None, notify_ids: set[str] | None = None,
                 settings: TradeSettings | None = None, flow_params: FlowParams | None = None,
                 squeeze_log=None, tape_fetcher=None, market: MarketMonitor | None = None):
        self.store, self.risk = store, risk
        self.settings = settings or TradeSettings(
            per_trade_cap=risk.limits.per_symbol_cap, total_cap=risk.limits.total_cap,
            daily_max_loss=risk.limits.daily_max_loss)
        self.buying_power: float | None = None     # 信用新規可能額（APIから取得した値。無ければ None）
        self.buying_power_note: str = "未取得"
        self.close_all = False        # 管理画面の「全決済」
        self.close_ids: set[int] = set()   # 管理画面で個別に「決済」を押した取引
        self.last_tick: datetime | None = None   # 最後に株価を受信した時刻（動作確認用）
        self.last_bar: datetime | None = None    # 最後に足が確定した時刻
        self.warnings: dict[str, str] = {}
        self.params = params or Params()
        self.slippage, self.exit_after = slippage, exit_after
        self.strategies = [s for s in STRATEGIES if enabled is None or s.id in enabled]
        ids = {s.id for s in self.strategies}
        wanted = ids | (notify_ids or set())
        # 大口の売り板の観察（約定の向きと出来高の差分が要るので、スクイーズの検出器と一緒に動かす）
        self.walls = WallTracker() if wanted & set(WALL_STRATEGIES.values()) else None
        self.sweep_on = bool(wanted & {SWEEP_STRATEGY, BOS_STRATEGY, EXT_STRATEGY})
        self.sweep_params = sweep.SweepParams()
        self.sweep_seen: set[tuple[str, str]] = set()      # (銘柄, 押し安値の時刻)。同じプールは1回だけ
        self.last_msg: dict[str, dict] = {}                # 銘柄ごとの最新の配信（記録に板などを残すため）
        # 寄り付き後の押し目買い（厚い買い板を背に、数ティック上を狙う）。通知と記録のみ
        self.opening = opening.OpenTracker() if OPEN_STRATEGY in wanted else None
        self.prev_days: dict[str, dict] = {}
        self.hourly_past: dict[str, list[Bar]] = {}
        self.last_day: dict[str, list[Bar]] = {}     # 当日の足が無いときに表示する、直近の営業日の足
        self._hourly_ext: dict[str, tuple] = {}
        self._frames: dict[str, tuple] = {}
        self.open_active: list[dict] = []                  # 目標・損切りのどちらに先に届くかを見ている検知
        want_flow = (FLOW_STRATEGY in wanted or self.walls is not None or self.sweep_on
                     or self.opening is not None or bool(wanted & set(FVG_STRATEGIES.values())))
        self.flow_notify = FLOW_STRATEGY in wanted   # スクイーズそのものを通知・記録するか
        self.flow = FlowDetector(flow_params) if want_flow else None
        self.flow_trades = FLOW_STRATEGY in ids      # スクイーズで売買もするか（通知だけなら False）
        # 検知の記録と、その後の値動きの追跡（モードを切り替えても同じものを使い続ける）
        self.squeeze_log = squeeze_log if squeeze_log is not None else (
            SqueezeLog(store, tape_fetcher) if self.flow is not None else None)
        self.notes: dict[tuple[str, str, str], str] = {}
        self.drops: dict[tuple[str, str, str], float | None] = {}
        # FVGの通知（対象の戦略IDが、有効な戦略か通知対象に入っているものだけ）
        self.fvg_kinds = {k for k, sid in FVG_STRATEGIES.items()
                          if sid in ids or (notify_ids is not None and sid in notify_ids)}
        self.fvg_params = fvg.FvgParams()
        self.fvg_seen: set[tuple[str, str, str]] = set()   # (銘柄, FVGができた時刻, 種類)。同じFVGは1回だけ
        self.quotes: dict[str, dict] = {}    # 銘柄ごとの最新の受信値（VWAP・前日比・高安など。表示用）
        self.market = market if market is not None else MarketMonitor()   # 地合いの監視
        self.history: dict[str, deque[Bar]] = defaultdict(lambda: deque(maxlen=400))  # 1分足でも当日分が収まる
        self.prices: dict[str, float] = {}
        self.watchlist: list[str] = []
        self.names: dict[str, str] = {}
        self.trade_enabled = trade_enabled   # False: 通知のみ（仮想売買もしない）
        self.notify, self.notify_ids = notify, notify_ids
        self.auto_chart = False   # サイン発生時にkabuステーションのチャートを自動で切り替える
        self.on_auto_chart = None  # 切り替えを行う関数（symbol を受け取る）
        self.pending: dict[str, list[tuple[str, datetime, float]]] = defaultdict(list)
        self.open: list[Trade] = store.load_trades(self.mode, open_only=True)
        self.day = None
        # 同一戦略・同一銘柄・同一発生時刻のサインは1回だけ扱う（再起動後も重複させない）
        self.seen: set[tuple[str, str, str]] = {
            (r["strategy_id"], r["symbol"], r["time"]) for r in store.recent_signals(self.mode, 2000)}
        self.snapshot: dict = {}
        today = datetime.now(JST).date()
        risk.realized_today = sum(t.pnl() for t in store.load_trades(self.mode)
                                  if t.exit_time and t.exit_time.astimezone(JST).date() == today)
        self.apply_sizing()

    def load_history(self, symbols: list[str], minutes: int, today=None) -> None:
        """保存済みの足を読み込む。再起動してもチャートと判定を途切れさせないため。

        当日の足のほか、過去の日の足から1時間足と前日の高値・安値を作る。当日の足がまだ無いとき
        （休日・寄り付き前）は、直近の営業日の足を表示用に持っておく（サインの判定には使わない）。
        """
        today = today or datetime.now(JST).date()
        for sym in symbols:
            if self.history[sym]:
                continue
            bars = [b for b in self.store.load_bars(sym, minutes, limit=400)
                    if b.start.date() == today]
            self.history[sym].extend(bars)
            if bars:
                self.prices.setdefault(sym, bars[-1].close)
            past = self.store.past_bars(sym, minutes, today.isoformat())
            ref = today.isoformat()
            if past and not bars:
                last = past[-1].start.date()
                self.last_day[sym] = [b for b in past if b.start.date() == last]
                past = [b for b in past if b.start.date() != last]
                ref = last.isoformat()
            if past:
                self.hourly_past[sym] = mtf.aggregate(past, 60)   # 過去の日の1時間足（数日分の流れを見る）
            prev = self.store.prev_day(sym, minutes, ref)
            if prev:
                self.prev_days[sym] = prev     # 前日の高値・安値（上位足の流動性として表示する）

    def _roll_day(self, symbol: str, old: list[Bar]) -> None:
        """日が変わった。前の日の足を1時間足にまとめて過去分に足し、前日の高値・安値を入れ替える。"""
        if not old:
            return
        self.hourly_past[symbol] = self.hourly_past.get(symbol, []) + mtf.aggregate(old, 60)
        self.prev_days[symbol] = dict(day=old[0].start.date().isoformat(), high=max(b.high for b in old),
                                      low=min(b.low for b in old), close=old[-1].close)

    def day_bars(self, symbol: str) -> list[Bar]:
        """表示・向きの判定に使う足。当日の足、まだ無ければ直近の営業日の足。"""
        for _ in range(3):
            try:
                return list(self.history.get(symbol, ())) or list(self.last_day.get(symbol, ()))
            except RuntimeError:   # 追加と読み出しが重なった場合はやり直す
                continue
        return []

    # --- 入力 ---------------------------------------------------------------
    def on_bar(self, bar: Bar) -> None:
        if self.market.is_market_symbol(bar.symbol):
            return   # 先物・TOPIXは地合いの判定にだけ使う（売買や通知の対象にしない）
        h = self.history[bar.symbol]
        if h and h[-1].start.date() != bar.start.date():
            self._roll_day(bar.symbol, list(h))
            h.clear()
        if bar.symbol in self.last_day:
            self._roll_day(bar.symbol, self.last_day.pop(bar.symbol))   # 当日の足が来たので、表示用の前営業日分を過去へ
        h.append(bar)
        self.last_bar = bar.end
        if not bar.complete or bar.start.time() >= self.exit_after or self.in_quiet_open(bar.end):
            return
        if bar.close < self.settings.min_price:
            return   # 株価の低い銘柄は対象外（1ティックが相対的に大きく、足の形が崩れやすい）
        if self.is_ranging(bar.symbol):
            return   # 値動きが小さい（レンジ）間は、通知も売買もしない
        window = contiguous_tail(list(h)[-80:])
        self._check_fvg(bar, h)
        self._check_sweep(bar, h)
        params = replace(self.params, min_drop=self.settings.min_drop_pct / 100)
        drop = decline(window, 2, params)
        drop_pct = None if drop is None else round(drop * 100, 3)
        for s in self.strategies:
            low = s.detect(window, params)
            if low is None:
                continue
            key = (s.id, bar.symbol, bar.end.isoformat())
            if key in self.seen:
                continue
            self.seen.add(key)
            log.info("サイン %s %s %s 安値%s", s.name, bar.symbol, bar.end.strftime("%H:%M"), low)
            if self.notify and self._wants(s.id):
                label = f"{bar.symbol} {self.names.get(bar.symbol) or ''}".strip()
                self._notify(f"{s.name}  {label}",
                            f"{bar.end:%H:%M} 終値 {bar.close:,.1f} / パターン安値 {low:,.1f}",
                            bar.symbol)
            if self.auto_chart and self.on_auto_chart:
                self.on_auto_chart(bar.symbol)
            if self.trade_enabled:
                self.drops[(s.id, bar.symbol, bar.end.isoformat())] = drop_pct
                self.pending[bar.symbol].append((s.id, bar.end, low))
            else:
                self._signal(self.mode, s.id, bar.symbol, bar.end, bar.close, low, "通知のみ",
                                      vwap=self.vwap(bar.symbol), drop_pct=drop_pct)

    def _wants(self, strategy_id: str) -> bool:
        """このサインを通知するか（起動時の対象に入っていて、管理画面でオンになっているもの）。"""
        return ((self.notify_ids is None or strategy_id in self.notify_ids)
                and self.settings.notifies(strategy_id))

    def in_quiet_open(self, ts: datetime) -> bool:
        """寄り付き直後の、サインを出さない時間帯か。値動きが荒く、サインが出すぎるため。"""
        ts = ts.astimezone(JST)
        minutes = (ts.hour - 9) * 60 + ts.minute + ts.second / 60
        return 0 <= minutes < self.settings.quiet_open_min

    def _signal(self, mode, strategy_id, symbol, *args, **kw) -> None:
        """サインを保存する。そのときの平均値幅率も一緒に残す。"""
        self.store.add_signal(mode, strategy_id, symbol, *args, range_pct=self.range_pct(symbol), **kw)

    def range_pct(self, symbol: str, n: int = 30) -> float | None:
        """足1本あたりの平均値幅率（%）。直近 n 本の（高値−安値）÷終値の平均。5本未満なら None。"""
        bars = [b for b in list(self.history.get(symbol, ()))[-n:] if b.complete and b.close > 0]
        if len(bars) < 5:
            return None
        return sum((b.high - b.low) / b.close for b in bars) / len(bars) * 100

    def frames(self, symbol: str) -> list[dict]:
        """足ごとの向き（1分足・5分足・15分足）。足が確定するまで同じ結果を使い回す。"""
        h = self.day_bars(symbol)   # 当日の足すべて（約定の無かった分や昼休みで途切れていても使う）
        if not h:
            return []
        key = (h[-1].start, len(h))
        hit = self._frames.get(symbol)
        if hit is None or hit[0] != key:
            bars = h
            hit = (key, mtf.summary(bars, bars[0].minutes, self.hourly_past.get(symbol, ())))
            self._frames[symbol] = hit
        return hit[1]

    def direction_text(self, symbol: str) -> str:
        """通知に添える方向性。例: '方向：5分足 上昇／15分足 レンジ'。上位足が無ければ空。"""
        fr = [f for f in self.frames(symbol)[1:] if f["trend"]]      # 判定できない足は書かない
        return "方向：" + "／".join(f"{f['name']} {f['label']}" for f in fr) if fr else ""

    def _notify(self, title: str, body: str, symbol: str) -> None:
        """通知を送る。上位足の方向性を添える。"""
        d = self.direction_text(symbol)
        self.notify(title, f"{body}　{d}" if d else body, symbol)

    def chart_liquidity(self, symbol: str, bars: list[Bar], minutes: int, day: list[Bar]) -> list[dict]:
        """チャートに出す流動性。外部流動性は、出どころ（当日の大きな波・前日の高値安値・数日間の高値安値）に
        かかわらず1種類にまとめる。同じ価格のものは1本にし、当日すでに抜かれたものは出さない。
        """
        out = sweep.liquidity_levels(bars, self.swing_params(minutes))
        if minutes >= 60 or not bars:
            return out                    # 1時間足は過去の日の足そのものが表示されているので、足す必要がない
        hi, lo = max(b.high for b in day), min(b.low for b in day)
        extra = [dict(side=x["side"], level=x["level"]) for x in self.hourly_external(symbol)]
        prev = self.prev_days.get(symbol)
        if prev:
            extra += [dict(side="high", level=prev["high"]), dict(side="low", level=prev["low"])]
        first = bars[0].start.isoformat()
        for x in extra:
            lv = x["level"]
            if lv is None or (x["side"] == "high" and hi > lv) or (x["side"] == "low" and lo < lv):
                continue                  # 当日すでに抜かれた（流動性は使われた）
            same = next((o for o in out if o["side"] == x["side"] and abs(o["level"] - lv) <= lv * 0.0002), None)
            if same:
                same["kind"] = "external"
            else:
                out.append(dict(side=x["side"], t=first, level=lv, kind="external"))
        return out

    def swing_params(self, minutes: int) -> "sweep.SweepParams":
        """外部流動性とみなす「大きな波」の幅を、足の長さに合わせる（左右それぞれ約45分。最低10本）。"""
        return replace(self.sweep_params, major=max(10, 45 // max(1, minutes)))

    def hourly_external(self, symbol: str) -> list[dict]:
        """過去の日をつなげた1時間足から見た外部流動性（数日間の大きな高値・安値）。上位足の流動性として使う。"""
        past = self.hourly_past.get(symbol)
        if not past:
            return []
        hit = self._hourly_ext.get(symbol)
        if hit is None or hit[0] != len(past):
            lv = sweep.liquidity_levels(past, self.sweep_params, major=6, max_internal=0)
            hit = (len(past), [dict(side=x["side"], level=x["level"], t=x["t"]) for x in lv])
            self._hourly_ext[symbol] = hit
        today = list(self.history.get(symbol, ()))
        hi = max((b.high for b in today[:-2]), default=None)
        lo = min((b.low for b in today[:-2]), default=None)
        # 今日すでに抜かれたものは、流動性を使い切ったものとして除く
        return [x for x in hit[1] if not ((x["side"] == "high" and hi is not None and hi > x["level"])
                                          or (x["side"] == "low" and lo is not None and lo < x["level"]))]

    def htf(self, symbol: str) -> str:
        """上位足（5分足・15分足）の向き。取引の記録用。"""
        bars = list(self.history.get(symbol, ()))
        return mtf.code(bars, bars[0].minutes, self.hourly_past.get(symbol, ())) if bars else ""

    def is_ranging(self, symbol: str) -> bool:
        """値動きが小さい（レンジ）か。足の平均値幅率が設定の下限より小さいとき。足が足りなければ False。"""
        r = self.range_pct(symbol)
        return r is not None and r < self.settings.min_range_pct

    def day_range_pct(self, symbol: str) -> float | None:
        """当日の値幅率（%）。（当日高値−当日安値）÷始値。受信していなければ None。"""
        q = self.quotes.get(symbol) or {}
        hi, lo, op = q.get("HighPrice"), q.get("LowPrice"), q.get("OpeningPrice")
        return (hi - lo) / op * 100 if hi and lo and op else None

    def vwap(self, symbol: str) -> float | None:
        v = (self.quotes.get(symbol) or {}).get("VWAP")
        return float(v) if v else None

    def market_state(self, now: datetime) -> dict:
        """地合いの状態。設定の基準を反映して判定する。"""
        st, p = self.settings, self.market.p
        p.fut_drop, p.topix_drop = st.mkt_fut_drop, st.mkt_topix_drop
        p.breadth = st.mkt_breadth if st.mkt_breadth > 0 else 101.0   # 0 は「止めない」
        return self.market.state(now.astimezone(JST), self.watchlist)

    def _check_walls(self, symbol: str, msg: dict, now: datetime) -> None:
        """大口の売り板の出来事（接近・突破・取消・跳ね返し）を記録し、対象のものを通知する。"""
        if self.walls is None:
            return
        for ev in self.walls.update(symbol, msg, now.astimezone(JST), self.flow.last_trade[0]):
            if self.in_quiet_open(now) or now.astimezone(JST).time() >= self.exit_after:
                continue
            if ev.kind != "approach" and self.squeeze_log is not None:
                # 突破だけでなく、取消で消えた・跳ね返された場合も、比較のためにすべて残す
                self.squeeze_log.on_signal(ev, msg, self.names.get(symbol), kind="wall")
            sid = WALL_STRATEGIES.get(ev.kind)
            if sid is None or not self._wants(sid):
                continue
            key = (sid, symbol, ev.time.isoformat())
            if key in self.seen:
                continue
            self.seen.add(key)
            name = STRATEGY_BY_ID[sid].name
            log.info("サイン %s %s %s", name, symbol, ev.note)
            if self.notify:
                label = f"{symbol} {self.names.get(symbol) or ''}".strip()
                self._notify(f"{name}  {label}", f"{ev.time:%H:%M:%S} {ev.price:,.1f}円　{ev.note}", symbol)
            if self.auto_chart and self.on_auto_chart:
                self.on_auto_chart(symbol)
            self._signal(self.mode, sid, symbol, ev.time, ev.price, ev.wall.price, "通知のみ",
                                  ev.note, self.vwap(symbol))

    def _check_opening(self, symbol: str, msg: dict, now: datetime) -> None:
        """寄り付き後の押し目買いを判定して通知・記録し、目標と損切りのどちらに先に届いたかを残す。

        寄り付き直後を狙うものなので、「寄り付き後に待つ時間」の対象にはしない。
        """
        if self.opening is None:
            return
        now = now.astimezone(JST)
        price = msg.get("CurrentPrice")
        for a in list(self.open_active):
            if a["symbol"] != symbol or price is None:
                continue
            secs = (now - a["time"]).total_seconds()
            result = ("target" if price >= a["metrics"]["target"] else
                      "stop" if price <= a["metrics"]["stop"] else
                      "timeout" if secs >= self.opening.p.timeout else None)
            if result:
                self.open_active.remove(a)
                a["metrics"].update(outcome=result, outcome_label=opening.RESULTS[result],
                                    result_seconds=round(secs, 1))
                if self.squeeze_log is not None:
                    self.squeeze_log.set_metrics(a["id"], a["metrics"])
        if not self._wants(OPEN_STRATEGY):
            return
        sig = self.opening.update(symbol, msg, now)
        key = (OPEN_STRATEGY, symbol, now.isoformat())
        if sig is None or key in self.seen:
            return
        self.seen.add(key)
        name = STRATEGY_BY_ID[OPEN_STRATEGY].name
        log.info("サイン %s %s %s", name, symbol, sig.note)
        if self.squeeze_log is not None:
            event_id = self.squeeze_log.on_signal(sig, msg, self.names.get(symbol), kind="open")
            self.open_active.append(dict(id=event_id, symbol=symbol, time=now, metrics=sig.metrics))
        if self.notify:
            label = f"{symbol} {self.names.get(symbol) or ''}".strip()
            self._notify(f"{name}  {label}", f"{now:%H:%M:%S} {sig.note}", symbol)
        if self.auto_chart and self.on_auto_chart:
            self.on_auto_chart(symbol)
        self._signal(self.mode, OPEN_STRATEGY, symbol, now, sig.price, sig.low, "通知のみ",
                     sig.note, self.vwap(symbol))

    def _check_sweep(self, bar: Bar, history) -> None:
        """足の確定ごとに、流動性スイープ（押し安値を割って反転）を判定して通知・記録する。

        下の流動性にタッチ（→上へ）、上の流動性にタッチ（→下へ）のどちらも「流動性タッチ」として知らせ、
        どの流動性にタッチしたか、次のオーダーブロック（反対側の外部流動性）、途中の内部流動性を文面に入れる。
        """
        if not self.sweep_on:
            return
        # 流動性は当日の足すべてから探す（約定の無かった分や昼休みで途切れていても、高値・安値は有効）
        sig = sweep.detect(list(history)[-330:], self.swing_params(bar.minutes))
        if sig is None:
            return
        m, prev = sig.metrics, self.prev_days.get(bar.symbol)
        up = m.get("side") == "high"                  # 上の流動性にタッチ（下へ向かう合図）
        if prev:
            # 前日の高値・安値は、上位足の外部流動性として扱う
            touched = prev.get("high") if up else prev.get("low")
            if touched and ((bar.close <= touched < sig.low) if up else (sig.low < touched <= bar.close)):
                label = "前日高値" if up else "前日安値"
                m.update(liquidity="external", ext_kind=label, bos_setup=False, prev_level=touched)
                sig.note = f"{label} {touched:,.1f} にもタッチ。" + sig.note
            goal = prev.get("low") if up else prev.get("high")
            nearer = goal and ((goal < bar.close) if up else (goal > bar.close)) and (
                m.get("target") is None or abs(goal - bar.close) < abs(m["target"] - bar.close))
            if nearer:                                # 前日の高値・安値のほうが近ければ、それを次の目標にする
                old_target = sweep.target_text(m)
                m.update(target=goal, target_pct=(goal / bar.close - 1) * 100, target_kind="前日安値" if up else "前日高値",
                         waypoints=[v for v in m.get("waypoints", []) if abs(v - bar.close) < abs(goal - bar.close)])
                sig.note = sig.note.replace(old_target, sweep.target_text(m) + f"（{m['target_kind']}）")
        # 1時間足（数日分）の外部流動性にもタッチしたか。タッチした側にもう流動性が残っていなければ、より強い場面
        hx = [x for x in self.hourly_external(bar.symbol) if x["side"] == ("high" if up else "low")]
        hit = [x["level"] for x in hx if ((bar.close <= x["level"] < sig.low) if up else (sig.low < x["level"] <= bar.close))]
        if hit:
            left = [x["level"] for x in hx if ((x["level"] > sig.low) if up else (x["level"] < sig.low))]
            lv = max(hit) if up else min(hit)
            m.update(liquidity="external", ext_kind="1時間足の外部流動性", bos_setup=False, htf_level=lv,
                     htf_cleared=not left)
            side_word = "上" if up else "下"
            other = "下" if up else "上"
            sig.note = (f"1時間足の外部流動性 {lv:,.1f} にもタッチ"
                        + (f"（{side_word}にはもう流動性が残っておらず、{other}にしか流動性がありません）" if not left else "")
                        + "。" + sig.note)
        sid = SWEEP_STRATEGY
        key = (bar.symbol, sig.level_time.isoformat() + (":high" if up else ""))
        if key in self.sweep_seen or any((x, bar.symbol, bar.end.isoformat()) in self.seen
                                         for x in (SWEEP_STRATEGY, BOS_STRATEGY, EXT_STRATEGY)):
            return
        self.sweep_seen.add(key)
        self.seen.add((sid, bar.symbol, bar.end.isoformat()))
        name = STRATEGY_BY_ID[sid].name
        log.info("サイン %s %s %s", name, bar.symbol, sig.note)
        if self.squeeze_log is not None:
            self.squeeze_log.on_signal(sig, self.last_msg.get(bar.symbol, {}), self.names.get(bar.symbol),
                                       kind="sweep")
        if not self._wants(sid):
            return                    # 記録だけ残す（通知をオフにしていても、後で比べられるように）
        if self.notify:
            label = f"{bar.symbol} {self.names.get(bar.symbol) or ''}".strip()
            self._notify(f"{name}  {label}", f"{bar.end:%H:%M} {sig.note}", bar.symbol)
        if self.auto_chart and self.on_auto_chart:
            self.on_auto_chart(bar.symbol)
        self._signal(self.mode, sid, bar.symbol, bar.end, bar.close, sig.low,
                     "通知のみ", sig.note, self.vwap(bar.symbol))

    def _check_fvg(self, bar: Bar, history) -> None:
        """足の確定ごとに、FVGの反発・割れを判定して通知する（売買はしない）。"""
        if not self.fvg_kinds:
            return
        sig = fvg.detect(contiguous_tail(list(history)[-200:]), self.fvg_params)
        if sig is None or sig.kind not in self.fvg_kinds:
            return
        key = (bar.symbol, sig.formed.isoformat(), sig.kind)
        if key in self.fvg_seen:
            return
        self.fvg_seen.add(key)
        sid = FVG_STRATEGIES[sig.kind]
        name = STRATEGY_BY_ID[sid].name
        log.info("サイン %s %s %s", name, bar.symbol, sig.note)
        if (sid, bar.symbol, bar.end.isoformat()) in self.seen:
            return   # 再起動などで同じ足をもう一度見た場合
        self.seen.add((sid, bar.symbol, bar.end.isoformat()))
        if self.squeeze_log is not None:
            # 反発も割れも検知履歴に残し、その後の値動きを追う
            vol_ok = f"出来高は直近平均の {sig.volume_ratio:.1f}倍。"
            conditions = [
                dict(key="fvg", label="割安ゾーンのFVG", ok=True,
                     text=f"{sig.formed:%H:%M} の足でできたFVG {sig.bottom:,.1f}〜{sig.top:,.1f}円。"
                          f"上昇の範囲は {sig.leg_low:,.1f}〜{sig.leg_high:,.1f}円。"),
                dict(key="close", label="FVGより上で確定" if sig.kind == "hold" else "FVGを終値で下抜け", ok=True,
                     text=f"終値 {bar.close:,.1f}円。押しの安値 {sig.pullback_low:,.1f}円。"),
                dict(key="volume", label="出来高の増加", ok=True, text=vol_ok),
            ]
            metrics = dict(outcome=sig.kind, outcome_label=name, bottom=sig.bottom, top=sig.top,
                           leg_low=sig.leg_low, leg_high=sig.leg_high, pullback_low=sig.pullback_low,
                           volume_ratio=sig.volume_ratio, poc=sig.poc, poc_source=sig.poc_source,
                           formed=sig.formed.isoformat())
            self.squeeze_log.on_signal(
                SimpleNamespace(symbol=bar.symbol, time=bar.end, price=bar.close, metrics=metrics,
                                conditions=conditions),
                self.last_msg.get(bar.symbol, {}), self.names.get(bar.symbol), kind="fvg")
        if self.notify and self._wants(sid):
            label = f"{bar.symbol} {self.names.get(bar.symbol) or ''}".strip()
            self._notify(f"{name}  {label}", f"{bar.end:%H:%M} 終値 {bar.close:,.1f}　{sig.note}", bar.symbol)
        if self.auto_chart and self.on_auto_chart:
            self.on_auto_chart(bar.symbol)
        self._signal(self.mode, sid, bar.symbol, bar.end, bar.close, sig.bottom, "通知のみ",
                              sig.note, self.vwap(bar.symbol))

    def capacity(self) -> float | None:
        """信用で建てられる総額。本番では、余力に、すでに建てている分を足し戻す。"""
        if self.buying_power is None:
            return None
        used = sum(t.entry_price * t.qty for t in self.open) if self.mode == "live" else 0.0
        return self.buying_power + used

    def apply_sizing(self) -> None:
        """設定（金額または余力に対する割合）から、買いの上限を決める。"""
        ok = self.settings.apply_to(self.risk.limits, self.capacity())
        if ok:
            self.warnings.pop("sizing", None)
        else:
            self.warnings["sizing"] = ("信用の買付余力を取得できていないため、割合ではなく金額の上限"
                                       f"（1回 {self.settings.per_trade_cap:,.0f}円）で動いています。")

    def set_feed(self, ok: bool) -> None:
        """API接続の状態が変わった。切断中は新規の買いを止め、追跡中の検知は欠損として扱う。"""
        self.risk.feed_ok = ok
        if not ok and self.squeeze_log is not None:
            self.squeeze_log.on_gap("株価の受信が途切れたため、以降は記録できていません", datetime.now(JST))

    def on_board(self, symbol: str, msg: dict, now: datetime) -> None:
        """配信1件ごとに呼ばれる。板と約定の流れからスクイーズを判定する。"""
        self.quotes[symbol] = {k: msg.get(k) for k in QUOTE_FIELDS}
        self.last_msg[symbol] = msg
        if self.flow is None or self.market.is_market_symbol(symbol):
            return
        sig = self.flow.update(symbol, msg, now)
        if self.squeeze_log is not None:
            self.squeeze_log.on_tick(symbol, msg, now, *self.flow.last_trade)
        cheap = (msg.get("CurrentPrice") or 0) < self.settings.min_price
        if cheap:
            return
        if not in_continuous(now):
            if self.walls is not None:
                self.walls.walls.pop(symbol, None)   # 昼休みなどをまたいで売り板を追わない
            return
        self._check_opening(symbol, msg, now)     # 寄り付き直後は足が少なく、レンジの判定はしない
        if self.is_ranging(symbol):
            return
        self._check_walls(symbol, msg, now)
        if (sig is None or not self.flow_notify or now.astimezone(JST).time() >= self.exit_after
                or self.in_quiet_open(now)):
            return
        name = STRATEGY_BY_ID[FLOW_STRATEGY].name
        key = (FLOW_STRATEGY, symbol, sig.time.isoformat())
        if key in self.seen:
            return
        self.seen.add(key)
        log.info("サイン %s %s %s", name, symbol, sig.note)
        if self.squeeze_log is not None:
            self.squeeze_log.on_signal(sig, msg, self.names.get(symbol))
        if self.notify and self._wants(FLOW_STRATEGY):
            label = f"{symbol} {self.names.get(symbol) or ''}".strip()
            self._notify(f"{name}  {label}", f"{sig.time:%H:%M:%S} {sig.price:,.1f}円　{sig.note}", symbol)
        if self.auto_chart and self.on_auto_chart:
            self.on_auto_chart(symbol)
        if self.trade_enabled and self.flow_trades:
            self.notes[key] = sig.note
            self.pending[symbol].append((FLOW_STRATEGY, sig.time, sig.low))
        else:
            self._signal(self.mode, FLOW_STRATEGY, symbol, sig.time, sig.price, sig.low,
                                  "通知のみ", sig.note, self.vwap(symbol))

    def on_price(self, symbol: str, price: float, ts: datetime) -> None:
        self.last_tick = datetime.now(JST)
        if self.market.on_price(symbol, price, ts.astimezone(JST)):
            return   # 先物・TOPIX
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
            reason = self.exit_reason_by_time(t, now, late or stale)
            if reason and price is None:
                self.warnings[f"price{t.id}"] = (f"{t.symbol}: 株価をまだ受信できていないため、"
                                                 "決済を保留しています。受信でき次第、決済します。")
            elif reason:
                self.warnings.pop(f"price{t.id}", None)
                self._close(t, now, price, reason)
        self.close_all = False
        self.close_ids &= {t.id for t in self.open}
        if self.squeeze_log is not None:
            self.squeeze_log.on_clock(now)
        self.snapshot = self.build_snapshot(now)

    # --- 売買 ---------------------------------------------------------------
    def _enter(self, strategy_id, symbol, sig_time, low, price, ts) -> None:
        entry = price * (1 + self.slippage)
        st = self.settings
        stop = st.initial_stop(entry, low)
        qty, why = self._check(strategy_id, symbol, entry, stop, ts)
        self._signal(self.mode, strategy_id, symbol, sig_time, price, low, why,
                              self.notes.pop((strategy_id, symbol, sig_time.isoformat()), ""),
                              self.vwap(symbol),
                              self.drops.pop((strategy_id, symbol, sig_time.isoformat()), None))
        if qty <= 0:
            log.info("見送り %s %s: %s", strategy_id, symbol, why)
            return
        t = Trade(self.mode, strategy_id, symbol, sig_time, ts, entry, qty, stop, stop, high=entry,
                  trail_mode=st.trail_mode, trail_value=st.trail_value, pattern_low=low,
                  range_pct=self.range_pct(symbol), htf=self.htf(symbol))
        self.store.save_trade(t)
        self.open.append(t)
        log.info("仮想買い %s %s %d株 @%.1f 損切り%.1f", strategy_id, symbol, qty, entry, stop)

    def exit_reason_by_time(self, t: Trade, now: datetime, late: bool) -> str | None:
        """時刻・保有時間・手動操作による決済の理由。該当しなければ None。"""
        if late:
            return "時間決済"
        if self.close_all or t.id in self.close_ids:
            return "手動決済"
        limit = self.settings.max_hold_min
        if limit > 0 and (now - t.entry_time).total_seconds() >= limit * 60:
            return "保有時間切れ"
        return None

    def _check(self, strategy_id, symbol, entry, stop, ts) -> tuple[int, str]:
        """新規買いの可否と数量。"""
        if ts.time() >= self.exit_after:
            return 0, "時間外"
        self.apply_sizing()
        if self.market_state(ts)["halt"]:
            return 0, "地合い悪化で新規停止中"
        vwap, px = self.vwap(symbol), self.prices.get(symbol, entry)
        if self.settings.vwap_rule == "above" and vwap and px < vwap:
            return 0, "VWAPより下"     # 下げている途中の一時的な止まりであることが多い（VWAPが分からなければ見送らない）
        unreal = sum(t.pnl(self.prices.get(t.symbol, t.entry_price)) for t in self.open)
        return self.risk.check_entry(strategy_id, symbol, entry, stop, self.open, unreal)

    def _close(self, t: Trade, ts: datetime, price: float, reason: str) -> None:
        t.close(ts, price * (1 - self.slippage), reason)
        self.store.save_trade(t)
        self.open.remove(t)
        self.risk.on_close(t)
        log.info("仮想決済 %s %s %s 損益%+.0f円", t.strategy_id, t.symbol, reason, t.pnl())

    # --- 管理画面用 ---------------------------------------------------------
    def state_label(self, t: Trade) -> str:
        return "保有中"

    def health(self, now: datetime) -> dict:
        """動いているかを画面で確かめるための情報。"""
        need = self.params.trend_bars + 2
        ready = sum(1 for s in self.watchlist
                    if len(contiguous_tail(list(self.history.get(s, ()))[-need:])) >= need)
        today = now.date().isoformat()
        return dict(
            last_tick=self.last_tick.isoformat() if self.last_tick else None,
            tick_age=(now - self.last_tick).total_seconds() if self.last_tick else None,
            last_bar=self.last_bar.isoformat() if self.last_bar else None,
            bars_today=sum(len(self.history.get(s, ())) for s in self.watchlist),
            ready=ready, watching=len(self.watchlist),
            signals_today=sum(1 for k in self.seen if k[2].startswith(today)),
            market_open=any(a <= now.time() <= b for a, b in SESSIONS))

    def update_settings(self, data: dict) -> None:
        """管理画面からの設定変更。以後の新規の買いに適用する（保有中の分は変えない）。"""
        self.settings = TradeSettings.from_dict({**vars(self.settings), **data})
        self.apply_sizing()

    def chart_data(self, symbol: str, tf: int | None = None) -> dict:
        """チャート表示用の当日の足。tf を指定すると、その分足にまとめて返す。管理画面のスレッドから呼ばれる。"""
        base = self.day_bars(symbol)
        minutes = base[0].minutes if base else None
        frames = self.frames(symbol) if base else []
        bars = base
        if base and tf and tf > minutes and tf % minutes == 0:
            bars, minutes = mtf.aggregate(base, tf), tf
            if tf == 60:
                bars = list(self.hourly_past.get(symbol, ())) + bars      # 1時間足は過去の日もつなげる
        tail = contiguous_tail(bars[-200:]) if bars else []
        return dict(symbol=symbol, name=self.names.get(symbol), minutes=minutes,
                    base_minutes=base[0].minutes if base else None, frames=frames,
                    prev_day=self.prev_days.get(symbol),
                    shown_day=base[0].start.date().isoformat() if base else None,
                    live=bool(self.history.get(symbol)),
                    quote=self.quotes.get(symbol),
                    range_pct=self.range_pct(symbol), day_range_pct=self.day_range_pct(symbol),
                    structure=fvg.structure(tail, self.fvg_params) if bars else None,
                    liquidity=self.chart_liquidity(symbol, bars[-330:], minutes, base) if bars else [],
                    pools=[dict(t=tail[i].start.isoformat(), level=lv)
                           for i, lv in sweep.pools(tail, self.sweep_params)] if bars else [],
                    bars=[dict(t=b.start.isoformat(), o=b.open, h=b.high, l=b.low, c=b.close,
                               v=b.volume, ok=b.complete, vwap=b.vwap) for b in bars])

    def build_snapshot(self, now: datetime) -> dict:
        trades = self.store.load_trades(self.mode)
        by_strategy = defaultdict(list)
        for t in trades:
            by_strategy[t.strategy_id].append(t)
        name = lambda sid: STRATEGY_BY_ID[sid].name if sid in STRATEGY_BY_ID else sid
        positions = []
        for t in self.open:
            px = self.prices.get(t.symbol)
            positions.append(dict(
                id=t.id, strategy=name(t.strategy_id), symbol=t.symbol, qty=t.qty,
                state=self.state_label(t), name=self.names.get(t.symbol),
                signal_time=t.signal_time.isoformat(), entry_time=t.entry_time.isoformat(),
                entry_price=t.entry_price, price=px,
                initial_stop=t.initial_stop, stop=t.stop, vwap=self.vwap(t.symbol),
                range_pct=self.range_pct(t.symbol),
                next_raise=t.next_raise(), trail_mode=t.trail_mode, trail_value=t.trail_value,
                pnl=None if px is None else t.pnl(px),
                r=None if px is None else t.r_multiple(px),
                hold_min=(now - t.entry_time).total_seconds() / 60))
        history = [dict(id=t.id, name=self.names.get(t.symbol), initial_stop=t.initial_stop,
                        strategy=name(t.strategy_id), symbol=t.symbol, qty=t.qty,
                        entry_time=t.entry_time.isoformat(), entry_price=t.entry_price,
                        exit_time=t.exit_time.isoformat(), exit_price=t.exit_price,
                        reason=t.exit_reason, pnl=t.pnl(), r=t.r_multiple(), range_pct=t.range_pct,
                        stop_pct=(t.entry_price - t.initial_stop) / t.entry_price * 100)
                   for t in reversed(trades) if not t.is_open][:200]
        signals = self.store.recent_signals(self.mode, 50)
        for s in signals:
            s["strategy"] = name(s.pop("strategy_id"))
            s["name"] = self.names.get(s["symbol"])
        return dict(
            time=now.isoformat(), mode=self.mode_label if self.trade_enabled else "通知のみ",
            live=self.mode == "live", trading=self.trade_enabled,
            settings=vars(self.settings), warnings=list(self.warnings.values()),
            health=self.health(now),
            buying_power=self.buying_power, buying_power_note=self.buying_power_note,
            capacity=self.capacity(),
            market=self.market_state(now), halted=self.risk.halted,
            feed_ok=self.risk.feed_ok, auto_chart=self.auto_chart, realized_today=self.risk.realized_today,
            limits=vars(self.risk.limits),
            exposure=sum(t.entry_price * t.qty for t in self.open),
            watchlist=[dict(symbol=s, name=self.names.get(s), price=self.prices.get(s),
                            excluded=(self.prices.get(s) or 0) < self.settings.min_price
                            and self.prices.get(s) is not None,
                            ranging=self.is_ranging(s),
                            frames=[dict(minutes=f["minutes"], trend=f["trend"]) for f in self.frames(s)[1:]],
                            vwap=self.vwap(s),
                            range_pct=self.range_pct(s), day_range_pct=self.day_range_pct(s),
                            change_pct=(self.quotes.get(s) or {}).get("ChangePreviousClosePer"))
                       for s in self.watchlist],
            positions=positions, signals=signals, history=history,
            stats=[dict(strategy=s.name, **summarize(by_strategy.get(s.id, [])))
                   for s in STRATEGIES if s.id not in (BOS_STRATEGY, EXT_STRATEGY)])   # 一本化した旧サインは出さない
