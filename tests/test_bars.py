from datetime import datetime

from candle.bars import JST, BarBuilder, bucket_start


def t(h, m, s=0):
    return datetime(2026, 11, 24, h, m, s, tzinfo=JST)


def test_bucket_start_sessions():
    assert bucket_start(t(9, 0)) == t(9, 0)
    assert bucket_start(t(9, 4, 59)) == t(9, 0)
    assert bucket_start(t(9, 5)) == t(9, 5)
    assert bucket_start(t(11, 30)) == t(11, 25)   # 前引けは最後の足へ
    assert bucket_start(t(15, 30)) == t(15, 25)   # 大引けは最後の足へ
    assert bucket_start(t(8, 59, 59)) is None
    assert bucket_start(t(11, 30, 1)) is None
    assert bucket_start(t(12, 0)) is None
    assert bucket_start(t(15, 30, 1)) is None


def test_builds_ohlcv_from_cumulative_volume():
    b = BarBuilder(5)
    assert b.on_tick("7203", t(8, 55), 3000, 0) == []       # 寄り前の基準値
    assert b.on_tick("7203", t(9, 0, 1), 3010, 1000) == []
    assert b.on_tick("7203", t(9, 1), 3030, 1500) == []
    assert b.on_tick("7203", t(9, 1), 3030, 1500) == []     # 板だけの更新は無視
    assert b.on_tick("7203", t(9, 4), 3005, 1800) == []
    closed = b.on_tick("7203", t(9, 5, 2), 3020, 2500)
    assert len(closed) == 1
    bar = closed[0]
    assert (bar.start, bar.open, bar.high, bar.low, bar.close) == (t(9, 0), 3010, 3030, 3005, 3005)
    assert bar.volume == 1800 and bar.ticks == 3 and bar.complete


def test_day_high_low_fill_missed_extremes():
    b = BarBuilder(5)
    b.on_tick("A", t(8, 55), 100, 0, None, None)
    b.on_tick("A", t(9, 0), 100, 10, 100, 100)
    # 配信の合間に105まで付いて戻った → 当日高値の更新で拾う
    b.on_tick("A", t(9, 2), 101, 50, 105, 99)
    bar = b.close_all()[0]
    assert bar.high == 105 and bar.low == 99 and bar.close == 101


def test_join_mid_session_marks_incomplete():
    b = BarBuilder(5)
    b.on_tick("A", t(10, 2), 100, 50_000)                   # 途中参加: 基準値のみ
    b.on_tick("A", t(10, 3), 101, 50_100)
    closed = b.on_tick("A", t(10, 5), 102, 50_300)
    assert closed[0].volume == 100 and not closed[0].complete
    closed = b.on_tick("A", t(10, 10), 103, 50_400)
    assert closed[0].start == t(10, 5) and closed[0].complete


def test_gap_marks_incomplete_and_rebases_volume():
    b = BarBuilder(5)
    b.on_tick("A", t(8, 55), 100, 0)
    b.on_tick("A", t(9, 1), 100, 100)
    b.mark_gap()                                            # 切断
    b.on_tick("A", t(9, 3), 104, 900)                       # 再接続後の最初の1件は基準値
    b.on_tick("A", t(9, 4), 105, 950)
    closed = b.on_tick("A", t(9, 6), 106, 1000)
    assert not closed[0].complete
    assert closed[0].volume == 150                          # 切断中の800株は足に混ぜない
    nxt = b.close_all()[0]
    assert nxt.start == t(9, 5) and nxt.complete


def test_flush_closes_after_grace_and_drops_late():
    b = BarBuilder(5, grace_seconds=5)
    b.on_tick("A", t(8, 55), 100, 0)
    b.on_tick("A", t(9, 1), 100, 100)
    assert b.flush(t(9, 5, 4)) == []
    assert len(b.flush(t(9, 5, 5))) == 1
    b.on_tick("A", t(9, 4, 59), 99, 200)                    # 確定済みの足への遅延データ
    assert b.dropped_late == 1 and b.close_all() == []


def test_no_bar_outside_session_and_new_day_resets():
    b = BarBuilder(5)
    b.on_tick("A", t(8, 55), 100, 0)
    b.on_tick("A", t(9, 1), 100, 100)
    b.on_tick("A", t(11, 40), 101, 150)                     # 立会時間外の出来高は足にしない
    nxt = datetime(2026, 11, 25, 9, 0, 5, tzinfo=JST)
    closed = b.on_tick("A", datetime(2026, 11, 25, 8, 50, tzinfo=JST), 101, 0)
    assert len(closed) == 1 and closed[0].volume == 100
    b.on_tick("A", nxt, 102, 300)
    bar = b.close_all()[0]
    assert bar.start.day == 25 and bar.volume == 300 and bar.complete


def test_one_minute_bars_and_contiguity():
    from candle.patterns import expected_next
    assert bucket_start(t(9, 0, 59), 1) == t(9, 0)
    assert bucket_start(t(9, 1), 1) == t(9, 1)
    assert bucket_start(t(11, 30), 1) == t(11, 29) and bucket_start(t(15, 30), 1) == t(15, 29)
    assert expected_next(t(11, 29), 1) == t(12, 30) and expected_next(t(15, 29), 1) is None
    b = BarBuilder(1)
    b.on_tick("A", t(8, 55), 100, 0)
    b.on_tick("A", t(9, 0, 10), 100, 10)
    b.on_tick("A", t(9, 0, 50), 102, 30)
    (bar,) = b.on_tick("A", t(9, 1, 5), 101, 40)
    assert (bar.start, bar.minutes, bar.open, bar.close, bar.volume) == (t(9, 0), 1, 100, 102, 30)


def test_config_with_bom_and_one_minute(tmp_path):
    from candle.config import load_config
    p = tmp_path / "config.toml"
    p.write_bytes(b"\xef\xbb\xbf[bars]\nminutes = 1\n")
    assert load_config(p).bar_minutes == 1


def test_first_trade_of_day_ignores_stale_day_low():
    """その日の最初の約定では、配信の当日安値（前日の値が残っていることがある）で足を補正しない。"""
    from datetime import datetime
    from candle.bars import JST, BarBuilder
    bb = BarBuilder(1)
    t = lambda h, m, s=0: datetime(2026, 11, 27, h, m, s, tzinfo=JST)
    bb.on_tick("T", t(8, 59), 4625, 0, None, None)                     # 寄り付き前（基準）
    bb.on_tick("T", t(9, 0, 1), 4600, 1000, 4713, 4584)                # 最初の約定。高安は前日の値のまま
    bb.on_tick("T", t(9, 0, 30), 4610, 2000, 4612, 4598)               # 以降は当日の値
    (bar,) = bb.on_tick("T", t(9, 1, 5), 4605, 3000, 4612, 4598)
    assert (bar.open, bar.high, bar.low, bar.close) == (4600, 4610, 4600, 4610)   # 前日の値を足に混ぜない
