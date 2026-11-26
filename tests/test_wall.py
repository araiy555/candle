from datetime import datetime, timedelta

from candle.bars import JST
from candle.wall import WallParams, WallTracker, ask_levels

T0 = datetime(2026, 11, 27, 10, 0, 0, tzinfo=JST)


def board(price, asks):
    """asks: [(価格, 数量)]（安い順）。"""
    m = {"CurrentPrice": price}
    for i, (p, q) in enumerate(asks, start=1):
        m[f"Sell{i}"] = {"Price": p, "Qty": q}
    return m


def asks_with_wall(best, wall_price, wall_qty, n=10, base=2000):
    return [(best + i, wall_qty if best + i == wall_price else base) for i in range(n)]


def run(steps, params=None):
    """steps: (秒, 株価, 売り板, その配信までの出来高)。起きた出来事を返す。"""
    tr = WallTracker(params)
    out = []
    for sec, price, asks, vol in steps:
        out += tr.update("7203", board(price, asks), T0 + timedelta(seconds=sec), vol)
    return out, tr


def test_ask_levels_and_wall_detection():
    assert ask_levels(board(1000, [(1002, 5), (1001, 9)])) == [(1001.0, 9.0), (1002.0, 5.0)]
    _, tr = run([(0, 998, asks_with_wall(999, 1003, 30000), 0)])
    w = tr.walls["7203"]
    assert (w.price, w.max_qty, w.ratio) == (1003, 30000, 15.0)
    _, tr = run([(0, 998, asks_with_wall(999, 1003, 6000), 0)])           # 3倍では足りない
    assert tr.walls == {}
    _, tr = run([(0, 98, asks_with_wall(99, 103, 30000), 0)])             # 金額が小さい（約300万円）
    assert tr.walls == {}


def test_wall_absorbed_by_buying_then_broken():
    steps = [(0, 998, asks_with_wall(999, 1003, 30000), 0),
             (15, 1002, asks_with_wall(1002, 1003, 30000), 500),          # 接近
             (20, 1003, asks_with_wall(1003, 1003, 18000), 12000),        # 売り板の価格で約定して減る
             (25, 1003, asks_with_wall(1003, 1003, 8000), 10000),
             (30, 1004, asks_with_wall(1004, 9999, 0), 8000)]             # 売り板が無くなり上へ
    events, tr = run(steps)
    assert [e.kind for e in events] == ["approach", "break"]
    m = events[-1].metrics
    assert m["executed"] == 30000 and m["cancelled"] == 0 and m["executed_share"] == 1.0
    assert m["outcome_label"] == "吸収して突破" and "吸収して突破" in events[-1].note
    assert tr.walls == {}                                                 # 終わった売り板は追わない


def test_wall_pulled_without_trades_is_not_a_break():
    steps = [(0, 998, asks_with_wall(999, 1003, 30000), 0),
             (15, 1002, asks_with_wall(1002, 1003, 30000), 500),
             (20, 1002, asks_with_wall(1002, 1003, 5000), 300),           # 約定が無いのに減る → 取消
             (25, 1004, asks_with_wall(1004, 9999, 0), 5000)]
    events, _ = run(steps)
    assert [e.kind for e in events] == ["approach", "pulled"]
    m = events[-1].metrics
    assert m["cancelled"] == 25000 and m["executed"] == 5000 and m["executed_share"] < 0.5


def test_replenishment_is_counted():
    steps = [(0, 998, asks_with_wall(999, 1003, 30000), 0),
             (15, 1003, asks_with_wall(1003, 1003, 20000), 10000),        # 1万株約定
             (20, 1003, asks_with_wall(1003, 1003, 30000), 0),            # 1万株積み直し
             (25, 1003, asks_with_wall(1003, 1003, 30000), 6000)]         # 6千株約定したが数量は同じ
    _, tr = run(steps)
    w = tr.walls["7203"]
    assert (w.executed, w.added) == (16000, 16000)


def test_rejected_after_approach_and_flicker_ignored():
    steps = [(0, 998, asks_with_wall(999, 1003, 30000), 0),
             (15, 1002, asks_with_wall(1002, 1003, 30000), 500),
             (40, 997, asks_with_wall(998, 1003, 30000), 3000)]           # 0.4%以上、下に離れた
    events, _ = run(steps)
    assert [e.kind for e in events] == ["approach", "rejected"]
    flicker = [(0, 998, asks_with_wall(999, 1003, 30000), 0),
               (3, 1004, asks_with_wall(1004, 9999, 0), 100)]             # 10秒もたずに消えた
    assert run(flicker)[0] == []
    assert run([(0, 998, [(999, 1), (1000, 2)], 0)])[0] == []             # 板が5本未満なら判定しない
