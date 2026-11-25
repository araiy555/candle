"""スクイーズ検知の履歴ページ（一覧・集計・詳細）。既存の管理画面とは独立している。

ここでは保存済みの記録を読むだけで、売買や検知には一切影響しない。
株価などの受信値はそのまま返し、騰落率などはここで計算して「計算値」として返す。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from statistics import median

from .squeeze_log import HORIZONS, POST_SECONDS, PRE_SECONDS

FLAT = 0.05   # 騰落率がこの%以内なら「横ばい」と数える


def _connect(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _pct(price, base):
    return None if price is None or not base else (price - base) / base * 100


def _summary(r) -> dict:
    """1件分。受信値（acquired）と計算値（computed）を分けて返す。"""
    horizons = json.loads(r["horizons"] or "{}")
    metrics = json.loads(r["metrics"] or "{}")
    conditions = json.loads(r["conditions"] or "[]")
    base = r["price"]
    hs = {}
    for h in HORIZONS:
        v = horizons.get(str(h))
        if v is None:
            hs[str(h)] = {"pending": True} if r["status"] == "tracking" else \
                {"missing": r["gap_reason"] or "記録できていません"}
        elif "missing" in v:
            hs[str(h)] = {"missing": v["missing"]}
        else:
            hs[str(h)] = {"price": v["price"], "age": v.get("age"), "ret": _pct(v["price"], base)}
    return dict(
        id=r["id"], status=r["status"], gap_reason=r["gap_reason"],
        acquired=dict(time=r["time"], symbol=r["symbol"], name=r["name"], price=base,
                      prev_close=r["prev_close"], change=r["change"], change_pct=r["change_pct"],
                      volume=r["volume"], max_price=r["max_price"], min_price=r["min_price"],
                      tracked_seconds=r["tracked_seconds"]),
        computed=dict(absorb_score=metrics.get("absorb_score"), run=metrics.get("run"),
                      buy_ratio=metrics.get("buy_ratio"), sell_share=metrics.get("sell_share"),
                      surge=metrics.get("surge"),
                      max_up=_pct(r["max_price"], base), max_down=_pct(r["min_price"], base)),
        conditions=conditions, horizons=hs, metrics=metrics)


def _aggregate(events: list[dict]) -> dict:
    by_h = []
    for h in HORIZONS:
        rets = [e["horizons"][str(h)]["ret"] for e in events if "ret" in e["horizons"][str(h)]]
        rets = [x for x in rets if x is not None]
        by_h.append(dict(
            seconds=h, n=len(rets), missing=len(events) - len(rets),
            up=sum(1 for x in rets if x > FLAT), down=sum(1 for x in rets if x < -FLAT),
            flat=sum(1 for x in rets if -FLAT <= x <= FLAT),
            avg=sum(rets) / len(rets) if rets else None, median=median(rets) if rets else None))
    hours: dict[int, list[dict]] = {}
    for e in events:
        hours.setdefault(datetime.fromisoformat(e["acquired"]["time"]).hour, []).append(e)
    by_hour = []
    for hour in sorted(hours):
        row = dict(hour=hour, n=len(hours[hour]))
        for h in HORIZONS:
            rets = [e["horizons"][str(h)].get("ret") for e in hours[hour]]
            rets = [x for x in rets if x is not None]
            row[str(h)] = dict(n=len(rets), avg=sum(rets) / len(rets) if rets else None)
        by_hour.append(row)
    full = [e for e in events if e["status"] == "done"]
    ups = [e["computed"]["max_up"] for e in full if e["computed"]["max_up"] is not None]
    downs = [e["computed"]["max_down"] for e in full if e["computed"]["max_down"] is not None]
    return dict(total=len(events), complete=len(full),
                incomplete=sum(1 for e in events if e["status"] == "incomplete"),
                tracking=sum(1 for e in events if e["status"] == "tracking"),
                flat_threshold=FLAT, by_horizon=by_h, by_hour=by_hour,
                avg_max_up=sum(ups) / len(ups) if ups else None,
                avg_max_down=sum(downs) / len(downs) if downs else None)


def list_events(db_path, date_from: str | None = None, date_to: str | None = None) -> dict:
    """検知の一覧（新しい順）と集計。"""
    if not db_path:
        return dict(error="この起動方法では履歴を表示できません", events=[], aggregate=_aggregate([]))
    sql, args = "SELECT * FROM squeeze_events WHERE 1=1", []
    if date_from:
        sql += " AND substr(time,1,10) >= ?"
        args.append(date_from)
    if date_to:
        sql += " AND substr(time,1,10) <= ?"
        args.append(date_to)
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(sql + " ORDER BY time DESC LIMIT 2000", args).fetchall()
            days = [r[0] for r in conn.execute(
                "SELECT DISTINCT substr(time,1,10) FROM squeeze_events ORDER BY 1 DESC")]
    except sqlite3.OperationalError:
        rows, days = [], []      # まだ1件も記録がない
    events = [_summary(r) for r in rows]
    return dict(events=events, aggregate=_aggregate(events), days=days, horizons=list(HORIZONS))


def get_event(db_path, event_id: int) -> dict | None:
    if not db_path:
        return None
    try:
        with _connect(db_path) as conn:
            r = conn.execute("SELECT * FROM squeeze_events WHERE id=?", (event_id,)).fetchone()
            if r is None:
                return None
            ticks = [dict(t) for t in conn.execute(
                "SELECT rel,price,vol,side,ask,ask_qty,bid,bid_qty FROM squeeze_ticks "
                "WHERE event_id=? ORDER BY rel", (event_id,))]
    except sqlite3.OperationalError:
        return None
    out = _summary(r)
    out["board"] = json.loads(r["board"]) if r["board"] else None
    out["tape"] = json.loads(r["tape"]) if r["tape"] else None
    out["ticks"] = ticks
    out["window"] = dict(pre=PRE_SECONDS, post=POST_SECONDS)
    pre = [t for t in ticks if t["rel"] < 0]
    out["pre_coverage"] = round(-pre[0]["rel"], 1) if pre else 0.0   # 検知前に実際に残っていた秒数
    return out


PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>スクイーズ検知履歴</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--mut:#6b7585;--line:#e3e6eb;--up:#c0392b;--down:#1f7a4d;--warn:#b7791f;--acc:#2f5fd0;--calc:#eef2fb}
@media(prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e6e9ee;--mut:#98a2b3;--line:#2b323d;--up:#ff7b6b;--down:#4cc38a;--warn:#e0b050;--acc:#7da2ff;--calc:#222a3a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,"Yu Gothic UI",Meiryo,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:2}
h1{font-size:16px;margin:0}h2{font-size:14px;margin:0 0 8px}h3{font-size:13px;margin:14px 0 6px}
a{color:var(--acc)}main{padding:16px;display:grid;gap:16px;max-width:1500px;margin:auto}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:5px 8px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;font-size:12px}th.l,td.l{text-align:left}tr:last-child td{border-bottom:0}
td.wrap{white-space:normal;text-align:left;max-width:640px}
#list th,#list td{padding:4px 6px;font-size:13px}#list th .tag{display:none}
tbody tr.row{cursor:pointer}tbody tr.row:hover{background:var(--calc)}
.pos{color:var(--up)}.neg{color:var(--down)}.mut{color:var(--mut)}.miss{color:var(--warn)}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:4px;border:1px solid var(--line);color:var(--mut);margin-left:4px;vertical-align:1px}
.tag.c{background:var(--calc)}th .tag{margin-left:2px}
.ok{color:var(--down)}.ng{color:var(--mut)}
select,button{font:inherit;padding:5px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
button{cursor:pointer}.kpis{display:flex;flex-wrap:wrap;gap:22px}.kpis>div>span{display:block;color:var(--mut);font-size:12px}.kpis b{font-size:18px;white-space:nowrap}
.note{color:var(--mut);font-size:12px;margin-top:8px}.warnbox{border:1px solid var(--warn);border-radius:6px;padding:8px 10px;margin-bottom:10px}
.chart{position:relative}.chart svg{display:block;width:100%}.tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12px;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.15);display:none}
.legend{display:flex;gap:16px;font-size:12px;color:var(--mut);margin:4px 0}.legend i{display:inline-block;width:22px;height:0;border-top:2px solid var(--acc);vertical-align:middle;margin-right:5px}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
[hidden]{display:none!important}
</style></head><body>
<header><h1>スクイーズ検知履歴</h1><a href="/">← 管理画面へ戻る</a><span style="flex:1"></span>
<span class="mut" style="font-size:12px"><span class="tag">取得</span> 受信した値　<span class="tag c">計算</span> 受信値から計算した値　<span class="miss">欠損</span> 受信できなかった値</span></header>
<main id="listView">
<section><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><label for="day">期間</label><select id="day"></select><button id="reload">更新</button><span class="mut" id="count"></span></div></section>
<section><h2>集計</h2><div id="agg"></div></section>
<section><h2>検知一覧（新しい順）</h2><div id="list"></div><div class="note">検知日時・銘柄・株価・前日比は受信した値、それ以外の列は計算した値です。行をクリックすると詳細を開きます。成立条件：吸収＝売り板の吸収、連続＝買い約定の連続、耐性＝売り圧力への耐性。</div></section>
</main>
<main id="detailView" hidden>
<section><button id="back">← 一覧へ戻る</button> <b id="dtitle" style="font-size:16px;margin-left:8px"></b><span id="dstatus" style="margin-left:8px"></span></section>
<section id="dwarn" hidden></section>
<section><h2>検知時の情報</h2><div class="kpis" id="dkpis"></div></section>
<section><h2>検知条件</h2><div id="dconds"></div></section>
<section><h2>検知後の株価</h2><div id="dhor"></div></section>
<section><h2>株価の推移（検知前60秒〜検知後5分）</h2><div class="legend"><span><i></i>約定値</span><span><i style="border-top:1px dashed var(--mut)"></i>最良売り気配</span><span><i style="border-top:1px dotted var(--mut)"></i>最良買い気配</span></div>
<div class="chart"><svg id="chart" height="320" role="img"></svg><div class="tip" id="tip"></div></div><div class="note" id="chartnote"></div></section>
<section class="two" style="border:0;background:none;padding:0"><section><h2>検知時の板 <span class="tag">取得</span></h2><div id="dboard"></div></section>
<section><h2>約定の履歴（配信から作成） <span class="tag c">計算</span></h2><div id="dtape" style="max-height:420px;overflow:auto"></div></section></section>
<section><h2>歩み値（APIから取得） <span class="tag">取得</span></h2><div id="dapi" style="max-height:420px;overflow:auto"></div></section>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const n=(v,d=0)=>v==null?null:Number(v).toLocaleString('ja-JP',{minimumFractionDigits:d,maximumFractionDigits:d});
const MISS='<span class="miss">欠損</span>';
const num=(v,d=0,unit='')=>v==null?MISS:n(v,d)+unit;
const pct=v=>v==null?MISS:`<span class="${v>0?'pos':v<0?'neg':''}">${v>0?'+':''}${n(v,2)}%</span>`;
const dt=s=>new Date(s).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'});
const lab=h=>h<60?h+'秒後':(h/60)+'分後';
const G='<span class="tag">取得</span>',C='<span class="tag c">計算</span>';
let H=[10,30,60,180,300],data=null;
function hcell(v){if(v.pending)return '<span class="mut">記録中</span>';if(v.missing)return `<span class="miss" title="${esc(v.missing)}">欠損</span>`;return pct(v.ret)}
function table(heads,rows,cls){return '<table><thead><tr>'+heads.map(h=>`<th${h.l?' class="l"':''}>${h.t??h}</th>`).join('')+'</tr></thead><tbody>'+rows.map(r=>`<tr${r.attr||''}>`+r.cells.map(c=>`<td${c&&c.l?' class="l"':c&&c.w?' class="wrap"':''}>${c&&c.v!==undefined?c.v:c}</td>`).join('')+'</tr>').join('')+'</tbody></table>'}
async function loadList(){
  const day=$('day').value;let q='';if(day&&day!=='all')q=`?from=${day}&to=${day}`;
  let d;try{d=await (await fetch('/api/squeeze/list'+q)).json()}catch(e){$('list').innerHTML='<div class="miss">読み込めませんでした。プログラムが動いているか確認してください。</div>';return}
  data=d;H=d.horizons||H;
  if($('day').options.length===0){$('day').innerHTML='<option value="all">すべての日</option>'+(d.days||[]).map(x=>`<option>${x}</option>`).join('')}
  if(d.error){$('list').innerHTML=`<div class="miss">${esc(d.error)}</div>`;$('agg').innerHTML='';return}
  const a=d.aggregate;$('count').textContent=`${a.total}件（追跡完了 ${a.complete}／途中で途切れた ${a.incomplete}／記録中 ${a.tracking}）`;
  if(!a.total){$('agg').innerHTML='<div class="mut">まだ検知がありません。</div>';$('list').innerHTML='<div class="mut">検知されると、上がったかどうかに関係なくここに残ります。</div>';return}
  const share=(k,r)=>r.n?`${n(r[k]/r.n*100,0)}%（${r[k]}件）`:'<span class="mut">-</span>';
  $('agg').innerHTML=table([{t:'経過',l:1},'対象件数','欠損','上昇 '+C,'横ばい '+C,'下落 '+C,'平均騰落率 '+C,'中央値 '+C],
    a.by_horizon.map(r=>({cells:[{l:1,v:lab(r.seconds)},r.n,r.missing?`<span class="miss">${r.missing}</span>`:'0',share('up',r),share('flat',r),share('down',r),r.avg==null?'<span class="mut">-</span>':pct(r.avg),r.median==null?'<span class="mut">-</span>':pct(r.median)]})))+
    `<div class="note">横ばいは騰落率が±${a.flat_threshold}%以内。欠損した検知は割合と平均の計算から除いています。追跡が5分完了した ${a.complete}件の平均：最大上昇 ${a.avg_max_up==null?'-':pct(a.avg_max_up)}、最大下落 ${a.avg_max_down==null?'-':pct(a.avg_max_down)}</div>`+
    '<h3>検知した時間帯別の平均騰落率 '+C+'</h3>'+table([{t:'時間帯',l:1},'件数',...H.map(lab)],
    a.by_hour.map(r=>({cells:[{l:1,v:r.hour+'時台'},r.n,...H.map(h=>r[h].avg==null?'<span class="mut">-</span>':pct(r[h].avg)+` <span class="mut">(${r[h].n})</span>`)]})));
  $('list').innerHTML=table([{t:'検知日時 '+G,l:1},{t:'銘柄 '+G,l:1},'株価 '+G,'前日比 '+G,'出来高比 '+C,'吸収 '+C,'買い連続 '+C,'売り比率 '+C,{t:'成立条件',l:1},...H.map(h=>lab(h)+' '+C),'最大上昇 '+C,'最大下落 '+C,{t:'状態',l:1}],
    d.events.map(e=>{const q=e.acquired,c=e.computed;return {attr:` class="row" data-id="${e.id}"`,cells:[{l:1,v:dt(q.time)},{l:1,v:`<a href="#${e.id}">${esc(q.symbol)} ${esc(q.name||'')}</a>`},num(q.price,1),q.change_pct==null?MISS:pct(q.change_pct),
      c.surge==null?MISS:n(c.surge,1)+'倍',c.absorb_score==null?MISS:n(c.absorb_score,1)+'倍',c.run==null?MISS:c.run+'回',c.sell_share==null?MISS:n(c.sell_share*100,0)+'%',
      {l:1,v:e.conditions.filter(x=>x.ok).map(x=>({absorb:'吸収',run:'連続',resist:'耐性'}[x.key]||esc(x.label))).join('・')},...H.map(h=>hcell(e.horizons[h])),
      e.status==='tracking'?'<span class="mut">記録中</span>':pct(c.max_up),e.status==='tracking'?'<span class="mut">記録中</span>':pct(c.max_down),
      {l:1,v:e.status==='done'?'完了':e.status==='tracking'?'<span class="mut">記録中</span>':`<span class="miss" title="${esc(e.gap_reason||'')}">途中まで（${n(q.tracked_seconds,0)}秒）</span>`}]}}));
}
const NS='http://www.w3.org/2000/svg';
function el(tag,attrs,text){const e=document.createElementNS(NS,tag);for(const k in attrs)e.setAttribute(k,attrs[k]);if(text!=null)e.textContent=text;return e}
function ticks(lo,hi,count){const raw=(hi-lo)/count||1,p=Math.pow(10,Math.floor(Math.log10(raw))),f=raw/p,step=(f<1.5?1:f<3?2:f<7?5:10)*p,out=[];for(let v=Math.ceil(lo/step)*step;v<=hi+1e-9;v+=step)out.push(v);return out}
function draw(e){
  const svg=$('chart'),tip=$('tip');svg.innerHTML='';const W=svg.clientWidth||1000,Ht=320,L=10,R=70,T=16,B=28;svg.setAttribute('viewBox',`0 0 ${W} ${Ht}`);
  const tk=e.ticks.filter(t=>t.price!=null),pre=e.window.pre,post=e.window.post;
  if(!tk.length){svg.appendChild(el('text',{x:W/2,y:Ht/2,'text-anchor':'middle',fill:'var(--warn)','font-size':13},'この検知の株価データは保存されていません'));return}
  const vals=tk.flatMap(t=>[t.price,t.ask,t.bid]).filter(v=>v!=null);let lo=Math.min(...vals),hi=Math.max(...vals);const pad=(hi-lo)*0.1||hi*0.001;lo-=pad;hi+=pad;
  const x=s=>L+(s+pre)/(pre+post)*(W-L-R),y=v=>T+(hi-v)/(hi-lo)*(Ht-T-B);
  const dec=hi<1000?1:0,f=v=>v.toLocaleString('ja-JP',{minimumFractionDigits:dec,maximumFractionDigits:dec});
  for(const v of ticks(lo,hi,5)){svg.appendChild(el('line',{x1:L,x2:W-R,y1:y(v),y2:y(v),stroke:'var(--line)'}));svg.appendChild(el('text',{x:W-R+6,y:y(v)+4,fill:'var(--mut)','font-size':11},f(v)))}
  for(const s of [-60,0,60,120,180,240,300])svg.appendChild(el('text',{x:x(s),y:Ht-8,'text-anchor':'middle',fill:'var(--mut)','font-size':11},s===0?'検知':(s>0?'+':'')+s+'秒'));
  const tracked=e.status==='done'?post:e.acquired.tracked_seconds;
  if(tracked<post){svg.appendChild(el('rect',{x:x(tracked),y:T,width:x(post)-x(tracked),height:Ht-T-B,fill:'var(--warn)',opacity:.12}));
    svg.appendChild(el('text',{x:(x(tracked)+x(post))/2,y:T+16,'text-anchor':'middle',fill:'var(--warn)','font-size':12},e.status==='tracking'?'記録中':'欠損（記録なし）'))}
  if(e.pre_coverage<pre-5){svg.appendChild(el('rect',{x:x(-pre),y:T,width:x(-e.pre_coverage)-x(-pre),height:Ht-T-B,fill:'var(--warn)',opacity:.12}))}
  const step=(key)=>{let d='',prev=null;for(const t of tk){if(t[key]==null){prev=null;continue}d+=(prev==null?'M':'H')+x(t.rel)+(prev==null?','+y(t[key]):'')+'V'+y(t[key]);prev=t[key]}return d};
  svg.appendChild(el('path',{d:step('ask'),fill:'none',stroke:'var(--mut)','stroke-width':1,'stroke-dasharray':'5 3'}));
  svg.appendChild(el('path',{d:step('bid'),fill:'none',stroke:'var(--mut)','stroke-width':1,'stroke-dasharray':'1 3'}));
  svg.appendChild(el('path',{d:step('price'),fill:'none',stroke:'var(--acc)','stroke-width':2}));
  svg.appendChild(el('line',{x1:x(0),x2:x(0),y1:T,y2:Ht-B,stroke:'var(--fg)','stroke-width':1,'stroke-dasharray':'3 3'}));
  for(const h of H){const v=e.horizons[h];if(v&&v.price!=null){svg.appendChild(el('circle',{cx:x(h),cy:y(v.price),r:5,fill:'var(--acc)',stroke:'var(--card)','stroke-width':2}))}}
  const cross=el('line',{y1:T,y2:Ht-B,stroke:'var(--mut)',opacity:0});svg.appendChild(cross);
  const hit=el('rect',{x:L,y:T,width:W-L-R,height:Ht-T-B,fill:'transparent'});svg.appendChild(hit);
  hit.onmousemove=ev=>{const r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)*W/r.width,s=(px-L)/(W-L-R)*(pre+post)-pre;let b=null;for(const t of tk){if(t.rel<=s)b=t;else break}if(!b)b=tk[0];
    cross.setAttribute('x1',x(b.rel));cross.setAttribute('x2',x(b.rel));cross.setAttribute('opacity',.7);
    tip.innerHTML=`<b>${b.rel>=0?'+':''}${n(b.rel,1)}秒</b><br>約定値 ${f(b.price)}<br>売り気配 ${b.ask==null?'欠損':f(b.ask)}（${b.ask_qty==null?'-':n(b.ask_qty)}株）<br>買い気配 ${b.bid==null?'欠損':f(b.bid)}（${b.bid_qty==null?'-':n(b.bid_qty)}株）`;
    tip.style.display='block';const tx=x(b.rel)*r.width/W;tip.style.left=(tx>r.width/2?tx-tip.offsetWidth-10:tx+14)+'px';tip.style.top='18px'};
  hit.onmouseleave=()=>{cross.setAttribute('opacity',0);tip.style.display='none'};
}
async function loadDetail(id){
  let e;try{const r=await fetch('/api/squeeze/event?id='+id);if(!r.ok)throw 0;e=await r.json()}catch(x){$('dtitle').textContent='この検知は見つかりません';return}
  const q=e.acquired,c=e.computed;$('dtitle').textContent=`${q.symbol} ${q.name||''}　${dt(q.time)}`;
  $('dstatus').innerHTML=e.status==='done'?'<span class="ok">5分間の追跡が完了</span>':e.status==='tracking'?'<span class="mut">記録中（5分経つと完了します）</span>':'<span class="miss">追跡が途中で途切れています</span>';
  const warn=[];if(e.status==='incomplete')warn.push(`検知後 ${n(q.tracked_seconds,0)}秒までしか記録できていません。理由：${esc(e.gap_reason||'不明')}`);
  if(e.pre_coverage<e.window.pre-5)warn.push(`検知前のデータは ${n(e.pre_coverage,0)}秒分しか残っていません（起動直後や受信開始直後のため）。`);
  $('dwarn').hidden=!warn.length;$('dwarn').innerHTML=warn.map(w=>`<div class="miss">${w}</div>`).join('');
  $('dkpis').innerHTML=[['検知時株価 '+G,num(q.price,1,'円')],['前日比 '+G,q.change==null?MISS:`${q.change>0?'+':''}${n(q.change,1)}円（${pct(q.change_pct)}）`],['前日終値 '+G,num(q.prev_close,1,'円')],['当日出来高 '+G,num(q.volume,0,'株')],
    ['出来高比率 '+C,c.surge==null?MISS:n(c.surge,1)+'倍'],['売り板吸収スコア '+C,c.absorb_score==null?MISS:n(c.absorb_score,1)+'倍'],['買い約定の連続 '+C,c.run==null?MISS:c.run+'回'],['買い約定の比率 '+C,c.buy_ratio==null?MISS:n(c.buy_ratio*100,0)+'%'],['売り約定の比率 '+C,c.sell_share==null?MISS:n(c.sell_share*100,0)+'%']]
    .map(([k,v])=>`<div><span>${k}</span><b>${v}</b></div>`).join('');
  $('dconds').innerHTML=table([{t:'条件',l:1},{t:'成否',l:1},{t:'具体的な理由（検知時点の数値）',l:1}],e.conditions.map(x=>({cells:[{l:1,v:esc(x.label)},{l:1,v:x.ok?'<b class="ok">成立</b>':'<span class="ng">不成立</span>'},{w:1,v:esc(x.text)}]})))+
    `<div class="note">3つのうち2つ以上が成立し、出来高が通常の2倍以上のときに検知します。直近${n(e.metrics.window,0)}秒の約定 ${n(e.metrics.trades)}回・${n(e.metrics.volume)}株（通常ペースでの見込み ${n(e.metrics.expected_volume)}株）。約定の向きは、配信からの推定です。</div>`;
  const mx=e.status==='tracking'?'<span class="mut">記録中</span>':`${pct(c.max_up)}（${num(q.max_price,1,'円')}）`,mn=e.status==='tracking'?'<span class="mut">記録中</span>':`${pct(c.max_down)}（${num(q.min_price,1,'円')}）`;
  $('dhor').innerHTML=table([{t:'経過',l:1},'株価 '+G,'騰落率 '+C,{t:'備考',l:1}],H.map(h=>{const v=e.horizons[h];return {cells:[{l:1,v:lab(h)},v.price!=null?n(v.price,1)+'円':v.pending?'<span class="mut">記録中</span>':MISS,v.price!=null?pct(v.ret):v.pending?'':MISS,
      {l:1,v:v.missing?`<span class="miss">${esc(v.missing)}</span>`:v.price!=null&&v.age>0?`<span class="mut">その時点で最後の約定は ${n(v.age,0)}秒前</span>`:''}]}}))+
    `<div class="note">株価は、その時点までに受信した最後の約定値です。最大上昇 ${mx}／最大下落 ${mn}`+(e.status==='incomplete'?`　<span class="miss">※記録できた ${n(q.tracked_seconds,0)}秒間の範囲での値です</span>`:'')+'</div>';
  draw(e);$('chartnote').textContent='丸印は検知後10秒・30秒・1分・3分・5分の株価。薄く色のついた範囲はデータがない時間です。気配は最良気配のみです。';
  if(e.board){const lv=e.board.levels,rows=[];for(let i=10;i>=1;i--){const s=lv['Sell'+i];if(s)rows.push({cells:[{l:1,v:'売り'+i},n(s.Qty)??MISS,n(s.Price,1),'']})}
    for(let i=1;i<=10;i++){const b=lv['Buy'+i];if(b)rows.push({cells:[{l:1,v:'買い'+i},'',n(b.Price,1),n(b.Qty)??MISS]})}
    $('dboard').innerHTML=table([{t:'',l:1},'売り数量','値段','買い数量'],rows)+`<div class="note">成行売り ${num(e.board.MarketOrderSellQty)}／成行買い ${num(e.board.MarketOrderBuyQty)}／OVER ${num(e.board.OverSellQty)}／UNDER ${num(e.board.UnderBuyQty)}</div>`}
  else $('dboard').innerHTML='<div class="miss">欠損：検知時の板は受信内容に含まれていませんでした。</div>';
  const tr=e.ticks.filter(t=>t.vol>0);
  $('dtape').innerHTML=tr.length?table([{t:'経過',l:1},'約定値','数量',{t:'向き（推定）',l:1},'売り気配','買い気配'],tr.map(t=>({cells:[{l:1,v:(t.rel>=0?'+':'')+n(t.rel,1)+'秒'},n(t.price,1),n(t.vol),{l:1,v:t.side>0?'<span class="pos">買い</span>':t.side<0?'<span class="neg">売り</span>':'<span class="mut">不明</span>'},t.ask==null?MISS:n(t.ask,1),t.bid==null?MISS:n(t.bid,1)]})))+
    '<div class="note">配信の出来高の差から作った履歴です。配信1回分が1行で、複数の約定がまとまっていることがあります。</div>':'<div class="miss">欠損：約定の履歴は保存されていません。</div>';
  const tp=e.tape;$('dapi').innerHTML=!tp?'<div class="mut">未取得（追跡が終わった後に取得します）</div>':tp.missing?`<div class="miss">欠損：${esc(tp.missing)}</div>`:
    table([{t:'経過',l:1},'約定値','数量'],tp.rows.map(r=>({cells:[{l:1,v:(r.rel>=0?'+':'')+n(r.rel,1)+'秒'},num(r.price,1),num(r.volume)]})))+`<div class="note">kabuステーションの歩み値APIの応答（${n(tp.source_rows)}件）のうち、この時間帯の ${tp.rows.length}件。売り買いの向きはAPIに含まれません。</div>`;
}
function route(){const id=location.hash.slice(1);$('listView').hidden=!!id;$('detailView').hidden=!id;window.scrollTo(0,0);if(id)loadDetail(id);else loadList()}
document.addEventListener('click',e=>{const r=e.target.closest('tr.row');if(r&&!e.target.closest('a'))location.hash=r.dataset.id});
$('back').onclick=()=>{location.hash=''};$('reload').onclick=loadList;$('day').onchange=loadList;
window.addEventListener('hashchange',route);route();
setInterval(()=>{if(!location.hash)loadList();else if(document.querySelector('#dstatus .mut'))loadDetail(location.hash.slice(1))},10000);
</script></body></html>"""
