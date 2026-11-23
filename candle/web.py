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
let halted=false;
async function load(){
  let d;try{d=await (await fetch('/api/state')).json()}catch(e){$('feed').innerHTML='<span class="ng">プログラム停止中</span>';return}
  if(!d.time){$('time').textContent='起動中…';return}
  halted=d.halted;
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
    d.signals.map(s=>[esc(s.strategy),esc(s.symbol)+' <span class="mut">'+esc(s.name||'')+'</span>',md(s.time),n(s.price,1),n(s.pattern_low,1),
      s.action==='entry'?'エントリー':s.action==='通知のみ'?'通知':'<span class="mut">見送り: '+esc(s.action)+'</span>']),'まだサインはありません');
  table('history',['戦略','銘柄','株数','買い','買値','決済','売値','理由','損益(円)','R'],
    d.history.map(t=>[esc(t.strategy),esc(t.symbol),n(t.qty),md(t.entry_time),n(t.entry_price,1),md(t.exit_time),n(t.exit_price,1),
      esc(t.reason),yen(t.pnl),rr(t.r)]),'決済済みの取引はありません');
  $('watch').innerHTML=d.watchlist.map(w=>`<div><span>${esc(w.symbol)} <span class="mut">${esc(w.name||'')}</span></span><span>${n(w.price,1)}</span></div>`).join('')||'<div class="empty">未登録</div>';
}
$('btn').onclick=async()=>{
  if(!halted&&!confirm('新規の買いを停止します。保有中のポジションの損切り・決済は継続します。'))return;
  await fetch(halted?'/api/resume':'/api/halt',{method:'POST'});load();
};
load();setInterval(load,2000);
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
