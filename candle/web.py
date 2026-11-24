"""管理画面。標準ライブラリのみで、このPC（127.0.0.1）からだけ閲覧できる。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>candle 管理画面</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--mut:#6b7585;--line:#e3e6eb;--up:#c0392b;--down:#1f7a4d;--warn:#b7791f;--acc:#2f5fd0}
@media(prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e6e9ee;--mut:#98a2b3;--line:#2b323d;--up:#ff7b6b;--down:#4cc38a;--warn:#e0b050;--acc:#7da2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,"Yu Gothic UI",Meiryo,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0}
h1{font-size:16px;margin:0 8px 0 0}h2{font-size:14px;margin:0 0 8px}
.pill{padding:2px 10px;border-radius:999px;border:1px solid var(--line);font-size:12px}
.ok{color:var(--down)}.ng{color:var(--up)}.warn{color:var(--warn)}
main{padding:16px;display:grid;gap:16px;max-width:1400px;margin:auto}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:5px 8px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;font-size:12px}th:first-child,td:first-child,.l{text-align:left}
tr:last-child td{border-bottom:0}.pos{color:var(--up)}.neg{color:var(--down)}.mut{color:var(--mut)}
button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.stop{background:#c0392b;border-color:#c0392b;color:#fff;font-weight:600}
.kpis{display:flex;flex-wrap:wrap;gap:20px}.kpis div span{display:block;color:var(--mut);font-size:12px}.kpis b{font-size:18px;white-space:nowrap}
.watch{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:6px}
.watch div{border:1px solid var(--line);border-radius:6px;padding:4px 8px;display:flex;justify-content:space-between}
.empty{color:var(--mut);padding:8px 0}
.link{cursor:pointer;color:var(--acc);text-decoration:underline;text-underline-offset:2px;background:none;border:0;padding:0;font:inherit}
.watch button.w{all:unset;cursor:pointer;border:1px solid var(--line);border-radius:6px;padding:4px 8px;display:flex;justify-content:space-between;gap:8px}
.watch button.w:hover,.watch button.w:focus-visible{border-color:var(--acc)}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--fg);padding:0;width:min(1100px,96vw)}
dialog::backdrop{background:rgba(0,0,0,.45)}
.dh{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}
.dh b{font-size:16px;margin-right:8px}.dh a{color:var(--acc);font-size:13px}
.chart{position:relative;padding:8px 8px 12px}.chart svg{display:block;width:100%;height:360px}
.tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12px;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.15);display:none;font-variant-numeric:tabular-nums}
.note{color:var(--mut);font-size:12px;padding:0 16px 12px}
</style></head><body>
<header><h1>candle</h1><span class="pill" id="mode"></span><span class="pill" id="feed"></span>
<span class="pill" id="halt"></span><span class="mut" id="time"></span>
<span style="flex:1"></span><button id="btn"></button></header>
<main>
<section><div class="kpis" id="kpis"></div></section>
<section><h2>保有ポジション</h2><div id="positions"></div></section>
<section><h2>戦略別成績</h2><div id="stats"></div></section>
<section><h2>検出されたサイン</h2><div id="signals"></div></section>
<section><h2>取引履歴</h2><div id="history"></div></section>
<section><h2>監視銘柄</h2><div class="watch" id="watch"></div></section>
</main>
<dialog id="dlg"><div class="dh"><b id="dt"></b><button id="copy">銘柄コードをコピー</button>
<a id="l1" target="_blank" rel="noopener">Yahoo!ファイナンス</a><a id="l2" target="_blank" rel="noopener">TradingView</a><a id="l3" target="_blank" rel="noopener">株探</a>
<span style="flex:1"></span><button id="close">閉じる</button></div>
<div class="chart"><svg id="svg" role="img"></svg><div class="tip" id="tip"></div></div>
<div class="note" id="note"></div></dialog>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const n=(v,d=0)=>v==null?'-':Number(v).toLocaleString('ja-JP',{minimumFractionDigits:d,maximumFractionDigits:d});
const yen=v=>v==null?'-':`<span class="${v>0?'pos':v<0?'neg':''}">${v>0?'+':''}${n(v)}</span>`;
const rr=v=>v==null?'-':`<span class="${v>0?'pos':v<0?'neg':''}">${v>0?'+':''}${n(v,2)}R</span>`;
const hm=s=>s?new Date(s).toLocaleTimeString('ja-JP',{timeZone:'Asia/Tokyo',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'-';
const md=s=>s?new Date(s).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'-';
function table(el,heads,rows,empty){
  if(!rows.length){$(el).innerHTML=`<div class="empty">${empty}</div>`;return}
  $(el).innerHTML='<table><thead><tr>'+heads.map(h=>`<th>${h}</th>`).join('')+'</tr></thead><tbody>'+
    rows.map(r=>'<tr>'+r.map(c=>`<td>${c}</td>`).join('')+'</tr>').join('')+'</tbody></table>';
}
let halted=false,state=null,cur=null;
async function load(){
  let d;try{d=await (await fetch('/api/state')).json()}catch(e){$('feed').innerHTML='<span class="ng">プログラム停止中</span>';return}
  if(!d.time){$('time').textContent='起動中…';return}
  halted=d.halted;state=d;
  $('mode').textContent=d.mode;$('time').textContent=md(d.time)+' 更新';
  $('feed').innerHTML=d.feed_ok?'<span class="ok">API接続中</span>':'<span class="ng">API切断（新規停止）</span>';
  $('halt').innerHTML=d.halted?'<span class="ng">緊急停止中</span>':'<span class="ok">新規受付中</span>';
  $('btn').textContent=d.halted?'新規受付を再開':'緊急停止';$('btn').className=d.halted?'':'stop';
  const unreal=d.positions.reduce((a,p)=>a+(p.pnl||0),0);
  $('kpis').innerHTML=[['含み損益',yen(unreal)+' 円'],['本日の確定損益',yen(d.realized_today)+' 円'],
    ['投資額 / 上限',n(d.exposure)+' / '+n(d.limits.total_cap)+' 円'],['1日の最大損失',n(d.limits.daily_max_loss)+' 円'],
    ['保有数',d.positions.length]].map(([k,v])=>`<div><span>${k}</span><b>${v}</b></div>`).join('');
  table('positions',['戦略','銘柄','株数','サイン時刻','エントリー','現在値','初期損切り','現在の損切り','含み損益','R','保有時間'],
    d.positions.map(p=>[esc(p.strategy),esc(p.symbol),n(p.qty),hm(p.signal_time),n(p.entry_price,1),n(p.price,1),n(p.initial_stop,1),
      n(p.stop,1),yen(p.pnl),rr(p.r),n(p.hold_min)+'分']),'保有ポジションはありません');
  table('stats',['戦略','取引数','勝率','PF','期待損益(円)','期待値(R)','合計損益(円)','平均保有'],
    d.stats.map(s=>[esc(s.strategy),s.trades,s.win_rate==null?'-':n(s.win_rate*100,1)+'%',s.pf==null?'-':n(s.pf,2),
      yen(s.expectancy),rr(s.expectancy_r),yen(s.total),s.avg_hold_min==null?'-':n(s.avg_hold_min)+'分']),'');
  table('signals',['戦略','銘柄','発生時刻','価格','パターン安値','結果'],
    d.signals.map(s=>[esc(s.strategy),`<button class="link" data-sym="${esc(s.symbol)}">${esc(s.symbol)} ${esc(s.name||'')}</button>`,md(s.time),n(s.price,1),n(s.pattern_low,1),
      s.action==='entry'?'エントリー':s.action==='通知のみ'?'通知':'<span class="mut">見送り: '+esc(s.action)+'</span>']),'まだサインはありません');
  table('history',['戦略','銘柄','株数','買い','買値','決済','売値','理由','損益(円)','R'],
    d.history.map(t=>[esc(t.strategy),esc(t.symbol),n(t.qty),md(t.entry_time),n(t.entry_price,1),md(t.exit_time),n(t.exit_price,1),
      esc(t.reason),yen(t.pnl),rr(t.r)]),'決済済みの取引はありません');
  $('watch').innerHTML=d.watchlist.map(w=>`<button class="w" data-sym="${esc(w.symbol)}"><span>${esc(w.symbol)} <span class="mut">${esc(w.name||'')}</span></span><span>${n(w.price,1)}</span></button>`).join('')||'<div class="empty">未登録</div>';
}
$('btn').onclick=async()=>{
  if(!halted&&!confirm('新規の買いを停止します。保有中のポジションの損切り・決済は継続します。'))return;
  await fetch(halted?'/api/resume':'/api/halt',{method:'POST'});load();
};
const NS='http://www.w3.org/2000/svg';
function el(tag,attrs,text){const e=document.createElementNS(NS,tag);for(const k in attrs)e.setAttribute(k,attrs[k]);if(text!=null)e.textContent=text;return e}
function ticks(lo,hi,count){const raw=(hi-lo)/count,p=Math.pow(10,Math.floor(Math.log10(raw))),f=raw/p,step=(f<1.5?1:f<3?2:f<7?5:10)*p,out=[];
  for(let v=Math.ceil(lo/step)*step;v<=hi+1e-9;v+=step)out.push(v);return out}
function draw(data){
  const svg=$('svg'),tip=$('tip'),bars=data.bars;svg.innerHTML='';tip.style.display='none';
  const W=svg.clientWidth||900,H=360,L=8,R=64,T=14,B=26;svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  if(!bars.length){svg.appendChild(el('text',{x:W/2,y:H/2,'text-anchor':'middle',fill:'var(--mut)','font-size':13},'まだ足がありません（起動後に確定した足から表示します）'));return}
  const marks=(state?.signals||[]).filter(s=>s.symbol===data.symbol);
  let lo=Math.min(...bars.map(b=>b.l),...marks.map(m=>m.pattern_low)),hi=Math.max(...bars.map(b=>b.h));
  const pad=(hi-lo)*0.08||hi*0.002;lo-=pad*1.6;hi+=pad;
  const n0=Math.max(bars.length,30),bw=(W-L-R)/n0,x=i=>L+bw*(i+.5),y=v=>T+(hi-v)/(hi-lo)*(H-T-B);
  const dec=hi<1000?1:0,fmt=v=>v.toLocaleString('ja-JP',{minimumFractionDigits:dec,maximumFractionDigits:dec});
  for(const v of ticks(lo,hi,5)){svg.appendChild(el('line',{x1:L,x2:W-R,y1:y(v),y2:y(v),stroke:'var(--line)','stroke-width':1}));
    svg.appendChild(el('text',{x:W-R+6,y:y(v)+4,fill:'var(--mut)','font-size':11},fmt(v)))}
  const every=Math.max(1,Math.ceil(70/bw));
  bars.forEach((b,i)=>{if(i%every===0)svg.appendChild(el('text',{x:Math.max(x(i),L+16),y:H-8,'text-anchor':'middle',fill:'var(--mut)','font-size':11},
    new Date(b.t).toLocaleTimeString('ja-JP',{timeZone:'Asia/Tokyo',hour:'2-digit',minute:'2-digit'})))});
  const w=Math.max(1,Math.min(14,bw*0.7));
  bars.forEach((b,i)=>{const col=b.c>b.o?'var(--up)':b.c<b.o?'var(--down)':'var(--mut)',g=el('g',{opacity:b.ok?1:.35});
    g.appendChild(el('line',{x1:x(i),x2:x(i),y1:y(b.h),y2:y(b.l),stroke:col,'stroke-width':1}));
    const top=y(Math.max(b.o,b.c)),h=Math.max(1,y(Math.min(b.o,b.c))-top);
    g.appendChild(el('rect',{x:x(i)-w/2,y:top,width:w,height:h,fill:col}));svg.appendChild(g)});
  const ms=(data.minutes||5)*60000;
  for(const m of marks){const i=bars.findIndex(b=>Date.parse(b.t)+ms===Date.parse(m.time));if(i<0)continue;
    const py=y(m.pattern_low);
    svg.appendChild(el('line',{x1:x(i)-bw,x2:W-R,y1:py,y2:py,stroke:'var(--fg)','stroke-width':1,'stroke-dasharray':'4 3',opacity:.6}));
    svg.appendChild(el('path',{d:`M${x(i)},${py+6} l6,10 h-12 z`,fill:'var(--fg)'}));
    const right=x(i)+140>W-R;svg.appendChild(el('text',{x:x(i)+(right?-10:10),y:py+17,'text-anchor':right?'end':'start',fill:'var(--fg)','font-size':11},`${m.strategy} 安値 ${fmt(m.pattern_low)}`))}
  const cross=el('line',{y1:T,y2:H-B,stroke:'var(--mut)','stroke-width':1,opacity:0});svg.appendChild(cross);
  const hit=el('rect',{x:L,y:T,width:W-L-R,height:H-T-B,fill:'transparent'});svg.appendChild(hit);
  hit.onmousemove=e=>{const r=svg.getBoundingClientRect(),px=(e.clientX-r.left)*W/r.width,i=Math.max(0,Math.min(bars.length-1,Math.floor((px-L)/bw))),b=bars[i];
    cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));cross.setAttribute('opacity',.7);
    tip.innerHTML=`<b>${new Date(b.t).toLocaleTimeString('ja-JP',{timeZone:'Asia/Tokyo',hour:'2-digit',minute:'2-digit'})}</b>${b.ok?'':' <span class="mut">（不完全）</span>'}<br>始 ${fmt(b.o)}　高 ${fmt(b.h)}<br>安 ${fmt(b.l)}　終 ${fmt(b.c)}<br>出来高 ${n(b.v)}`;
    tip.style.display='block';const tx=x(i)*r.width/W;tip.style.left=(tx>r.width/2?tx-tip.offsetWidth-4:tx+20)+'px';tip.style.top='20px'};
  hit.onmouseleave=()=>{cross.setAttribute('opacity',0);tip.style.display='none'};
}
async function chart(){if(!cur)return;let d;try{d=await (await fetch('/api/bars?symbol='+encodeURIComponent(cur))).json()}catch(e){return}
  if(d.symbol!==cur)return;$('dt').textContent=`${d.symbol} ${d.name||''}`;
  $('note').textContent=`${d.minutes||''}分足・当日分（このプログラムが起動中に受信した足）。赤=陽線、緑=陰線、薄い足=データ欠落を含む足。▲と点線はサインとパターン安値。`;
  if(!$('tip').style.display||$('tip').style.display==='none')draw(d)}
function openChart(sym){cur=sym;$('l1').href=`https://finance.yahoo.co.jp/quote/${sym}.T/chart`;$('l2').href=`https://jp.tradingview.com/chart/?symbol=TSE%3A${sym}`;
  $('l3').href=`https://kabutan.jp/stock/chart?code=${sym}`;$('copy').textContent='銘柄コードをコピー';$('svg').innerHTML='';$('dt').textContent=sym;$('dlg').showModal();chart()}
document.addEventListener('click',e=>{const b=e.target.closest('[data-sym]');if(b)openChart(b.dataset.sym)});
$('close').onclick=()=>$('dlg').close();$('dlg').addEventListener('close',()=>{cur=null});
$('dlg').addEventListener('click',e=>{if(e.target===$('dlg'))$('dlg').close()});
$('copy').onclick=async()=>{try{await navigator.clipboard.writeText(cur);$('copy').textContent='コピーしました'}catch(e){$('copy').textContent='コピーできませんでした'}};
load();setInterval(load,2000);setInterval(chart,5000);
</script></body></html>"""


def start_web(engine, port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path.startswith("/api/bars?symbol="):
                symbol = self.path.split("=", 1)[1][:10]
                if not symbol.isalnum():
                    return self._send(b"bad symbol", "text/plain", 400)
                self._send(json.dumps(engine.chart_data(symbol), ensure_ascii=False).encode("utf-8"),
                           "application/json")
            elif self.path == "/api/state":
                self._send(json.dumps(engine.snapshot, ensure_ascii=False).encode("utf-8"),
                           "application/json")
            else:
                self._send(b"not found", "text/plain", 404)

        def do_POST(self):
            # 他サイトからの操作を防ぐため、同一オリジンのリクエストだけ受け付ける
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://localhost:{port}", f"http://127.0.0.1:{port}"):
                return self._send(b"forbidden", "text/plain", 403)
            if self.path == "/api/halt":
                engine.risk.halted = True
            elif self.path == "/api/resume":
                engine.risk.halted = False
            else:
                return self._send(b"not found", "text/plain", 404)
            self._send(b"{}", "application/json")

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
