"""PUSH配信（現値・累積出来高）から分足を組み立てる。

設計上の約束:
- 出来高は累積値の差分で求める。接続直後の最初の1件は基準値としてのみ使う。
- 足の途中から受信を始めた場合や切断を挟んだ場合、その足は complete=False とする。
  パターン検出は complete=True の足だけを使うこと。
- 約定のない時間帯の足は生成しない（足の連続性は start の間隔で判定する）。
- PUSHは全約定を配信するとは限らないため、当日高値・安値の更新を足の高値・安値に反映する。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

JST = timezone(timedelta(hours=9), "JST")

# 東証の立会時間（2024年11月以降、後場は15:30まで）
SESSIONS: tuple[tuple[time, time], ...] = (
    (time(9, 0), time(11, 30)),
    (time(12, 30), time(15, 30)),
)


@dataclass
class Bar:
    symbol: str
    start: datetime  # 足の開始時刻（JST）
    minutes: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    ticks: int = 0
    complete: bool = True
    vwap: float | None = None   # 足の確定時点のVWAP（配信で受信した値。無ければ None）

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)


@dataclass
class _State:
    day: date | None = None
    cum_volume: float | None = None  # 直近の累積出来高
    need_baseline: bool = True
    day_high: float | None = None
    day_low: float | None = None
    bar: Bar | None = None
    next_incomplete: bool = False  # 次に作る足は途中参加なので不完全
    last_closed_start: datetime | None = None


def bucket_start(ts: datetime, minutes: int = 5) -> datetime | None:
    """ts が属する足の開始時刻を返す。立会時間外は None。

    引け（11:30:00 / 15:30:00）ちょうどの約定は、その場の最後の足に含める。
    """
    ts = ts.astimezone(JST)
    t = ts.time()
    for s_open, s_close in SESSIONS:
        if s_open <= t <= s_close:
            day0 = ts.replace(hour=0, minute=0, second=0, microsecond=0)
            mins = ts.hour * 60 + ts.minute
            close_mins = s_close.hour * 60 + s_close.minute
            if mins >= close_mins:  # 引けちょうど
                mins = close_mins - 1
            return day0 + timedelta(minutes=mins - mins % minutes)
    return None


class BarBuilder:
    def __init__(self, minutes: int = 5, grace_seconds: float = 5.0):
        self.minutes = minutes
        self.grace = timedelta(seconds=grace_seconds)
        self._states: dict[str, _State] = {}
        self.dropped_late = 0

    def on_tick(
        self,
        symbol: str,
        ts: datetime,
        price: float | None,
        cum_volume: float | None,
        day_high: float | None = None,
        day_low: float | None = None,
        vwap: float | None = None,
    ) -> list[Bar]:
        """1件の配信を取り込み、確定した足があれば返す。

        ts は現値時刻（最後の約定時刻）、cum_volume は当日の累積出来高。
        """
        if price is None or ts is None:
            return []
        ts = ts.astimezone(JST)
        st = self._states.setdefault(symbol, _State())
        closed: list[Bar] = []
        cum = float(cum_volume or 0.0)

        # 日付が変わった、または累積出来高が巻き戻った → 新しい営業日として扱う
        if st.day != ts.date() or (st.cum_volume is not None and cum < st.cum_volume):
            if st.bar is not None:
                closed.append(self._close(st))
            fresh = st.day is not None and st.day != ts.date()
            st.day, st.cum_volume = ts.date(), None
            st.day_high = st.day_low = None
            # 稼働中に日付が変わった場合は累積0から始まるので基準取り直しは不要
            st.need_baseline = not (fresh and cum == 0.0)

        start = bucket_start(ts, self.minutes)

        if st.need_baseline:
            # 最初の1件は基準値としてのみ使う。すでに出来高があれば途中参加なので、
            # 次に作る足は不完全とする（昼休み中の接続でも安全側に倒して不完全扱い）。
            st.need_baseline = False
            st.cum_volume = cum
            st.day_high, st.day_low = day_high, day_low
            if cum > 0:
                if st.bar is not None and start == st.bar.start:
                    st.bar.complete = False  # 欠落は作成中の足の中に収まっている
                else:
                    st.next_incomplete = True
                    if st.bar is not None:
                        st.bar.complete = False
            return closed

        delta = cum - (st.cum_volume or 0.0)
        st.cum_volume = cum
        new_high = day_high is not None and (st.day_high is None or day_high > st.day_high)
        new_low = day_low is not None and (st.day_low is None or day_low < st.day_low)
        if day_high is not None:
            st.day_high = day_high
        if day_low is not None:
            st.day_low = day_low

        if delta <= 0 or start is None:
            return closed  # 板のみの更新、または立会時間外

        if st.bar is not None and start > st.bar.start:
            closed.append(self._close(st))
        if st.bar is None:
            if st.last_closed_start is not None and start <= st.last_closed_start:
                self.dropped_late += 1  # すでに確定した足への遅延データ
                return closed
            st.bar = Bar(symbol, start, self.minutes, price, price, price, price,
                         complete=not st.next_incomplete)
            st.next_incomplete = False
        elif start < st.bar.start:
            self.dropped_late += 1
            return closed

        b = st.bar
        b.close = price
        if vwap:
            b.vwap = float(vwap)
        b.high = max(b.high, price)
        b.low = min(b.low, price)
        # 配信の合間に付いた高値・安値を当日高安の更新から拾う
        if new_high and day_high is not None:
            b.high = max(b.high, day_high)
        if new_low and day_low is not None:
            b.low = min(b.low, day_low)
        b.volume += delta
        b.ticks += 1
        return closed

    def flush(self, now: datetime) -> list[Bar]:
        """終了時刻（+猶予）を過ぎた足を確定して返す。定期的に呼ぶ。"""
        now = now.astimezone(JST)
        closed = []
        for st in self._states.values():
            if st.bar is not None and now >= st.bar.end + self.grace:
                closed.append(self._close(st))
        return closed

    def mark_gap(self) -> None:
        """切断時に呼ぶ。作成中の足を不完全とし、次の1件を基準値として扱う。"""
        for st in self._states.values():
            st.need_baseline = True
            if st.bar is not None:
                st.bar.complete = False

    def close_all(self) -> list[Bar]:
        return [self._close(st) for st in self._states.values() if st.bar is not None]

    def _close(self, st: _State) -> Bar:
        bar = st.bar
        assert bar is not None
        st.bar = None
        st.last_closed_start = bar.start
        return bar
