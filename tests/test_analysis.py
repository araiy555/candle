from datetime import datetime, timedelta

from candle import analysis
from candle.bars import JST
from candle.store import Store
from candle.trade import Trade


def trade(store, sym, h, m, entry, exit_, stop, rng, hold=5, day=26, reason="トレーリング"):
    t0 = datetime(2026, 11, day, h, m, tzinfo=JST)
    t = Trade("paper", "tweezer_bottom", sym, t0, t0 + timedelta(seconds=10), entry, 100, stop, stop,
              high=max(entry, exit_), exit_time=t0 + timedelta(minutes=hold), exit_price=exit_,
              exit_reason=reason, range_pct=rng)
    store.save_trade(t)
    store.add_signal("paper", "tweezer_bottom", sym, t0, entry, stop, "entry", vwap=entry + 5, drop_pct=0.6)


def test_bucket_labels():
    assert analysis._bucket(0.05, [0.10, 0.15]) == (0, "0.1未満")
    assert analysis._bucket(0.12, [0.10, 0.15], "%") == (1, "0.1%〜0.15%未満")
    assert analysis._bucket(0.2, [0.10, 0.15], "%") == (2, "0.15%以上")
    assert analysis._bucket(None, [1]) == (99, "記録なし")


def test_analyze_groups_and_totals(tmp_path):
    db = tmp_path / "c.sqlite"
    store = Store(db)
    trade(store, "7011", 9, 40, 2700, 2720, 2690, 0.25)                # +2000  +2R
    trade(store, "7011", 10, 5, 2710, 2705, 2700, 0.08, day=27)        # -500   -0.5R
    trade(store, "8766", 10, 10, 600, 598, 596, 0.09, hold=40, day=27, reason="損切り")   # -200
    store.conn.commit()
    res = analysis.analyze(db)
    t = res["total"]
    assert (t["n"], t["wins"], t["total"], t["days"], t["max_losing_streak"]) == (3, 1, 1300, 2, 2)
    assert round(t["pf"], 2) == round(2000 / 700, 2) and t["best"] == 2000 and t["worst"] == -500
    g = {x["title"]: x["rows"] for x in res["groups"]}
    rng = {r["label"]: (r["n"], r["total"]) for r in g["足の平均値幅率別"]}
    assert rng == {"0.1%未満": (2, -700), "0.2%〜0.3%未満": (1, 2000)}
    assert [r["label"] for r in g["日別"]] == ["11-26（木）", "11-27（金）"]
    assert [(r["label"], r["n"]) for r in g["買った時間帯別（30分ごと）"]] == [("9:30〜", 1), ("10:00〜", 2)]
    assert g["VWAPとの位置"][0]["label"] == "VWAPより下で買い" and g["VWAPとの位置"][0]["n"] == 3
    assert [r["label"] for r in g["銘柄別（損益の小さい順）"]][0].startswith("8766")
    assert {r["label"] for r in g["決済理由別"]} == {"トレーリング", "損切り"}
    one = analysis.analyze(db, date_from="2026-11-27", date_to="2026-11-27")
    assert one["total"]["n"] == 2 and one["days"] == ["2026-11-27", "2026-11-26"]
    assert analysis.analyze(db, "live")["total"] is None               # 本番の取引は無い
    assert analysis.analyze(None)["error"]


def test_analyze_signals_compares_kinds_in_expected_direction(tmp_path):
    import json
    from candle.squeeze_log import SqueezeLog
    db = tmp_path / "c.sqlite"
    store = Store(db)
    SqueezeLog(store)

    def ev(kind, outcome, base, path, day=27, status="done"):
        hz = {"60": {"price": path[1]}, "180": {"price": path[-1]}, "300": {"price": path[-1]}}
        cur = store.conn.execute(
            "INSERT INTO squeeze_events (symbol,name,time,price,metrics,conditions,horizons,max_price,min_price,"
            "status,kind) VALUES ('7203','トヨタ',?,?,?,'[]',?,?,?,?,?)",
            (f"2026-11-{day}T10:05:00+09:00", base, json.dumps({"outcome": outcome}), json.dumps(hz),
             max(path), min(path), status, kind))
        store.conn.executemany("INSERT INTO squeeze_ticks (event_id, rel, price) VALUES (?,?,?)",
                               [(cur.lastrowid, 10.0 * (i + 1), p) for i, p in enumerate(path)])

    ev("squeeze", None, 1000, [1001, 1002, 1004])            # +0.3% に先に届く
    ev("squeeze", None, 1000, [999, 996, 1005])              # 先に -0.3% に届く（後で上がっても損切り）
    ev("squeeze", None, 1000, [1001, 1000, 1001], day=26)    # どちらにも届かず、5分後 +0.1%
    ev("fvg", "break", 1000, [999, 998, 996])                # 割れ：下がれば見込みどおり
    ev("fvg", "hold", 1000, [1001, 1002, 1002], status="incomplete")
    store.conn.execute(
        "INSERT INTO squeeze_events (symbol,time,price,metrics,conditions,horizons,max_price,min_price,status,kind)"
        " VALUES ('7203','2026-11-25T10:05:00+09:00',1000,?,'[]','{}',1003,999,'done','sweep')",
        (json.dumps({"bos_setup": True, "after_bos": True, "zone": "discount", "liquidity": "internal"}),))
    store.conn.commit()
    res = analysis.analyze_signals(db)
    k = {x["label"]: x for x in res["kinds"]}
    sq = k["スクイーズ"]
    assert sq["n"] == 3 and sq["sim"]["n"] == 3 and round(sq["sim"]["avg"], 4) == round((0.3 - 0.3 + 0.1) / 3, 4)
    assert round(sq["sim"]["take"], 2) == 0.33 and round(sq["sim"]["cut"], 2) == 0.33
    assert round(sq["h300"]["avg"], 2) == 0.33 and sq["verdict"] == "データ不足"     # 30件未満は判定しない
    br = k["FVG割れ（下がれば見込みどおり）"]
    assert round(br["h300"]["avg"], 2) == 0.4 and br["sim"]["avg"] == 0.3 and round(br["max_fav"], 2) == 0.4
    assert k["FVG反発"]["n"] == 1 and k["FVG反発"]["sim"]["n"] == 0          # 追跡が途切れたものは試算しない
    assert k["流動性ブレイク（すべて）"]["n"] == 1 and k["　└ 内部流動性のうち、高値ブレイク後＋割安ゾーン"]["n"] == 1
    assert k["　└ 内部流動性"]["n"] == 1 and k["　└ 外部流動性（当日安値・大きな波の安値・前日安値）"]["n"] == 0 and k["　└ 高値ブレイクなし"]["n"] == 0
    assert res["days"] == ["2026-11-27", "2026-11-26", "2026-11-25"]
    assert analysis.analyze_signals(db, "2026-11-26", "2026-11-26")["kinds"][0]["n"] == 1
    assert analysis._verdict(40, 0.05, 0.04) == "見込みあり" and analysis._verdict(40, -0.05, -0.02) == "見込み薄"
    assert analysis._verdict(40, 0.01, 0.02) == "どちらとも言えない"
    assert analysis.analyze_signals(tmp_path / "none.sqlite")["kinds"] == []


def test_board_signals_only_in_continuous_trading_hours(tmp_path):
    from candle.bars import in_continuous
    at = lambda h, m: datetime(2026, 11, 27, h, m, tzinfo=JST)
    assert [in_continuous(at(*x)) for x in ((8, 59), (9, 0), (11, 29), (11, 30), (12, 15), (12, 30), (15, 24),
                                           (15, 25))] == [False, True, True, False, False, True, True, False]
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from test_opening import board
    from candle.engine import PaperEngine
    from candle.risk import RiskLimits, RiskManager
    store, sent = Store(tmp_path / "c.sqlite"), []
    e = PaperEngine(store, RiskManager(RiskLimits()), enabled={"tweezer_bottom"}, trade_enabled=False,
                    notify=lambda *a: sent.append(a), notify_ids={"open_dip", "wall_break"})
    e.opening.p.end = at(13, 0).time()                       # 時間帯の判定だけを見るため、対象時間を広げる
    e.on_board("7203", board(1000, 1000), at(12, 10))        # 昼休みの気配
    e.on_board("7203", board(1000, 1000), at(12, 10) + timedelta(seconds=5))
    assert sent == [] and e.walls.walls == {}
    e.on_board("7203", board(1000, 1000), at(12, 40))
    e.on_board("7203", board(1000, 1000), at(12, 40) + timedelta(seconds=5))
    assert len(sent) == 1
