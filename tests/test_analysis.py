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
