import pytest

from candle import patterns as P
from candle.patterns import Params, contiguous_tail, expected_next

from helpers import DOWN, make_bars

p = Params()

CASES = {
    "tweezer_bottom": ([(1010, 1011, 995, 998), (998, 1008, 995, 1006)], 995),
    "bullish_engulfing": ([(1010, 1011, 997, 1000), (999, 1014, 996, 1013)], 996),
    "hammer": ([(1004, 1005.5, 985, 1005)], 985),
    "morning_star": ([(1010, 1011, 989, 990), (989, 991, 985, 988), (990, 1004, 989, 1003)], 985),
    "piercing_line": ([(1010, 1011, 989, 990), (988, 1004, 986, 1003)], 986),
    "bullish_harami": ([(1010, 1011, 989, 990), (994, 1001, 993, 1000)], 989),
}


@pytest.mark.parametrize("name", CASES)
def test_pattern_detected_after_downtrend(name):
    tail, low = CASES[name]
    assert getattr(P, name)(make_bars(DOWN + tail), p) == low


@pytest.mark.parametrize("name", CASES)
def test_pattern_ignored_without_downtrend(name):
    tail, _ = CASES[name]
    up = [(900 + 10 * i, 915 + 10 * i, 898 + 10 * i, 910 + 10 * i) for i in range(6)]
    assert getattr(P, name)(make_bars(up + tail), p) is None
    assert getattr(P, name)(make_bars(tail), p) is None      # 前置きの足が足りない


def test_patterns_are_independent():
    """ある形が別の戦略のサインに混ざらないこと。"""
    hits = {s.id for s in P.STRATEGIES if s.detect(make_bars(DOWN + CASES["hammer"][0]), p)}
    assert hits == {"hammer"}
    assert P.hammer(make_bars(DOWN + [(1010, 1011, 995, 998)]), p) is None   # 普通の陰線


def test_double_bottom_breakout_only_once():
    shape = [(1050, 1052, 1038, 1040), (1040, 1042, 1028, 1030), (1030, 1032, 1018, 1020),
             (1020, 1022, 1000, 1005),                                   # 1番底 1000
             (1005, 1016, 1004, 1015), (1015, 1025, 1014, 1022),         # 戻り（ネック 1025）
             (1022, 1023, 1012, 1013), (1013, 1014, 1004, 1006),
             (1006, 1008, 1001, 1004),                                   # 2番底 1001
             (1004, 1012, 1003, 1011), (1011, 1020, 1010, 1019)]
    bars = make_bars(shape)
    assert P.double_bottom(bars, p) is None                              # まだ上抜けていない
    bars = make_bars(shape + [(1019, 1030, 1018, 1028)])
    assert P.double_bottom(bars, p) == 1000
    bars = make_bars(shape + [(1019, 1030, 1018, 1028), (1028, 1035, 1027, 1033)])
    assert P.double_bottom(bars, p) is None                              # 上抜け後は再発しない


def test_double_bottom_rejects_uneven_lows():
    shape = [(1050, 1052, 1038, 1040), (1040, 1042, 1028, 1030), (1030, 1032, 1018, 1020),
             (1020, 1022, 1000, 1005), (1005, 1016, 1004, 1015), (1015, 1025, 1014, 1022),
             (1022, 1023, 1012, 1013), (1013, 1014, 1009, 1010), (1010, 1012, 1008, 1011),
             (1011, 1015, 1010, 1014), (1014, 1020, 1013, 1019), (1019, 1030, 1018, 1028)]
    assert P.double_bottom(make_bars(shape), p) is None                  # 2番底が浅すぎる


def test_contiguity_and_lunch_break():
    bars = make_bars([(1, 2, 0.5, 1.5)] * 3, start=(11, 20))
    assert [b.start.strftime("%H:%M") for b in bars] == ["11:20", "11:25", "12:30"]
    assert expected_next(bars[-1].start.replace(hour=15, minute=25), 5) is None
    assert len(contiguous_tail(bars)) == 3
    bars[1].complete = False
    assert len(contiguous_tail(bars)) == 1                               # 不完全な足で途切れる
    gap = make_bars([(1, 2, 0.5, 1.5)] * 2) + make_bars([(1, 2, 0.5, 1.5)] * 2, start=(9, 20))
    assert len(contiguous_tail(gap)) == 2                                # 欠けた足で途切れる
