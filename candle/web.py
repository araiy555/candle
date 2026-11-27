"""管理画面。標準ライブラリのみで、このPC（127.0.0.1）からだけ閲覧できる。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
import threading

from .bars import JST
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
button{font:inherit;white-space:nowrap;padding:6px 14px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.stop{background:#c0392b;border-color:#c0392b;color:#fff;font-weight:600}
.kpis{display:flex;flex-wrap:wrap;gap:20px}.kpis>div>span{display:block;color:var(--mut);font-size:12px}.kpis b{font-size:18px;white-space:nowrap}
.watch{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:6px}
.watch div{border:1px solid var(--line);border-radius:6px;padding:4px 8px;display:flex;justify-content:space-between}
.empty{color:var(--mut);padding:8px 0}
.link{cursor:pointer;color:var(--acc);text-decoration:underline;text-underline-offset:2px;background:none;border:0;padding:0;font:inherit}
.watch button.w{all:unset;cursor:pointer;border:1px solid var(--line);border-radius:6px;padding:4px 8px;display:flex;justify-content:space-between;gap:8px}
.watch button.w:hover,.watch button.w:focus-visible{border-color:var(--acc)}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--fg);padding:0;width:min(1100px,96vw)}
dialog::backdrop{background:rgba(0,0,0,.45)}
.dh{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}
.dh b{font-size:16px;margin-right:8px}.dh a{color:var(--acc);font-size:13px}
.chart{position:relative;padding:8px 8px 12px}.chart svg{display:block;width:100%;height:460px}
.tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12px;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.15);display:none;font-variant-numeric:tabular-nums}
.note{color:var(--mut);font-size:12px;padding:0 16px 12px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;border-radius:0;border-right:1px solid var(--line);padding:6px 16px}.seg button:last-child{border-right:0}
.seg button.on{background:var(--acc);color:#fff;font-weight:600}.seg button.on[data-mode=live]{background:#c0392b}
.live{background:#c0392b;border-color:#c0392b;color:#fff;font-weight:600}
#warn{border-color:var(--warn)}#warn li{margin:2px 0}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px 20px;align-items:end}
.form label{display:block;font-size:12px;color:var(--mut);margin-bottom:4px}
.form .row{display:flex;gap:6px}.form select,.form input{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);min-width:0}
.form select{flex:1}.form input{width:110px;text-align:right}.form input:disabled{opacity:.4}
[hidden]{display:none!important}
</style></head><body>
<header><h1>candle</h1><span class="pill" id="mode"></span><span class="seg" id="modes" hidden><button data-mode="watch">通知のみ</button><button data-mode="paper">テスト</button><button data-mode="live">本番</button></span><span class="pill" id="feed"></span>
<span class="pill" id="halt"></span><span class="mut" id="time"></span>
<a href="/squeeze" style="color:var(--acc);font-size:13px">検知履歴（サインの後の値動き）</a>
<a href="/analysis" style="color:var(--acc);font-size:13px">分析（取引・通知）</a>
<label class="pill" style="cursor:pointer"><input type="checkbox" id="auto" style="vertical-align:-2px"> サインが出たらkabuステーションのチャートを自動で切り替える</label>
<span style="flex:1"></span><button id="closeall" hidden>全ポジションを決済</button><button id="btn"></button></header>
<main>
<section id="health" style="padding:8px 12px;font-size:13px"></section>
<section id="market"></section>
<section id="warn" hidden><h2>確認が必要です</h2><ul id="warnlist" style="margin:0;padding-left:20px"></ul></section>
<section data-trade><div class="kpis" id="kpis"></div></section>
<section><h2>設定</h2><div class="form">
<div><label for="stop_mode">損切りライン</label><div class="row"><select id="stop_mode"><option value="pattern_low">パターンの安値</option><option value="percent">買値から一定率（%）下</option><option value="yen">買値から一定額（円）下</option></select><input id="stop_value" type="number" step="0.1" min="0"></div></div>
<div><label for="trail_mode">トレーリングストップ</label><div class="row"><select id="trail_mode"><option value="r_step">リスク幅（R）ごとに引き上げ</option><option value="percent">高値から一定率（%）下に追従</option><option value="yen">高値から一定額（円）下に追従</option><option value="none">トレーリングなし</option></select><input id="trail_value" type="number" step="0.1" min="0"></div></div>
<div><label for="size_mode">買いの上限の決め方</label><div class="row"><select id="size_mode"><option value="yen">金額で指定</option><option value="percent">信用の買付余力に対する割合で指定</option></select></div></div>
<div><label for="per_trade_pct">割合で指定：1回の買い（余力の%）</label><div class="row"><input id="per_trade_pct" type="number" step="1" min="1" max="100" style="width:100%"></div></div>
<div><label for="total_pct">割合で指定：同時に持つ合計（余力の%）　100＝余力いっぱい</label><div class="row"><input id="total_pct" type="number" step="5" min="1" max="100" style="width:100%"></div></div>
<div><label for="per_trade_cap">1回の買いの上限（円）</label><div class="row"><input id="per_trade_cap" type="number" step="10000" min="0" style="width:100%"></div></div>
<div><label for="total_cap">同時に持つ合計の上限（円）</label><div class="row"><input id="total_cap" type="number" step="10000" min="0" style="width:100%"></div></div>
<div><label for="daily_max_loss">1日の最大損失（円）</label><div class="row"><input id="daily_max_loss" type="number" step="1000" min="0" style="width:100%"></div></div>
<div><label for="quiet_open_min">寄り付き後に待つ時間（分）　この間は通知も売買もしない</label><div class="row"><input id="quiet_open_min" type="number" step="1" min="0" style="width:100%"></div></div>
<div><label for="min_price">株価の下限（円）　これより安い銘柄は通知も売買もしない。0＝除かない</label><div class="row"><input id="min_price" type="number" step="50" min="0" style="width:100%"></div></div>
<div><label for="min_range_pct">足の平均値幅率の下限（%）　これ未満は値動きが小さい（レンジ）として通知も売買もしない。0＝除かない</label><div class="row"><input id="min_range_pct" type="number" step="0.01" min="0" style="width:100%"></div></div>
<div><label for="vwap_rule">VWAPの条件（売買のみ。通知はそのまま）</label><div class="row"><select id="vwap_rule" style="width:100%"><option value="above">VWAPより下では買わない</option><option value="off">VWAPを見ない</option></select></div></div>
<div><label for="min_drop_pct">直前の下落率の下限（%）　これ未満の下げは毛抜き底と見なさない。0＝除かない</label><div class="row"><input id="min_drop_pct" type="number" step="0.05" min="0" style="width:100%"></div></div>
<div><label for="mkt_fut_drop">地合い：先物が5分でこの%以上下げたら「警戒」</label><div class="row"><input id="mkt_fut_drop" type="number" step="0.05" min="0" style="width:100%"></div></div>
<div><label for="mkt_topix_drop">地合い：TOPIXも5分でこの%以上下げたら「警戒強化」</label><div class="row"><input id="mkt_topix_drop" type="number" step="0.05" min="0" style="width:100%"></div></div>
<div><label for="mkt_breadth">地合い：監視銘柄のこの%以上が下落で新規買い停止　0＝止めない</label><div class="row"><input id="mkt_breadth" type="number" step="5" min="0" max="100" style="width:100%"></div></div>
<div><label for="max_hold_min">最大保有時間（分）　0＝時間では決済しない</label><div class="row"><input id="max_hold_min" type="number" step="1" min="0" style="width:100%"></div></div>
<div style="grid-column:1/-1"><label>通知するサイン</label><div><label style="display:inline-flex;gap:4px;align-items:center;margin:0 12px 4px 0;font-size:13px;color:inherit"><input type="checkbox" class="nt" style="width:auto;flex:none;margin:0" value="tweezer_bottom">毛抜き底</label><label style="display:inline-flex;gap:4px;align-items:center;margin:0 12px 4px 0;font-size:13px;color:inherit"><input type="checkbox" class="nt" style="width:auto;flex:none;margin:0" value="squeeze">スクイーズ</label><label style="display:inline-flex;gap:4px;align-items:center;margin:0 12px 4px 0;font-size:13px;color:inherit"><input type="checkbox" class="nt" style="width:auto;flex:none;margin:0" value="fvg_hold">FVG反発</label><label style="display:inline-flex;gap:4px;align-items:center;margin:0 12px 4px 0;font-size:13px;color:inherit"><input type="checkbox" class="nt" style="width:auto;flex:none;margin:0" value="fvg_break">FVG割れ</label><label style="display:inline-flex;gap:4px;align-items:center;margin:0 12px 4px 0;font-size:13px;color:inherit"><input type="checkbox" class="nt" style="width:auto;flex:none;margin:0" value="wall_break">売り板突破</label><label style="display:inline-flex;gap:4px;align-items:center;margin:0 12px 4px 0;font-size:13px;color:inherit"><input type="checkbox" class="nt" style="width:auto;flex:none;margin:0" value="liq_sweep">流動性スイープ</label><label style="display:inline-flex;gap:4px;align-items:center;margin:0 12px 4px 0;font-size:13px;color:inherit"><input type="checkbox" class="nt" style="width:auto;flex:none;margin:0" value="open_dip">寄り付き押し目（9〜10時）</label><label style="display:inline-flex;gap:4px;align-items:center;margin:0 12px 4px 0;font-size:13px;color:inherit"><input type="checkbox" class="nt" style="width:auto;flex:none;margin:0" value="bos_dip">内部流動性ブレイク</label></div><input id="notify_on" type="hidden"></div>
<div><div class="row" style="align-items:center"><button id="save">設定を保存</button><span class="mut" id="savemsg" style="font-size:12px"></span></div></div>
</div><div id="example" style="margin-top:12px;font-size:13px"></div><div class="note" style="padding:10px 0 0">損切り・トレーリング・金額の上限は、テストと本番の売買に使います（次の買いから適用。保有中の分は買った時点の設定のまま）。寄り付き後に待つ時間は、通知のみのモードでも効きます。</div></section>
<section data-trade><h2>保有ポジション</h2><div id="positions"></div></section>
<section data-trade><h2>戦略別成績</h2><div id="stats"></div></section>
<section><h2>検出されたサイン</h2><div id="signals"></div></section>
<section data-trade><h2>取引履歴</h2><div id="history"></div></section>
<section><h2>監視銘柄</h2><div class="note" style="padding:0 0 8px">5分・15分の矢印は上位足の向きです（↑上昇　↓下降　→レンジ　?まだ判定できない）。</div><div class="watch" id="watch"></div></section>
</main>
<dialog id="dlg"><div class="dh"><b id="dt"></b><button id="kabu">kabuステーションで表示</button><span class="mut" id="kmsg" style="font-size:12px"></span><button id="copy">銘柄コードをコピー</button>
<a id="l1" target="_blank" rel="noopener">Yahoo!ファイナンス</a><a id="l2" target="_blank" rel="noopener">TradingView</a><a id="l3" target="_blank" rel="noopener">株探</a>
<span style="flex:1"></span><button id="close">閉じる</button></div>
<div class="kpis" id="qinfo" style="padding:10px 16px 0;gap:18px"></div>
<div id="mtf" style="padding:10px 16px 0;font-size:13px;display:flex;gap:8px;align-items:center;flex-wrap:wrap"></div>
<div style="padding:6px 16px 0;font-size:12px" class="mut"><span style="display:inline-block;width:22px;border-top:2px solid var(--acc);vertical-align:middle;margin-right:5px"></span>VWAP（受信した値。受信前の時間帯は線がありません）</div>
<div style="padding:4px 16px 0;font-size:12px;display:flex;flex-wrap:wrap;gap:6px 16px" class="mut"><span><span style="display:inline-block;width:14px;height:10px;border:2px solid var(--up);vertical-align:middle;margin-right:5px"></span>直近の上昇の範囲（中央の線より上が割高ゾーン、下が割安ゾーン）</span><span><span style="display:inline-block;width:18px;height:8px;background:var(--warn);opacity:.75;vertical-align:middle;margin-right:5px"></span>上昇中の出来高の分布</span><span><span style="display:inline-block;width:18px;border-top:2px dashed var(--fg);vertical-align:middle;margin-right:5px"></span>上昇中のPOC</span><span><span style="display:inline-block;width:18px;height:8px;border:1.5px solid var(--warn);vertical-align:middle;margin-right:5px"></span>FVG</span><span><span style="display:inline-block;width:18px;border-top:2px solid #d4a700;vertical-align:middle;margin-right:5px"></span>上昇前のレンジのPOC</span><span><span style="display:inline-block;width:18px;height:0;border-top:3px solid var(--up);vertical-align:middle;margin-right:5px"></span>外部流動性（当日の高値・安値、大きな波の高値・安値）</span><span><span style="display:inline-block;width:18px;height:0;border-top:1px dashed var(--warn);vertical-align:middle;margin-right:5px"></span>内部流動性（その内側の小さな波。まだ抜かれていないもの）</span></div>
<div class="chart"><svg id="svg" role="img"></svg><div class="tip" id="tip"></div></div>
<div id="tinfo" style="padding:8px 16px 0;font-size:13px"></div><div class="note" id="note"></div></dialog>
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
let halted=false,state=null,cur=null,formLoaded=false;let curDay=null,curTrade=null,curTf=0;const dayOf=t=>new Date(t).toLocaleDateString('sv-SE',{timeZone:'Asia/Tokyo'});const closing=new Set();
const FIELDS=['stop_mode','stop_value','trail_mode','trail_value','size_mode','per_trade_pct','total_pct','per_trade_cap','total_cap','daily_max_loss','max_hold_min','quiet_open_min','min_price','min_range_pct','vwap_rule','min_drop_pct','mkt_fut_drop','mkt_topix_drop','mkt_breadth','notify_on'];
function vdiff(price,vwap){if(price==null||!vwap)return '<span class="mut">-</span>';const d=(price-vwap)/vwap*100;return `<span class="${d>0?'pos':d<0?'neg':''}">${d>0?'+':''}${n(d,2)}%</span>`}
function vcell(price,vwap){return vwap?`${n(vwap,1)} <span style="font-size:12px">(${vdiff(price,vwap)})</span>`:'<span class="mut">-</span>'}
const rng=v=>v==null?'<span class="mut">-</span>':n(v,2)+'%';
function nextRaise(p){if(p.next_raise)return `${n(p.next_raise[0],1)} で ${n(p.next_raise[1],1)} へ`;
  if(p.trail_mode==='percent')return `<span class="mut">高値の${n(p.trail_value,2)}%下</span>`;if(p.trail_mode==='yen')return `<span class="mut">高値の${n(p.trail_value,1)}円下</span>`;return '<span class="mut">なし</span>'}
function example(){
  const E=1000,LOW=995,Q=100,f=v=>n(v,1),sm=$('stop_mode').value,sv=parseFloat($('stop_value').value)||0,tm=$('trail_mode').value,tv=parseFloat($('trail_value').value)||0;
  const stop=sm==='percent'?E*(1-sv/100):sm==='yen'?E-sv:LOW,R=E-stop;
  if(!(R>0)){$('example').innerHTML='<span class="ng">損切りラインが買値以上になっています。数値を見直してください。</span>';return}
  const line=h=>{let s=stop;if(tm==='r_step'){const k=Math.floor((h-E)/R+1e-9);if(k>=1)s=Math.max(s,E+(k-1)*R)}else if(tm==='percent'&&tv>0)s=Math.max(s,h*(1-tv/100));else if(tm==='yen'&&tv>0)s=Math.max(s,h-tv);return s};
  const highs=[E,E+R,E+2*R,E+3*R,E+5*R];
  $('example').innerHTML=`<b>数字での例</b>（${f(E)}円で${Q}株買い、パターンの安値が${f(LOW)}円の場合）<br>`+
    `最初の損切りライン：<b>${f(stop)}円</b>（買値から ${f(-R)}円、ここで決済すると <span class="neg">${n(-R*Q)}円</span>）`+
    '<table style="margin-top:6px;width:auto"><thead><tr><th>買った後の高値</th><th>損切りライン</th><th>そこで決済した場合の損益</th></tr></thead><tbody>'+
    highs.map(h=>{const s=line(h),pl=(s-E)*Q;return `<tr><td>${f(h)}円</td><td>${f(s)}円</td><td>${pl>0?'<span class="pos">+'+n(pl)+'円</span>':pl<0?'<span class="neg">'+n(pl)+'円</span>':'0円'}</td></tr>`}).join('')+'</tbody></table>'}
function syncForm(){example();const pc=$('size_mode').value==='percent';$('per_trade_pct').disabled=$('total_pct').disabled=!pc;$('per_trade_cap').disabled=$('total_cap').disabled=pc;$('stop_value').disabled=$('stop_mode').value==='pattern_low';$('trail_value').disabled=!['percent','yen'].includes($('trail_mode').value)}
async function load(){
  let d;try{d=await (await fetch('/api/state')).json()}catch(e){$('feed').innerHTML='<span class="ng">プログラム停止中</span>';return}
  if(!d.time){$('time').textContent='起動中…';return}
  halted=d.halted;state=d;$('auto').checked=!!d.auto_chart;
  document.querySelectorAll('[data-trade]').forEach(e=>e.hidden=!d.trading);$('closeall').hidden=!d.trading;
  $('mode').className='pill'+(d.live&&d.trading?' live':'');
  $('modes').hidden=!d.can_switch;$('mode').hidden=!!d.can_switch;
  document.querySelectorAll('#modes button').forEach(b=>{b.className=b.dataset.mode===d.mode_key?'on':'';b.disabled=!!d.switching});
  const h=d.health;if(h){const stale=h.market_open&&(h.tick_age==null||h.tick_age>30);
    $('health').innerHTML=(stale?'<b class="ng">株価を受信できていません</b>　':h.market_open?'<b class="ok">動作中</b>　':'<b class="mut">取引時間外</b>　')+
      `最後の受信 ${h.last_tick?hm(h.last_tick):'なし'}　／　最後に確定した足 ${h.last_bar?hm(h.last_bar):'なし'}　／　本日の足 ${n(h.bars_today)}本　／　判定できる銘柄 ${h.ready} / ${h.watching}　／　本日のサイン ${h.signals_today}件`}
  const mk=d.market;if(mk){const cls={normal:'ok',caution:'warn',strong:'warn',halt:'ng'}[mk.level];
    const ix=(v,name,lim)=>`<div><span>${esc(v.label||name)}（${n(mk.window_min,0)}分前から）</span><b>${v.available?`<span class="${v.change>0?'pos':v.change<0?'neg':''}">${v.change>0?'+':''}${n(v.change,2)}%</span>`:'<span class="mut" style="font-size:13px">取得できていません</span>'}</b><span>${v.available?(v.falling?`<span class="warn">基準（-${lim}%）以上の下げ</span>`:`基準 -${lim}%`):esc(v.reason||'')}</span></div>`;
    const b=mk.breadth;
    $('market').innerHTML=`<h2>地合い　<span class="${cls}" style="font-size:15px">${esc(mk.label)}</span>${mk.held?' <span class="mut" style="font-weight:400;font-size:12px">（条件は外れましたが、5分間は停止を続けます）</span>':''}</h2><div class="kpis">`+
      ix(mk.fut,'日経225先物',mk.thresholds.fut_drop)+ix(mk.topix,'TOPIX',mk.thresholds.topix_drop)+
      `<div><span>下落している監視銘柄（${n(mk.window_min,0)}分前より安い）</span><b>${b.available?`<span class="${b.falling?'ng':''}">${n(b.pct,0)}%</span> <span class="mut" style="font-size:13px">（${b.down} / ${b.total}銘柄）</span>`:'<span class="mut" style="font-size:13px">判定できません</span>'}</b><span>${b.available?(mk.thresholds.breadth>100?'停止の基準なし':`基準 ${n(mk.thresholds.breadth,0)}%以上で新規買い停止`):esc(b.reason||'')}</span></div></div>`+
      '<div class="note" style="padding:8px 0 0">先物が急落＝警戒、加えてTOPIXも下落＝警戒強化、監視銘柄の下落が基準以上＝新規買い停止。買いを止めるのは「新規買い停止」のときだけです。保有中の損切り・決済は通常どおり行います。</div>'}
  $('warn').hidden=!(d.warnings&&d.warnings.length);$('warnlist').innerHTML=(d.warnings||[]).map(w=>`<li>${esc(w)}</li>`).join('');
  if(!formLoaded&&d.settings){for(const k of FIELDS)$(k).value=d.settings[k];const on=(d.settings.notify_on||'').split(',');document.querySelectorAll('.nt').forEach(c=>c.checked=on.includes(c.value));formLoaded=true;syncForm()}
  $('mode').textContent=d.mode;$('time').textContent=md(d.time)+' 更新';
  $('feed').innerHTML=d.feed_ok?'<span class="ok">API接続中</span>':'<span class="ng">API切断（新規停止）</span>';
  $('halt').innerHTML=d.halted?'<span class="ng">緊急停止中</span>':'<span class="ok">新規受付中</span>';
  $('btn').textContent=d.halted?'新規受付を再開':'緊急停止';$('btn').className=d.halted?'':'stop';
  const unreal=d.positions.reduce((a,p)=>a+(p.pnl||0),0);
  $('kpis').innerHTML=[['含み損益',yen(unreal)+' 円'],['本日の確定損益',yen(d.realized_today)+' 円'],
    ['投資額 / 上限',n(d.exposure)+' / '+n(d.limits.total_cap)+' 円'],['1回の買いの上限',n(d.limits.per_symbol_cap)+' 円'],['信用の買付余力',d.buying_power==null?`<span class="mut" style="font-size:13px">${esc(d.buying_power_note||'未取得')}</span>`:n(d.buying_power)+' 円'],['1日の最大損失',n(d.limits.daily_max_loss)+' 円'],
    ['保有数',d.positions.length]].map(([k,v])=>`<div><span>${k}</span><b>${v}</b></div>`).join('');
  table('positions',['戦略','銘柄','状態','株数','サイン時刻','エントリー','現在値','VWAP（現在値との差）','平均値幅率','初期損切り','現在の損切り','次の引き上げ','含み損益','R','保有時間',''],
    d.positions.map(p=>[esc(p.strategy),`<button class="link" data-sym="${esc(p.symbol)}">${esc(p.symbol)} ${esc(p.name||'')}</button>`,esc(p.state),n(p.qty),hm(p.signal_time),n(p.entry_price,1),n(p.price,1),vcell(p.price,p.vwap),rng(p.range_pct),n(p.initial_stop,1),
      n(p.stop,1),nextRaise(p),yen(p.pnl),rr(p.r),n(p.hold_min)+'分',closing.has(p.id)||p.state==='決済中'?'<span class="mut">決済中…</span>':`<button data-close="${p.id}" data-label="${esc(p.symbol)} ${esc(p.name||'')} ${n(p.qty)}株" style="padding:2px 12px">決済</button>`]),'保有ポジションはありません');
  for(const id of [...closing])if(!d.positions.some(p=>p.id===id))closing.delete(id);
  table('stats',['戦略','取引数','勝率','PF','期待損益(円)','期待値(R)','合計損益(円)','平均保有'],
    d.stats.map(s=>[esc(s.strategy),s.trades,s.win_rate==null?'-':n(s.win_rate*100,1)+'%',s.pf==null?'-':n(s.pf,2),
      yen(s.expectancy),rr(s.expectancy_r),yen(s.total),s.avg_hold_min==null?'-':n(s.avg_hold_min)+'分']),'');
  table('signals',['戦略','銘柄','発生時刻','価格','VWAP比','平均値幅率','直前の下落率','安値','内容','結果'],
    d.signals.map(s=>[esc(s.strategy),`<button class="link" data-sym="${esc(s.symbol)}">${esc(s.symbol)} ${esc(s.name||'')}</button> <button data-kabu="${esc(s.symbol)}" style="padding:1px 8px;font-size:12px">kabuステで表示</button>`,md(s.time),n(s.price,1),vdiff(s.price,s.vwap),rng(s.range_pct),s.drop_pct==null?'<span class="mut">-</span>':'-'+n(s.drop_pct,2)+'%',n(s.pattern_low,1),`<span class="l mut" style="white-space:normal;display:inline-block;max-width:420px;text-align:left">${esc(s.note||'')}</span>`,
      s.action==='entry'?'エントリー':s.action==='通知のみ'?'通知':'<span class="mut">見送り: '+esc(s.action)+'</span>']),'まだサインはありません');
  table('history',['戦略','銘柄','株数','買い','買値','決済','売値','理由','損益(円)','R','平均値幅率','損切り幅÷値幅'],
    d.history.map(t=>[esc(t.strategy),`<button class="link" data-tid="${t.id}" title="チャートで売買の位置を見る">${esc(t.symbol)} ${esc(t.name||'')}</button>`,n(t.qty),md(t.entry_time),n(t.entry_price,1),md(t.exit_time),n(t.exit_price,1),
      esc(t.reason),yen(t.pnl),rr(t.r),rng(t.range_pct),t.range_pct?n(t.stop_pct/t.range_pct,1)+'倍':'<span class="mut">-</span>']),'決済済みの取引はありません');
  $('watch').innerHTML=d.watchlist.map(w=>`<button class="w" data-sym="${esc(w.symbol)}"${w.excluded||w.ranging?` style="opacity:.45" title="${w.excluded?'株価が下限より安いため対象外':'値動きが小さい（レンジ）ため見送り中'}"`:''}><span>${w.excluded?'<span class="mut">対象外 </span>':w.ranging?'<span class="mut">レンジ </span>':''}${esc(w.symbol)} <span class="mut">${esc(w.name||'')}</span></span><span style="text-align:right">${n(w.price,1)}<br><span style="font-size:11px" class="mut">${(w.frames||[]).map(f=>`${f.minutes}分<b class="${f.trend==='up'?'pos':f.trend==='down'?'neg':'mut'}">${f.trend==='up'?'↑':f.trend==='down'?'↓':f.trend==='range'?'→':'?'}</b>`).join(' ')}　VWAP比 ${vdiff(w.price,w.vwap)}　値幅 ${rng(w.range_pct)}</span></span></button>`).join('')||'<div class="empty">未登録</div>';
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
  const W=svg.clientWidth||900,H=460,L=8,R=64,T=14,VH=84,VG=22,B=26+VH+VG;svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  if(!bars.length){svg.appendChild(el('text',{x:W/2,y:H/2,'text-anchor':'middle',fill:'var(--mut)','font-size':13},'まだ足がありません（起動後に確定した足から表示します）'));return}
  const bday=dayOf(bars[0].t),marks=(state?.signals||[]).filter(s=>s.symbol===data.symbol&&dayOf(s.time)===bday);
  // この銘柄のその日の売買（決済済みと保有中）。どこで買ってどこで売ったかを重ねて描く
  const trs=[...(state?.history||[]),...(state?.positions||[])].filter(t=>t.symbol===data.symbol&&t.entry_time&&dayOf(t.entry_time)===bday);
  const selT=trs.find(t=>t.id===curTrade);
  const vw=bars.map(b=>b.vwap).filter(v=>v);
  let lo=Math.min(...bars.map(b=>b.l),...marks.map(m=>m.pattern_low),...vw,...(selT?[selT.initial_stop]:[])),hi=Math.max(...bars.map(b=>b.h),...vw);
  const pad=(hi-lo)*0.08||hi*0.002;lo-=pad*1.6;hi+=pad;
  const n0=Math.max(bars.length,30),bw=(W-L-R)/n0,x=i=>L+bw*(i+.5),y=v=>T+(hi-v)/(hi-lo)*(H-T-B);
  const dec=hi<1000?1:0,fmt=v=>v.toLocaleString('ja-JP',{minimumFractionDigits:dec,maximumFractionDigits:dec});
  for(const v of ticks(lo,hi,5)){svg.appendChild(el('line',{x1:L,x2:W-R,y1:y(v),y2:y(v),stroke:'var(--line)','stroke-width':1}));
    svg.appendChild(el('text',{x:W-R+6,y:y(v)+4,fill:'var(--mut)','font-size':11},fmt(v)))}
  const every=Math.max(1,Math.ceil(70/bw));
  bars.forEach((b,i)=>{if(i%every===0)svg.appendChild(el('text',{x:Math.max(x(i),L+16),y:H-8,'text-anchor':'middle',fill:'var(--mut)','font-size':11},
    new Date(b.t).toLocaleTimeString('ja-JP',{timeZone:'Asia/Tokyo',hour:'2-digit',minute:'2-digit'})))});
  // 直近の上昇の範囲・割高/割安ゾーン・出来高の分布・FVG（ローソク足の下に描く）
  const st=data.structure;
  if(st){const ix=t=>bars.findIndex(b=>b.t===t),i0=ix(st.start),i1=ix(st.end);
    if(i0>=0&&i1>i0){const xl=x(i0)-bw/2,xr=x(i1)+bw/2,yt=y(st.high),yb=y(st.low),ym=y(st.mid),right=W-R;
      svg.appendChild(el('rect',{x:xl,y:yt,width:xr-xl,height:yb-yt,fill:'var(--acc)',opacity:.07}));
      const pmax=Math.max(...st.profile.map(q=>q.vol),1),pw=Math.max(40,(xr-xl)*0.4),ph=Math.max(2,(yb-yt)/Math.max(st.profile.length,1)-1);
      for(const q of st.profile){if(q.vol>0)svg.appendChild(el('rect',{x:xl+2,y:y(q.price)-ph/2,width:Math.max(1,q.vol/pmax*pw),height:ph,fill:'var(--warn)',opacity:.75}))}
      for(const f of st.fvgs){const fi=ix(f.t);if(fi<1)continue;const bi=f.broken?ix(f.broken):-1,fx=x(fi-1)-bw/2,fr=bi>=0?x(bi)+bw/2:right;
        svg.appendChild(el('rect',{x:fx,y:y(f.top),width:Math.max(2,fr-fx),height:Math.max(2,y(f.bottom)-y(f.top)),fill:'var(--warn)','fill-opacity':f.discount&&!f.broken?.18:.05,stroke:'var(--warn)','stroke-width':f.discount?1.5:1,'stroke-opacity':f.discount&&!f.broken?1:.45,'stroke-dasharray':f.broken?'4 3':'none'}));
        if(f.discount)svg.appendChild(el('text',{x:Math.min(fr,right)-4,y:y(f.top)-3,'text-anchor':'end',fill:'var(--warn)','font-size':11},f.broken?'FVG（割れ）':'FVG'))}
      svg.appendChild(el('rect',{x:xl,y:yt,width:xr-xl,height:yb-yt,fill:'none',stroke:'var(--up)','stroke-width':2}));
      svg.appendChild(el('line',{x1:xl,x2:xr,y1:ym,y2:ym,stroke:'var(--up)','stroke-width':2}));
      const zone=(txt,yy)=>{const t=el('text',{x:xl+pw*0.35+8,y:yy,fill:'var(--fg)','font-size':12,'font-weight':700,stroke:'var(--card)','stroke-width':4,'paint-order':'stroke'},txt);svg.appendChild(t)};
      if(yb-yt>46){zone('割高ゾーン',(yt+ym)/2+4);zone('割安ゾーン',(ym+yb)/2+4)}
      if(st.leg_poc!=null){svg.appendChild(el('line',{x1:xl,x2:xr,y1:y(st.leg_poc),y2:y(st.leg_poc),stroke:'var(--fg)','stroke-width':1.5,'stroke-dasharray':'7 5'}))}
      if(st.range_poc!=null&&st.range_poc>lo&&st.range_poc<hi){svg.appendChild(el('line',{x1:L,x2:right,y1:y(st.range_poc),y2:y(st.range_poc),stroke:'#d4a700','stroke-width':1.5}));
        svg.appendChild(el('text',{x:L+4,y:y(st.range_poc)+14,fill:'var(--fg)','font-size':11,stroke:'var(--card)','stroke-width':4,'paint-order':'stroke'},'上昇前のレンジのPOC '+fmt(st.range_poc)))}}}
  // 流動性（まだ抜かれていない高値・安値。その先に損切りがたまりやすい）。外部＝大きな波・当日高安、内部＝その内側の小さな波
  for(const q of (data.liquidity||[])){const pi=bars.findIndex(b=>b.t===q.t);if(pi<0)continue;const ext=q.kind==='external',up=q.side==='high',py=y(q.level),th=ext?9:5,col=ext?'var(--up)':'var(--warn)',x0=x(pi)-bw/2;
    svg.appendChild(el('rect',{x:x0,y:up?py-th:py,width:(W-R)-x0,height:th,fill:col,'fill-opacity':ext?.22:.12}));
    svg.appendChild(el('line',{x1:x0,x2:W-R,y1:py,y2:py,stroke:col,'stroke-width':ext?2.5:1,'stroke-dasharray':ext?'none':'4 3'}));
    svg.appendChild(el('text',{x:(W-R)-4,y:up?py-th-3:py+th+11,'text-anchor':'end',fill:col,'font-size':ext?12:11,'font-weight':ext?700:400,stroke:'var(--card)','stroke-width':3,'paint-order':'stroke'},(ext?'外部流動性':'内部流動性')+(up?'（上）':'（下）')+' '+fmt(q.level)))}
  // 前日の高値・安値（上位足の外部流動性）。今の表示範囲に入るときだけ線を引く
  if(data.prev_day)for(const [v,label] of [[data.prev_day.high,'前日高値'],[data.prev_day.low,'前日安値']]){if(v==null||v<=lo||v>=hi)continue;
    svg.appendChild(el('line',{x1:L,x2:W-R,y1:y(v),y2:y(v),stroke:'var(--fg)','stroke-width':2,'stroke-dasharray':'10 5'}));
    svg.appendChild(el('text',{x:L+4,y:y(v)-4,fill:'var(--fg)','font-size':12,'font-weight':700,stroke:'var(--card)','stroke-width':3,'paint-order':'stroke'},label+'（上位足の外部流動性） '+fmt(v)))}
  const w=Math.max(1,Math.min(14,bw*0.7));
  // 出来高（株価とは別の枠。縦軸は共有しない）
  const vTop=H-26-VH,vBot=H-26,vmax=Math.max(...bars.map(b=>b.v),1),vy=v=>vBot-(v/vmax)*VH;
  svg.appendChild(el('line',{x1:L,x2:W-R,y1:vBot,y2:vBot,stroke:'var(--line)','stroke-width':1}));
  svg.appendChild(el('text',{x:L,y:vTop-6,fill:'var(--mut)','font-size':11},'出来高（1本ごと）'));
  svg.appendChild(el('text',{x:W-R+6,y:vTop+4,fill:'var(--mut)','font-size':11},vmax.toLocaleString('ja-JP')));
  svg.appendChild(el('text',{x:W-R+6,y:vBot+4,fill:'var(--mut)','font-size':11},'0'));
  bars.forEach((b,i)=>{if(!(b.v>0))return;const col=b.c>b.o?'var(--up)':b.c<b.o?'var(--down)':'var(--mut)';
    svg.appendChild(el('rect',{x:x(i)-w/2,y:vy(b.v),width:w,height:Math.max(1,vBot-vy(b.v)),fill:col,opacity:b.ok?.75:.3}))});
  bars.forEach((b,i)=>{const col=b.c>b.o?'var(--up)':b.c<b.o?'var(--down)':'var(--mut)',g=el('g',{opacity:b.ok?1:.35});
    g.appendChild(el('line',{x1:x(i),x2:x(i),y1:y(b.h),y2:y(b.l),stroke:col,'stroke-width':1}));
    const top=y(Math.max(b.o,b.c)),h=Math.max(1,y(Math.min(b.o,b.c))-top);
    g.appendChild(el('rect',{x:x(i)-w/2,y:top,width:w,height:h,fill:col}));svg.appendChild(g)});
  let vd='',pen=false;bars.forEach((b,i)=>{if(!b.vwap){pen=false;return}vd+=(pen?'L':'M')+x(i)+','+y(b.vwap);pen=true});
  if(vd)svg.appendChild(el('path',{d:vd,fill:'none',stroke:'var(--acc)','stroke-width':2}));
  const ms=(data.minutes||5)*60000;
  for(const m of marks){const mt=Date.parse(m.time);let i=bars.findIndex(b=>Date.parse(b.t)+ms===mt);if(i<0)i=bars.findIndex(b=>mt>=Date.parse(b.t)&&mt<Date.parse(b.t)+ms);if(i<0&&bars.length&&mt>=Date.parse(bars[bars.length-1].t)+ms)i=bars.length-1;if(i<0)continue;
    const py=y(m.pattern_low);
    svg.appendChild(el('line',{x1:x(i)-bw,x2:W-R,y1:py,y2:py,stroke:'var(--fg)','stroke-width':1,'stroke-dasharray':'4 3',opacity:.6}));
    svg.appendChild(el('path',{d:`M${x(i)},${py+6} l6,10 h-12 z`,fill:'var(--fg)'}));
    const right=x(i)+140>W-R;svg.appendChild(el('text',{x:x(i)+(right?-10:10),y:py+17,'text-anchor':right?'end':'start',fill:'var(--fg)','font-size':11},`${m.strategy} 安値 ${fmt(m.pattern_low)}`))}
  const bi=t=>{const v=Date.parse(t);return bars.findIndex(b=>v>=Date.parse(b.t)&&v<Date.parse(b.t)+ms)};
  const halo={stroke:'var(--card)','stroke-width':4,'paint-order':'stroke'};
  for(const t of trs){const i=bi(t.entry_time),j=t.exit_time?bi(t.exit_time):-1;if(i<0)continue;const sel=t===selT,done=t.exit_time&&j>=0;
    const col=done?(t.pnl>0?'var(--up)':'var(--down)'):'var(--acc)',ey=y(t.entry_price);
    if(sel&&t.initial_stop>lo){const sy=y(t.initial_stop),sx=done?x(j)+bw:W-R;svg.appendChild(el('line',{x1:x(i)-bw/2,x2:sx,y1:sy,y2:sy,stroke:'var(--warn)','stroke-width':1.5,'stroke-dasharray':'5 4'}));
      const lf=x(i)>190;svg.appendChild(el('text',{x:lf?x(i)-bw/2-5:x(i)-bw/2,y:lf?sy+4:sy+14,'text-anchor':lf?'end':'start',fill:'var(--warn)','font-size':11,...halo},'最初の損切り '+fmt(t.initial_stop)))}
    if(done)svg.appendChild(el('line',{x1:x(i),x2:x(j),y1:ey,y2:y(t.exit_price),stroke:col,'stroke-width':sel?3.5:2,opacity:sel||!selT?1:.55}));
    svg.appendChild(el('circle',{cx:x(i),cy:ey,r:sel?6:4.5,fill:'var(--acc)',stroke:'var(--card)','stroke-width':2}));
    if(sel||!selT)svg.appendChild(el('text',{x:x(i)-8,y:ey-9,'text-anchor':'end',fill:'var(--acc)','font-size':sel?13:11,'font-weight':700,...halo},'買 '+fmt(t.entry_price)));
    if(done){const xy=y(t.exit_price);svg.appendChild(el('circle',{cx:x(j),cy:xy,r:sel?6:4.5,fill:col,stroke:'var(--card)','stroke-width':2}));
      const rt=x(j)+150>W-R;svg.appendChild(el('text',{x:x(j)+(rt?-9:9),y:xy+(t.pnl>0?-9:17),'text-anchor':rt?'end':'start',fill:col,'font-size':sel?13:11,'font-weight':700,...halo},(sel?'売 '+fmt(t.exit_price)+'　':'')+(t.pnl>0?'+':'')+n(t.pnl)+'円'))}}
  const cross=el('line',{y1:T,y2:H-26,stroke:'var(--mut)','stroke-width':1,opacity:0});svg.appendChild(cross);
  const hit=el('rect',{x:L,y:T,width:W-L-R,height:H-T-26,fill:'transparent'});svg.appendChild(hit);
  hit.onmousemove=e=>{const r=svg.getBoundingClientRect(),px=(e.clientX-r.left)*W/r.width,i=Math.max(0,Math.min(bars.length-1,Math.floor((px-L)/bw))),b=bars[i];
    cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));cross.setAttribute('opacity',.7);
    tip.innerHTML=`<b>${new Date(b.t).toLocaleTimeString('ja-JP',{timeZone:'Asia/Tokyo',hour:'2-digit',minute:'2-digit'})}</b>${b.ok?'':' <span class="mut">（不完全）</span>'}<br>始 ${fmt(b.o)}　高 ${fmt(b.h)}<br>安 ${fmt(b.l)}　終 ${fmt(b.c)}<br>出来高 ${n(b.v)}<br>VWAP ${b.vwap?fmt(b.vwap)+'（終値との差 '+vdiff(b.c,b.vwap)+'）':'データなし'}`;
    tip.style.display='block';const tx=x(i)*r.width/W;tip.style.left=(tx>r.width/2?tx-tip.offsetWidth-4:tx+20)+'px';tip.style.top='20px'};
  hit.onmouseleave=()=>{cross.setAttribute('opacity',0);tip.style.display='none'};
}
async function chart(){if(!cur)return;let d;try{d=await (await fetch('/api/bars?symbol='+encodeURIComponent(cur)+(curDay?'&day='+curDay:'')+(curTf?'&tf='+curTf:''))).json()}catch(e){return}
  if(d.symbol!==cur)return;$('dt').textContent=`${d.symbol} ${d.name||''}`;
  const q=d.quote||{},m=v=>v==null?'<span class="mut">-</span>':null;
  $('qinfo').innerHTML=[['現在値',m(q.CurrentPrice)||n(q.CurrentPrice,1)+'円'],['前日比',m(q.ChangePreviousClose)||`<span class="${q.ChangePreviousClose>0?'pos':q.ChangePreviousClose<0?'neg':''}">${q.ChangePreviousClose>0?'+':''}${n(q.ChangePreviousClose,1)}円（${q.ChangePreviousClosePer>0?'+':''}${n(q.ChangePreviousClosePer,2)}%）</span>`],
    ['VWAP',m(q.VWAP)||n(q.VWAP,1)+'円'],['VWAPとの差',vdiff(q.CurrentPrice,q.VWAP)],['始値',m(q.OpeningPrice)||n(q.OpeningPrice,1)],['高値',m(q.HighPrice)||n(q.HighPrice,1)],['安値',m(q.LowPrice)||n(q.LowPrice,1)],
    ['足の平均値幅率',rng(d.range_pct)],['当日の値幅率',rng(d.day_range_pct)],['出来高',m(q.TradingVolume)||n(q.TradingVolume)+'株'],['売買代金',m(q.TradingValue)||n(q.TradingValue/1e8,1)+'億円']].map(([k,v])=>`<div><span>${k}</span><b style="font-size:15px">${v}</b></div>`).join('');
  $('note').textContent=`${d.minutes||''}分足・当日分（このプログラムが起動中に受信した足）。赤=陽線、緑=陰線、薄い足=データ欠落を含む足。▲と点線はサインとパターン安値。下の棒は1本ごとの出来高。足の平均値幅率は、直近30本の（高値−安値）÷終値の平均。`;
  const TC={up:'pos',down:'neg'},fr=d.frames||[],pd=d.prev_day;
  $('mtf').innerHTML=(fr.length?'<span class="mut">足の切り替え</span>'+fr.map(f=>`<button data-tf="${f.minutes===d.base_minutes?0:f.minutes}" style="padding:3px 10px${(curTf||d.base_minutes)===f.minutes?';border-color:var(--acc);color:var(--acc);font-weight:700':''}">${f.minutes}分足</button>`).join('')+
    '<span class="mut" style="margin-left:10px">向き</span>'+fr.map(f=>`<span>${f.minutes}分足 <b class="${TC[f.trend]||'mut'}">${esc(f.label)}</b></span>`).join('<span class="mut">／</span>'):'')+
    (pd?`<span class="mut" style="margin-left:10px">前日（${pd.day.slice(5)}）</span><span>高値 <b>${n(pd.high,1)}</b>　安値 <b>${n(pd.low,1)}</b>　終値 <b>${n(pd.close,1)}</b></span>`:'');
  const tt=(state?.history||[]).find(t=>t.id===curTrade),hmm=s=>new Date(s).toLocaleTimeString('ja-JP',{timeZone:'Asia/Tokyo',hour:'2-digit',minute:'2-digit'});
  $('tinfo').innerHTML=tt?`<b>この取引</b>　${md(tt.entry_time)} 買 ${n(tt.entry_price,1)} → ${hmm(tt.exit_time)} 売 ${n(tt.exit_price,1)}　${yen(tt.pnl)}　<span class="mut">${esc(tt.reason||'')}／最初の損切り ${n(tt.initial_stop,1)}／${n(tt.qty)}株</span>`+(d.bars.length?'':'　<span class="warn">この日の足が保存されていません</span>'):'';
  if(d.day)$('note').textContent=`${d.minutes||''}分足・${d.day} の保存済みの足。青い丸が買い、赤（利益）または緑（損失）の丸が売りで、線でつないでいます。`;
  else $('note').textContent+=' 青い丸が買い、赤（利益）・緑（損失）の丸が売り。';
  if(!$('tip').style.display||$('tip').style.display==='none')draw(d)}
function openChart(sym,trade){cur=sym;curTf=0;$('mtf').innerHTML='';curTrade=trade?trade.id:null;curDay=trade?dayOf(trade.entry_time):null;$('tinfo').innerHTML='';$('l1').href=`https://finance.yahoo.co.jp/quote/${sym}.T/chart`;$('l2').href=`https://jp.tradingview.com/chart/?symbol=TSE%3A${sym}`;
  $('l3').href=`https://kabutan.jp/stock/chart?code=${sym}`;$('copy').textContent='銘柄コードをコピー';$('kmsg').textContent='';$('svg').innerHTML='';$('dt').textContent=sym;$('dlg').showModal();chart()}
document.addEventListener('click',async e=>{const c=e.target.closest('[data-close]');if(c){if(!confirm(c.dataset.label+' を成行で決済します。よろしいですか？'))return;
  const id=Number(c.dataset.close);closing.add(id);await fetch('/api/close?id='+id,{method:'POST'});load();return}
  const k=e.target.closest('[data-kabu]');if(k){const r=await kabu(k.dataset.kabu);if(r&&!r.ok)alert(r.message);return}
  const tfb=e.target.closest('[data-tf]');if(tfb){curTf=Number(tfb.dataset.tf);$('tip').style.display='none';chart();return}
  const tr=e.target.closest("[data-tid]");if(tr){const t=(state?.history||[]).find(t=>String(t.id)===tr.dataset.tid);if(t)openChart(t.symbol,t);return}
  const b=e.target.closest('[data-sym]');if(b)openChart(b.dataset.sym)});
async function kabu(sym,msgEl){if(msgEl)msgEl.textContent='切り替え中…';try{const r=await (await fetch('/api/kabu-chart?symbol='+encodeURIComponent(sym),{method:'POST'})).json();
  if(msgEl)msgEl.textContent=r.ok?'':r.message;return r}catch(e){if(msgEl)msgEl.textContent='失敗しました'}}
$('kabu').onclick=()=>kabu(cur,$('kmsg'));
document.querySelectorAll('#modes button').forEach(b=>b.onclick=async()=>{const m=b.dataset.mode;if(state&&state.mode_key===m)return;
  if(m==='live'){const a=prompt('本番に切り替えます。毛抜き底のサインが出ると、確認なしで実際に信用取引の買い注文を出します。\n開始するには yes と入力してください。');if((a||'').trim().toLowerCase()!=='yes')return}
  try{const r=await (await fetch('/api/mode?m='+m,{method:'POST'})).json();if(!r.ok)alert(r.message)}catch(e){alert('切り替えできませんでした')}load()});
$('stop_mode').onchange=$('trail_mode').onchange=$('size_mode').onchange=syncForm;
$('stop_value').oninput=$('trail_value').oninput=example;
$('save').onclick=async()=>{$('notify_on').value=[...document.querySelectorAll('.nt')].filter(c=>c.checked).map(c=>c.value).join(',');const body={};for(const k of FIELDS)body[k]=$(k).value;
  try{const r=await (await fetch('/api/settings',{method:'POST',body:JSON.stringify(body)})).json();$('savemsg').textContent=r.message;if(r.ok)formLoaded=false}catch(e){$('savemsg').textContent='保存できませんでした'}};
$('closeall').onclick=async()=>{if(!confirm('保有中のすべてのポジションを成行で決済します。よろしいですか？'))return;await fetch('/api/close-all',{method:'POST'});load()};
$('auto').onchange=()=>fetch('/api/auto-chart?on='+($('auto').checked?1:0),{method:'POST'});
$('close').onclick=()=>$('dlg').close();$('dlg').addEventListener('close',()=>{cur=null});
$('dlg').addEventListener('click',e=>{if(e.target===$('dlg'))$('dlg').close()});
$('copy').onclick=async()=>{try{await navigator.clipboard.writeText(cur);$('copy').textContent='コピーしました'}catch(e){$('copy').textContent='コピーできませんでした'}};
load();setInterval(load,2000);setInterval(chart,5000);
</script></body></html>"""


def past_chart(db_path, symbol: str, day: str, name: str | None = None) -> dict | None:
    """過去の日のチャート用の足を、保存済みのデータから読む（読み取り専用。取引の振り返り用）。"""
    if not db_path:
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT minutes, start, open, high, low, close, volume, complete, vwap FROM bars "
                "WHERE symbol=? AND substr(start,1,10)=? AND minutes=(SELECT MIN(minutes) FROM bars "
                "WHERE symbol=? AND substr(start,1,10)=?) ORDER BY start", (symbol, day, symbol, day)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    return dict(symbol=symbol, name=name, minutes=rows[0][0], quote=None, range_pct=None, day_range_pct=None,
                structure=None, pools=[], liquidity=[], day=day,
                bars=[dict(t=r[1], o=r[2], h=r[3], l=r[4], c=r[5], v=r[6], ok=bool(r[7]), vwap=r[8]) for r in rows])


def start_web(target, port: int = 8765, data_dir=None, db_path=None) -> ThreadingHTTPServer:
    """target はエンジン、またはモードを切り替えられる App。"""
    app = target if hasattr(target, "request_mode") else None

    def cur():
        return app.engine if app else target

    flag_path = data_dir / "auto_chart.txt" if data_dir else None
    settings_file = data_dir / "settings.json" if data_dir else None

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
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                symbol, day = q.get("symbol", [""])[0][:10], q.get("day", [""])[0][:10]
                if not symbol.isalnum():
                    return self._send(b"bad symbol", "text/plain", 400)
                data = None
                tf = q.get("tf", [""])[0]
                tf = int(tf) if tf.isdigit() and int(tf) in (5, 15) else None
                if day and day != datetime.now(JST).strftime("%Y-%m-%d"):
                    data = past_chart(db_path, symbol, day, cur().names.get(symbol))   # 過去の日は保存した足から
                self._send(json.dumps(data or cur().chart_data(symbol, tf), ensure_ascii=False).encode("utf-8"),
                           "application/json")
            elif self.path.split("?")[0] == "/analysis":
                from . import analysis
                self._send(analysis.PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path.startswith("/api/analysis/signals"):
                from urllib.parse import parse_qs, urlparse

                from . import analysis
                q = parse_qs(urlparse(self.path).query)
                res = analysis.analyze_signals(db_path, q.get("from", [""])[0][:10] or None,
                                               q.get("to", [""])[0][:10] or None)
                self._send(json.dumps(res, ensure_ascii=False).encode("utf-8"), "application/json")
            elif self.path.startswith("/api/analysis"):
                from urllib.parse import parse_qs, urlparse

                from . import analysis
                q = parse_qs(urlparse(self.path).query)
                arg = lambda k: (q.get(k, [""])[0][:10] or None)
                mode = "live" if arg("mode") == "live" else "paper"
                res = analysis.analyze(db_path, mode, arg("from"), arg("to"))
                self._send(json.dumps(res, ensure_ascii=False).encode("utf-8"), "application/json")
            elif self.path.split("?")[0] == "/squeeze":
                from . import squeeze_web
                self._send(squeeze_web.PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path.startswith("/api/squeeze/list"):
                from urllib.parse import parse_qs, urlparse

                from . import squeeze_web
                q = parse_qs(urlparse(self.path).query)
                day = lambda k: (q.get(k) or [None])[0]
                ok = lambda v: v is None or (len(v) == 10 and v.replace("-", "").isdigit())
                if not (ok(day("from")) and ok(day("to"))):
                    return self._send(b"bad date", "text/plain", 400)
                kind = day("kind") if day("kind") in ("squeeze", "wall", "sweep", "open", "fvg") else "squeeze"
                outcome = day("outcome") if day("outcome") in ("break", "pulled", "rejected") else None
                res = squeeze_web.list_events(db_path, day("from"), day("to"), kind, outcome)
                self._send(json.dumps(res, ensure_ascii=False).encode("utf-8"), "application/json")
            elif self.path.startswith("/api/squeeze/event?id="):
                from . import squeeze_web
                value = self.path.split("=", 1)[1]
                res = squeeze_web.get_event(db_path, int(value)) if value.isdigit() else None
                if res is None:
                    return self._send(b"not found", "text/plain", 404)
                self._send(json.dumps(res, ensure_ascii=False).encode("utf-8"), "application/json")
            elif self.path == "/kabu-inspect":
                from .kabuwin import show_chart
                _, text = show_chart("0000", inspect=True)
                self._send(text.encode("utf-8"), "text/plain; charset=utf-8")
            elif self.path == "/api/state":
                eng = cur()
                state = dict(eng.snapshot) if eng else {}
                if app:
                    state.update(can_switch=True, mode_key=app.mode,
                                 switching=bool(app.want and app.want != app.mode))
                if state.get("time"):   # 切り替え操作はすぐ画面に反映する
                    state.update(auto_chart=cur().auto_chart, halted=cur().risk.halted,
                                 settings=vars(cur().settings))
                self._send(json.dumps(state, ensure_ascii=False).encode("utf-8"),
                           "application/json")
            else:
                self._send(b"not found", "text/plain", 404)

        def do_POST(self):
            # 他サイトからの操作を防ぐため、同一オリジンのリクエストだけ受け付ける
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://localhost:{port}", f"http://127.0.0.1:{port}"):
                return self._send(b"forbidden", "text/plain", 403)
            if self.path.startswith("/api/kabu-chart?symbol="):
                from .kabuwin import show_chart
                ok, message = show_chart(self.path.split("=", 1)[1][:10])
                return self._send(json.dumps({"ok": ok, "message": message},
                                             ensure_ascii=False).encode("utf-8"), "application/json")
            if self.path.startswith("/api/auto-chart?on="):
                cur().auto_chart = self.path.endswith("=1")
                if flag_path:
                    try:
                        flag_path.write_text("1" if cur().auto_chart else "0")
                    except OSError:
                        pass
                return self._send(b"{}", "application/json")
            if self.path.startswith("/api/mode?m="):
                if app is None:
                    res = {"ok": False, "message": "この起動方法ではモードを切り替えられません"}
                else:
                    ok, message = app.request_mode(self.path.split("=", 1)[1])
                    res = {"ok": ok, "message": message}
                return self._send(json.dumps(res, ensure_ascii=False).encode("utf-8"),
                                  "application/json")
            if self.path == "/api/settings":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    cur().update_settings(json.loads(self.rfile.read(n) or b"{}"))
                    cur().settings.save(settings_file)
                    res = {"ok": True, "message": "保存しました。次の買いから適用します。"}
                except (ValueError, TypeError) as e:
                    res = {"ok": False, "message": str(e)}
                return self._send(json.dumps(res, ensure_ascii=False).encode("utf-8"),
                                  "application/json")
            if self.path.startswith("/api/close?id="):
                value = self.path.split("=", 1)[1]
                if not value.isdigit():
                    return self._send(b"bad id", "text/plain", 400)
                cur().close_ids.add(int(value))
                return self._send(b"{}", "application/json")
            if self.path == "/api/close-all":
                cur().close_all = True
                return self._send(b"{}", "application/json")
            if self.path == "/api/halt":
                cur().risk.halted = True
            elif self.path == "/api/resume":
                cur().risk.halted = False
            else:
                return self._send(b"not found", "text/plain", 404)
            self._send(b"{}", "application/json")

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
