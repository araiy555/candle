"""取引結果の分析ページ。決済済みの取引を、いろいろな切り口で集計して比べる（読み取り専用）。

どの条件のときに勝ち、どの条件のときに負けているかを見て、設定を決める材料にする。
件数が少ない区分は偶然のぶれが大きいので、件数も必ず並べて表示する。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .bars import JST, in_continuous

WEEK = "月火水木金土日"
HTF = {"up": (0, "上昇"), "range": (1, "レンジ"), "down": (2, "下降"), "none": (3, "判定できず")}


def _bucket(v, edges, unit="", fmt="{:g}"):
    """v を区切り edges の区間に入れ、(並び順, 表示名) を返す。v が None なら「記録なし」。"""
    if v is None:
        return (99, "記録なし")
    for i, e in enumerate(edges):
        if v < e:
            lo = fmt.format(edges[i - 1]) + unit + "〜" if i else ""
            return (i, f"{lo}{fmt.format(e)}{unit}未満")
    return (len(edges), f"{fmt.format(edges[-1])}{unit}以上")


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    pnls = [r["pnl"] for r in rows]
    gain, loss = sum(x for x in pnls if x > 0), -sum(x for x in pnls if x < 0)
    return dict(n=n, wins=sum(1 for x in pnls if x > 0), win_rate=sum(1 for x in pnls if x > 0) / n,
                total=sum(pnls), avg=sum(pnls) / n, avg_r=sum(r["r"] for r in rows) / n,
                pf=gain / loss if loss > 0 else None, gain=gain, loss=loss,
                avg_win=gain / max(1, sum(1 for x in pnls if x > 0)),
                avg_loss=-loss / max(1, sum(1 for x in pnls if x < 0)))


def _group(rows: list[dict], key, title: str, note: str = "", by_total: bool = False) -> dict:
    groups: dict = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    items = [dict(label=k[1], order=k[0], **_stats(v)) for k, v in groups.items()]
    items.sort(key=(lambda x: x["total"]) if by_total else (lambda x: x["order"]))
    return dict(title=title, note=note, rows=items)


def analyze(db_path, mode: str = "paper", date_from: str | None = None, date_to: str | None = None) -> dict:
    if not db_path:
        return dict(error="この起動方法では分析を表示できません", total=None, groups=[], days=[])
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
            extra = ", t.range_pct" if "range_pct" in cols else ", NULL AS range_pct"
            htf = ", t.htf" if "htf" in cols else ", '' AS htf"
            trades = conn.execute(
                f"SELECT t.id, t.strategy_id, t.symbol, t.signal_time, t.entry_time, t.entry_price, t.qty, "
                f"t.initial_stop, t.high, t.exit_time, t.exit_price, t.exit_reason{extra}{htf} FROM trades t "
                f"WHERE t.mode=? AND t.exit_time IS NOT NULL ORDER BY t.exit_time", (mode,)).fetchall()
            sigs = {(r["strategy_id"], r["symbol"], r["time"]): r for r in conn.execute(
                "SELECT strategy_id, symbol, time, price, vwap, drop_pct FROM signals WHERE mode=?", (mode,))}
            names = {r[0]: r[1] for r in conn.execute(
                "SELECT symbol, name FROM watchlist WHERE name IS NOT NULL")}
        finally:
            conn.close()
    except sqlite3.Error as e:
        return dict(error=f"データを読めませんでした（{e}）", total=None, groups=[], days=[])

    rows, days = [], set()
    for t in trades:
        et = datetime.fromisoformat(t["entry_time"]).astimezone(JST)
        xt = datetime.fromisoformat(t["exit_time"]).astimezone(JST)
        day = et.strftime("%Y-%m-%d")
        days.add(day)
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        risk = t["entry_price"] - t["initial_stop"]
        move = t["exit_price"] - t["entry_price"]
        sig = sigs.get((t["strategy_id"], t["symbol"], t["signal_time"]))
        stop_pct = risk / t["entry_price"] * 100
        rng = t["range_pct"]
        rows.append(dict(
            pnl=move * t["qty"], r=move / risk if risk > 0 else 0.0, day=day, et=et,
            hold=(xt - et).total_seconds() / 60, symbol=t["symbol"], name=names.get(t["symbol"]),
            price=t["entry_price"], reason=t["exit_reason"] or "不明", strategy=t["strategy_id"],
            htf=dict(x.split(":") for x in (t["htf"] or "").split(",") if ":" in x),
            stop_pct=stop_pct, range_pct=rng, stop_per_range=stop_pct / rng if rng else None,
            drop_pct=sig["drop_pct"] if sig else None,
            vwap_diff=(t["entry_price"] - sig["vwap"]) / sig["vwap"] * 100 if sig and sig["vwap"] else None,
            # 買った後に一度でも含み益になった幅（リスク幅の何倍まで伸びたか）
            best_r=(t["high"] - t["entry_price"]) / risk if risk > 0 and t["high"] else None))
    if not rows:
        return dict(total=None, groups=[], days=sorted(days, reverse=True), mode=mode)

    half = lambda r: (r["et"].hour * 2 + r["et"].minute // 30,
                      f"{r['et'].hour}:{'30' if r['et'].minute >= 30 else '00'}〜")
    groups = [
        _group(rows, lambda r: (r["day"], f"{r['day'][5:]}（{WEEK[r['et'].weekday()]}）"), "日別"),
        _group(rows, half, "買った時間帯別（30分ごと）"),
        _group(rows, lambda r: _bucket(r["range_pct"], [0.10, 0.15, 0.20, 0.30], "%"), "足の平均値幅率別",
               "値動きの大きさ。小さいほどレンジ。設定「足の平均値幅率の下限」を決める材料。"),
        _group(rows, lambda r: _bucket(r["drop_pct"], [0.3, 0.5, 0.8, 1.2], "%"), "直前の下落率別",
               "サインの前にどれだけ下げていたか。設定「直前の下落率の下限」を決める材料。"),
        _group(rows, lambda r: _bucket(r["stop_pct"], [0.1, 0.2, 0.3, 0.5], "%"), "損切り幅別（買値からの%）",
               "買値から最初の損切りラインまでの幅。狭すぎると少しの揺れで切られやすい。"),
        _group(rows, lambda r: _bucket(r["stop_per_range"], [1, 2, 3, 5], "倍"), "損切り幅÷足の平均値幅率",
               "損切り幅が、ふだんの足1本の値幅の何倍か。"),
        _group(rows, lambda r: HTF.get(r["htf"].get("5"), (99, "記録なし")), "上位足（5分足）の向き別",
               "買った時点の5分足の向き。波の高値・安値が切り上がっていれば上昇、切り下がっていれば下降。"),
        _group(rows, lambda r: HTF.get(r["htf"].get("15"), (99, "記録なし")), "上位足（15分足）の向き別",
               "買った時点の15分足の向き。寄り付きから時間がたっていないと、足が少なく判定できません。"),
        _group(rows, lambda r: (99, "記録なし") if r["vwap_diff"] is None else
               (0, "VWAPより下で買い") if r["vwap_diff"] < 0 else (1, "VWAP以上で買い"), "VWAPとの位置"),
        _group(rows, lambda r: _bucket(r["price"], [1000, 2000, 3000, 5000, 10000], "円", "{:,.0f}"), "株価帯別"),
        _group(rows, lambda r: _bucket(r["hold"], [2, 5, 10, 30], "分"), "保有時間別"),
        _group(rows, lambda r: _bucket(r["best_r"], [0.25, 0.5, 1, 2], "R"), "買った後の最大の含み益（リスク幅の何倍か）",
               "一度は利益が乗ったのに負けた取引が多ければ、利益の守り方（トレーリング）を見直す材料。"),
        _group(rows, lambda r: (0, r["reason"]), "決済理由別", by_total=True),
        _group(rows, lambda r: (0, f"{r['symbol']} {r['name'] or ''}".strip()), "銘柄別（損益の小さい順）",
               by_total=True),
    ]
    # 損益の推移（日ごとの累計）と、連敗の最大
    streak = worst = 0
    for r in rows:
        streak = streak + 1 if r["pnl"] <= 0 else 0
        worst = max(worst, streak)
    total = _stats(rows)
    total.update(max_losing_streak=worst, days=len({r["day"] for r in rows}),
                 best=max(r["pnl"] for r in rows), worst=min(r["pnl"] for r in rows),
                 avg_hold=sum(r["hold"] for r in rows) / len(rows))
    return dict(total=total, groups=groups, days=sorted(days, reverse=True), mode=mode)


# 通知の種類（検知履歴の kind と、その中の結果）→ (表示名, 見込む方向)。方向 -1 は「下がれば見込みどおり」
SWEEP_TAG = lambda **want: (lambda m: all(m.get(k) == v for k, v in want.items()))
# 下へのタッチ（側の記録が無い以前の検知も、こちらに数える）
LOW = lambda **want: (lambda m: m.get("side", "low") == "low" and all(m.get(k) == v for k, v in want.items()))
# (検知履歴の kind, 絞り込み, 表示名, 見込む方向)
SIGNAL_KINDS = [
    ("squeeze", None, "スクイーズ", 1),
    ("sweep", LOW(), "流動性タッチ：下にタッチ→上へ", 1),
    ("sweep", LOW(liquidity="external"), "　└ 外部流動性（当日安値・大きな波の安値・前日安値）", 1),
    ("sweep", LOW(liquidity="internal"), "　└ 内部流動性", 1),
    ("sweep", LOW(zone="discount"), "　└ 割安ゾーン", 1),
    ("sweep", LOW(zone="premium"), "　└ 割高ゾーン", 1),
    ("sweep", LOW(bos_setup=True), "　└ 内部流動性のうち、高値ブレイク後＋割安ゾーン", 1),
    ("sweep", SWEEP_TAG(side="high"), "流動性タッチ：上にタッチ→下へ（下がれば見込みどおり）", -1),
    ("sweep", SWEEP_TAG(side="high", liquidity="external"), "　└ 外部流動性（当日高値・大きな波の高値・前日高値）", -1),
    ("sweep", SWEEP_TAG(side="high", liquidity="internal"), "　└ 内部流動性 ", -1),
    ("open", None, "寄り付き押し目", 1),
    ("fvg", SWEEP_TAG(outcome="hold"), "FVG反発", 1),
    ("fvg", SWEEP_TAG(outcome="break"), "FVG割れ（下がれば見込みどおり）", -1),
    ("wall", SWEEP_TAG(outcome="break"), "売り板突破（吸収して突破）", 1),
    ("wall", SWEEP_TAG(outcome="pulled"), "売り板：取消で消えた（通知なし）", 1),
    ("wall", SWEEP_TAG(outcome="rejected"), "売り板：跳ね返された（通知なし）", 1),
]
MIN_N = 30          # これ未満は「データ不足」とする件数
EDGE = 0.03         # 見込みあり／薄いを分ける、1回あたりの平均（%）


def _verdict(n: int, sim_avg: float | None, avg5: float | None) -> str:
    if n < MIN_N or sim_avg is None or avg5 is None:
        return "データ不足"
    if sim_avg >= EDGE and avg5 > 0:
        return "見込みあり"
    if sim_avg <= -EDGE and avg5 < 0:
        return "見込み薄"
    return "どちらとも言えない"


def analyze_signals(db_path, date_from: str | None = None, date_to: str | None = None,
                    take: float = 0.3, cut: float = 0.3) -> dict:
    """通知（検知履歴）の種類ごとに、その後5分の値動きを比べる。

    騰落率は「見込む方向」にそろえる（FVG割れは下がればプラス）。あわせて、検知の株価で入り、
    +take% か -cut% のどちらかに先に届いたら手じまい、届かなければ5分後に手じまう、という
    単純な試算をする。手数料・すべり・実際に買えたかは含まない。
    """
    if not db_path:
        return dict(error="この起動方法では分析を表示できません", kinds=[], days=[])
    import json
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            has_kind = "kind" in {r[1] for r in conn.execute("PRAGMA table_info(squeeze_events)")}
            events = conn.execute(
                "SELECT id, time, price, metrics, horizons, max_price, min_price, status"
                + (", kind" if has_kind else ", 'squeeze' AS kind") + " FROM squeeze_events ORDER BY time").fetchall()
            paths: dict[int, list] = {}
            for r in conn.execute("SELECT event_id, price FROM squeeze_ticks WHERE rel>0 AND price IS NOT NULL "
                                  "ORDER BY event_id, rel"):
                paths.setdefault(r[0], []).append(r[1])
        finally:
            conn.close()
    except sqlite3.Error:
        return dict(kinds=[], days=[], take=take, cut=cut, min_n=MIN_N)      # まだ記録がない

    days = sorted({e["time"][:10] for e in events}, reverse=True)
    buckets: dict = {}
    skipped = 0
    for e in events:
        day = e["time"][:10]
        if (date_from and day < date_from) or (date_to and day > date_to) or not e["price"]:
            continue
        if not in_continuous(datetime.fromisoformat(e["time"])):
            skipped += 1      # 昼休み・引け前の気配で出た検知（以前の版の記録）。値動きの比較から除く
            continue
        m = json.loads(e["metrics"] or "{}")
        key = e["kind"]
        hz = json.loads(e["horizons"] or "{}")
        buckets.setdefault(key, []).append(dict(
            base=e["price"], hz=hz, hi=e["max_price"], lo=e["min_price"], done=e["status"] == "done",
            path=paths.get(e["id"], []), hour=int(e["time"][11:13]), m=m))

    out = []
    for kind, pick, label, d in SIGNAL_KINDS:
        key = (kind, None)
        rows = [r for r in buckets.get(kind, []) if pick is None or pick(r["m"])]
        if not rows:
            out.append(dict(label=label, n=0, verdict="データ不足"))
            continue
        ret = lambda r, h: (d * (r["hz"][h]["price"] / r["base"] - 1) * 100
                            if h in r["hz"] and r["hz"][h].get("price") else None)
        col = {}
        for h in ("60", "180", "300"):
            v = [x for x in (ret(r, h) for r in rows) if x is not None]
            col[h] = dict(n=len(v), avg=sum(v) / len(v) if v else None,
                          win=sum(1 for x in v if x > 0.05) / len(v) if v else None,
                          lose=sum(1 for x in v if x < -0.05) / len(v) if v else None)
        done = [r for r in rows if r["done"] and r["hi"] and r["lo"]]
        fav = [((r["hi"] if d > 0 else r["lo"]) / r["base"] - 1) * 100 * d for r in done]
        adv = [((r["lo"] if d > 0 else r["hi"]) / r["base"] - 1) * 100 * d for r in done]
        sims = []
        for r in done:
            res = None
            for p in r["path"]:
                x = d * (p / r["base"] - 1) * 100
                if x >= take:
                    res = take
                    break
                if x <= -cut:
                    res = -cut
                    break
            if res is None:
                res = ret(r, "300")
            if res is not None:
                sims.append(res)
        sim = dict(n=len(sims), avg=sum(sims) / len(sims) if sims else None,
                   take=sum(1 for x in sims if x >= take) / len(sims) if sims else None,
                   cut=sum(1 for x in sims if x <= -cut) / len(sims) if sims else None)
        hours: dict = {}
        for r in rows:
            v = ret(r, "300")
            if v is not None:
                hours.setdefault(r["hour"], []).append(v)
        item = dict(label=label, n=len(rows), h60=col["60"], h180=col["180"], h300=col["300"],
                    max_fav=sum(fav) / len(fav) if fav else None, max_adv=sum(adv) / len(adv) if adv else None,
                    sim=sim, verdict=_verdict(len(sims), sim["avg"], col["300"]["avg"]),
                    hours=[] if label.startswith("　") else
                    [dict(hour=h, n=len(v), avg=sum(v) / len(v)) for h, v in sorted(hours.items())])
        if kind == "open":      # 寄り付き押し目は、目標（+7ティック）と損切りのどちらに先に届いたかも出す
            oc = [r["m"].get("outcome") for r in rows]
            item["open"] = {k: oc.count(k) for k in ("target", "stop", "timeout")}
        out.append(item)
    return dict(kinds=out, days=days, take=take, cut=cut, min_n=MIN_N, skipped=skipped)


PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>candle 分析</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--mut:#6b7585;--line:#e3e6eb;--up:#c0392b;--down:#1f7a4d;--warn:#b7791f;--acc:#2f5fd0}
@media(prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e6e9ee;--mut:#98a2b3;--line:#2b323d;--up:#ff7b6b;--down:#4cc38a;--warn:#e0b050;--acc:#7da2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,"Yu Gothic UI","Hiragino Sans",sans-serif}
header{display:flex;gap:14px;align-items:center;padding:12px 16px;background:var(--card);border-bottom:1px solid var(--line)}h1{font-size:17px;margin:0}
a{color:var(--acc)}main{padding:16px;display:grid;gap:16px;max-width:1300px;margin:auto}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}h2{font-size:14px;margin:0 0 4px}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(560px,1fr));gap:16px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:12px;color:var(--mut);font-weight:500}th:first-child,td:first-child{text-align:left}tr:last-child td{border-bottom:0}
.pos{color:var(--up)}.neg{color:var(--down)}.mut{color:var(--mut)}.few td{opacity:.55}
.kpis{display:flex;gap:26px;flex-wrap:wrap}.kpis>div>span{display:block;font-size:12px;color:var(--mut)}.kpis b{font-size:19px}
.note{font-size:12px;color:var(--mut);margin:0 0 6px}select,button{font:inherit;padding:5px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
td.bar{width:120px;padding-left:0}td.bar i{display:block;height:10px;border-radius:2px}
</style></head><body>
<header><h1>分析</h1><a href="/">← 管理画面へ戻る</a></header>
<main>
<section><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><label for="mode">対象</label><select id="mode"><option value="paper">テスト（仮想売買）</option><option value="live">本番</option><option value="signals">通知（サインの後の値動き）</option></select>
<label for="day">期間</label><select id="day"></select><button id="reload">更新</button><span class="mut" id="msg"></span></div></section>
<section id="sigsec" hidden><h2>通知の種類ごとの比較</h2><div class="note" id="signote"></div><div id="sigs" style="overflow:auto"></div></section>
<div class="two" id="sighours"></div>
<section id="allsec"><h2>全体</h2><div class="kpis" id="kpis"></div><div class="note" id="knote" style="margin-top:8px"></div></section>
<div class="two" id="groups"></div>
</main>
<script>
const $=id=>document.getElementById(id),n=(v,d=0)=>v==null?'-':Number(v).toLocaleString('ja-JP',{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const yen=v=>`<span class="${v>0?'pos':v<0?'neg':''}">${v>0?'+':''}${n(v)}</span>`,rr=v=>`<span class="${v>0?'pos':v<0?'neg':''}">${v>0?'+':''}${n(v,2)}R</span>`;
const FEW=5;let daysLoaded=false;
const pc=v=>v==null?'<span class="mut">-</span>':`<span class="${v>0?'pos':v<0?'neg':''}">${v>0?'+':''}${n(v,2)}%</span>`,sh=v=>v==null?'<span class="mut">-</span>':n(v*100,0)+'%';
async function loadSignals(day){
  let q='';if(day&&day!=='all')q=`?from=${day}&to=${day}`;
  let d;try{d=await (await fetch('/api/analysis/signals'+q)).json()}catch(e){$('msg').textContent='読み込めませんでした';return}
  if(!daysLoaded){$('day').innerHTML='<option value="all">すべての日</option>'+(d.days||[]).map(x=>`<option>${x}</option>`).join('');daysLoaded=true}
  $('msg').textContent=d.error||'';
  const V={'見込みあり':'pos','見込み薄':'neg'};
  $('signote').innerHTML=`騰落率は検知したときの株価からの変化で、<b>見込む方向にそろえています</b>（FVG割れは下がればプラス）。「試算」は、検知の株価で入り、+${d.take}% か −${d.cut}% のどちらかに先に届いたら手じまい、届かなければ5分後に手じまう場合の1回あたりの平均です。手数料・すべり・実際にその値段で買えたかは含みません。判定は、試算できた件数が${d.min_n}件以上のときだけ出す目安です。流動性タッチの「└」の行は、場面ごとの内訳です（場面の記録を始める前の検知は、内訳には入りません）。`+(d.skipped?`<br>昼休み（11:30〜12:30）と引け前（15:25以降）に出ていた検知 ${d.skipped}件は、売買が成立しない時間の気配によるものなので、集計から除いています。`:'');
  $('sigs').innerHTML='<table><thead><tr><th>種類</th><th>件数</th><th>1分後</th><th>3分後</th><th>5分後</th><th>5分後に上／下</th><th>最大の伸び</th><th>最大の逆行</th><th>試算：1回あたり</th><th>利確／損切り</th><th>判定（目安）</th></tr></thead><tbody>'+
    d.kinds.map(k=>k.n?`<tr${k.sim.n<d.min_n?' class="few"':''}><td>${esc(k.label)}${k.open?`<br><span class="mut" style="font-size:12px">目標に到達 ${k.open.target}／損切り ${k.open.stop}／届かず ${k.open.timeout}</span>`:''}</td><td>${k.n}</td><td>${pc(k.h60.avg)}</td><td>${pc(k.h180.avg)}</td><td>${pc(k.h300.avg)}</td><td>${sh(k.h300.win)}／${sh(k.h300.lose)}</td><td>${pc(k.max_fav)}</td><td>${pc(k.max_adv)}</td><td><b>${pc(k.sim.avg)}</b> <span class="mut">(${k.sim.n})</span></td><td>${sh(k.sim.take)}／${sh(k.sim.cut)}</td><td class="${V[k.verdict]||'mut'}">${esc(k.verdict)}</td></tr>`
      :`<tr class="few"><td>${esc(k.label)}</td><td>0</td><td colspan="8" style="text-align:left" class="mut">まだ記録がありません</td><td class="mut">データ不足</td></tr>`).join('')+'</tbody></table>';
  $('sighours').innerHTML=d.kinds.filter(k=>k.n&&k.hours.length).map(k=>`<section><h2>${esc(k.label)}：検知した時間帯別（5分後の平均）</h2><table><thead><tr><th>時間帯</th><th>件数</th><th>5分後の平均</th></tr></thead><tbody>`+
    k.hours.map(h=>`<tr${h.n<FEW?' class="few"':''}><td>${h.hour}時台</td><td>${h.n}</td><td>${pc(h.avg)}</td></tr>`).join('')+'</tbody></table></section>').join('');
}
async function load(){
  const sig=$('mode').value==='signals';$('sigsec').hidden=!sig;$('sighours').hidden=!sig;$('allsec').hidden=sig;$('groups').hidden=sig;
  if(sig)return loadSignals($('day').value);
  const day=$('day').value;let q='?mode='+$('mode').value;if(day&&day!=='all')q+=`&from=${day}&to=${day}`;
  let d;try{d=await (await fetch('/api/analysis'+q)).json()}catch(e){$('msg').textContent='読み込めませんでした';return}
  if(!daysLoaded){$('day').innerHTML='<option value="all">すべての日</option>'+(d.days||[]).map(x=>`<option>${x}</option>`).join('');daysLoaded=true}
  $('msg').textContent=d.error||'';const t=d.total;
  if(!t){$('kpis').innerHTML='<span class="mut">決済済みの取引がまだありません。</span>';$('knote').textContent='';$('groups').innerHTML='';return}
  $('kpis').innerHTML=[['取引数',n(t.n)+'回（'+t.days+'日）'],['勝率',n(t.win_rate*100,1)+'%（'+t.wins+'勝）'],['合計損益',yen(t.total)+' 円'],['1回あたり',yen(t.avg)+' 円'],['期待値',rr(t.avg_r)],['PF',t.pf==null?'-':n(t.pf,2)],
    ['平均の勝ち',yen(t.avg_win)+' 円'],['平均の負け',yen(t.avg_loss)+' 円'],['最大の勝ち',yen(t.best)+' 円'],['最大の負け',yen(t.worst)+' 円'],['最大連敗',t.max_losing_streak+'回'],['平均保有',n(t.avg_hold,0)+'分']]
    .map(([k,v])=>`<div><span>${k}</span><b>${v}</b></div>`).join('');
  $('knote').textContent=`PFは「勝ちの合計÷負けの合計」で、1より大きければ利益が出ています。件数が${FEW}件未満の行は薄く表示しています（偶然のぶれが大きく、判断材料にしにくいため）。`;
  $('groups').innerHTML=d.groups.map(g=>{const mx=Math.max(...g.rows.map(r=>Math.abs(r.total)),1);
    return `<section><h2>${esc(g.title)}</h2>${g.note?`<div class="note">${esc(g.note)}</div>`:''}<table><thead><tr><th>区分</th><th>件数</th><th>勝率</th><th>合計損益</th><th></th><th>1回あたり</th><th>期待値</th><th>PF</th></tr></thead><tbody>`+
    g.rows.map(r=>`<tr${r.n<FEW?' class="few"':''}><td>${esc(r.label)}</td><td>${r.n}</td><td>${n(r.win_rate*100,0)}%</td><td>${yen(r.total)}</td><td class="bar"><i style="width:${Math.max(2,Math.abs(r.total)/mx*100)}%;background:${r.total>=0?'var(--up)':'var(--down)'}"></i></td><td>${yen(r.avg)}</td><td>${rr(r.avg_r)}</td><td>${r.pf==null?'-':n(r.pf,2)}</td></tr>`).join('')+'</tbody></table></section>'}).join('');
}
$('reload').onclick=load;$('mode').onchange=()=>{daysLoaded=false;load()};$('day').onchange=load;load();
</script></body></html>"""
