"""取引結果の分析ページ。決済済みの取引を、いろいろな切り口で集計して比べる（読み取り専用）。

どの条件のときに勝ち、どの条件のときに負けているかを見て、設定を決める材料にする。
件数が少ない区分は偶然のぶれが大きいので、件数も必ず並べて表示する。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .bars import JST

WEEK = "月火水木金土日"


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
            trades = conn.execute(
                f"SELECT t.id, t.strategy_id, t.symbol, t.signal_time, t.entry_time, t.entry_price, t.qty, "
                f"t.initial_stop, t.high, t.exit_time, t.exit_price, t.exit_reason{extra} FROM trades t "
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


PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>candle 取引の分析</title>
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
<header><h1>取引の分析</h1><a href="/">← 管理画面へ戻る</a></header>
<main>
<section><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><label for="mode">対象</label><select id="mode"><option value="paper">テスト（仮想売買）</option><option value="live">本番</option></select>
<label for="day">期間</label><select id="day"></select><button id="reload">更新</button><span class="mut" id="msg"></span></div></section>
<section><h2>全体</h2><div class="kpis" id="kpis"></div><div class="note" id="knote" style="margin-top:8px"></div></section>
<div class="two" id="groups"></div>
</main>
<script>
const $=id=>document.getElementById(id),n=(v,d=0)=>v==null?'-':Number(v).toLocaleString('ja-JP',{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const yen=v=>`<span class="${v>0?'pos':v<0?'neg':''}">${v>0?'+':''}${n(v)}</span>`,rr=v=>`<span class="${v>0?'pos':v<0?'neg':''}">${v>0?'+':''}${n(v,2)}R</span>`;
const FEW=5;let daysLoaded=false;
async function load(){
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
