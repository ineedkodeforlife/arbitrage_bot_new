"""Локальная страница со списком вилок и мгновенными уведомлениями в браузере.

Сканер при каждом изменении отдаёт результат открытым вкладкам через
Server-Sent Events — без опроса и без внешних сервисов, задержка в миллисекундах.
Страница доступна только с этого компьютера (127.0.0.1). С неё же уходят команды
сканеру (скрыть, закрепить, сохранить ставку) — POST-запросами.
"""
from __future__ import annotations

import csv
import io
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вилки BC Game + Polymarket</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#15181d;--muted:#667085;--line:#e3e6eb;--good:#0a7d3c;--goodbg:#e6f6ec;--warn:#b54708;--accent:#2457d6;--soft:#f0f2f5}
@media (prefers-color-scheme:dark){:root{--bg:#101216;--card:#191c22;--text:#e8eaee;--muted:#98a2b3;--line:#2a2f38;--good:#4cd787;--goodbg:#12301f;--warn:#f5a25d;--accent:#7da2ff;--soft:#20242c}}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:14px 16px 60px}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between;margin-bottom:8px}
h1{font-size:18px;margin:0}h2{font-size:12px;font-weight:600;color:var(--muted);margin:18px 0 6px;text-transform:uppercase;letter-spacing:.04em}
button{font:inherit;padding:6px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.sm{padding:1px 7px;font-size:12px;border-radius:6px}
button.on{background:var(--accent);border-color:var(--accent);color:#fff}
input,select{font:inherit;padding:3px 7px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--text)}
input[type=number]{width:84px}
.bar{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
#status{color:var(--muted);font-size:12px;margin-bottom:6px;font-variant-numeric:tabular-nums}
#hidden{color:var(--muted);font-size:12px;margin:6px 0;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chip{background:var(--soft);border-radius:999px;padding:1px 4px 1px 9px;display:inline-flex;gap:4px;align-items:center}
.chip button{border:0;background:none;padding:0 5px;color:var(--muted);font-size:14px;line-height:1}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#c33;margin-right:6px;vertical-align:1px}.dot.on{background:var(--good)}
details.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 12px;margin:8px 0}
details.panel summary{cursor:pointer;font-weight:600;font-size:13px}
.panel .row{display:flex;gap:8px 14px;flex-wrap:wrap;align-items:center;margin:8px 0 4px;font-variant-numeric:tabular-nums}
.panel .out{font-weight:600}.panel label{display:inline-flex;gap:6px;align-items:center}
#tabs,#sports{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
#sports{display:grid;gap:5px}#sports .frow{display:flex;gap:6px;flex-wrap:wrap;align-items:center}#sports .lbl{color:var(--muted);font-size:12px;min-width:62px}#sports button{padding:3px 10px;font-size:13px;border-radius:999px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:8px;align-items:start}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px;font-size:13px}
.card.arb{border-color:var(--good)}.card.young{opacity:.6;border-style:dashed}.card.fresh{animation:flash 2.5s}
.card.off{border-color:var(--line);color:var(--muted)}.card.off .pct{color:var(--muted)}
@keyframes flash{0%{background:var(--goodbg)}100%{background:var(--card)}}
.top{display:flex;gap:4px 8px;align-items:baseline;flex-wrap:wrap}
.pct{font-size:16px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--muted)}.arb .pct{color:var(--good)}
.match{font-weight:600}.meta{color:var(--muted);font-size:12px}.flag{color:var(--warn);font-size:12px;flex-basis:100%}
.age{font-size:12px;font-variant-numeric:tabular-nums;background:var(--soft);border-radius:999px;padding:0 7px}
.legs{margin:5px 0 3px;display:grid;gap:2px;font-variant-numeric:tabular-nums}
.leg{display:grid;grid-template-columns:74px 1fr auto auto;gap:8px;align-items:baseline}
.leg b{font-weight:600}.leg .amt{white-space:nowrap;text-align:right}
.sum{font-weight:600;font-variant-numeric:tabular-nums}.bad{color:var(--warn)}
.foot{display:flex;gap:4px 10px;flex-wrap:wrap;align-items:center;margin-top:5px}
.foot a{color:var(--accent);font-size:12px}.foot .sp{flex:1}.empty{color:var(--muted);padding:10px 2px}
.form{background:var(--soft);border-radius:8px;padding:8px;margin-top:6px;display:grid;gap:5px}
.form .frow{display:flex;gap:6px;flex-wrap:wrap;align-items:center}.form input[type=number]{width:76px}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600;font-size:12px}
td.num,th.num{text-align:right;white-space:nowrap}
@media (max-width:480px){.grid{grid-template-columns:1fr}.leg{grid-template-columns:1fr auto}}
</style></head><body><main>
<header><h1>Вилки BC Game + Polymarket</h1>
<div class="bar"><button id="enable" class="primary">Включить уведомления и звук</button><button id="test">Проверить</button></div></header>
<div id="status"><span class="dot" id="dot"></span><span id="st">подключение…</span></div>
<div id="tabs"><button data-tab="arbs" class="on">Вилки</button><button data-tab="bets">Мои ставки</button></div>

<div id="page-arbs">
<details class="panel" id="calcbox"><summary>Калькулятор и сумма ставки</summary>
<div class="row"><label>Считать от
<select id="mode"><option value="total">общей суммы</option><option value="leg1">суммы 1-го плеча (BC Game)</option><option value="leg2">суммы 2-го плеча (Polymarket)</option></select></label>
<label>сумма, $ <input type="number" id="amount" min="1" step="1"></label>
<span class="meta">по этой сумме считаются ставки во всех карточках ниже</span></div>
<div class="row"><span class="meta">Свои коэффициенты:</span>
<label>кф 1-го плеча <input type="number" id="k1" min="1.01" step="0.01"></label>
<label>кф 2-го плеча <input type="number" id="k2" min="1.01" step="0.01"></label>
<span class="out" id="manual"></span></div></details>
<div id="sports"></div>
<div id="hidden" hidden></div>
<div id="pinwrap" hidden><h2>Закреплённые</h2><div id="pinned" class="grid"></div></div>
<h2>Вилки</h2><div id="arbs" class="grid"></div><div class="empty" id="noarbs">Ждём данные сканера…</div>
<div id="nearwrap" hidden><h2>Ближайшие к вилке</h2><div id="near" class="grid"></div></div>
</div>

<div id="page-bets" hidden>
<div class="bar" style="margin:10px 0"><span id="betsum" class="sum"></span><a href="/bets.csv" style="color:var(--accent)">Скачать CSV</a></div>
<div id="bets"></div></div>
</main><script>
const $=id=>document.getElementById(id);let ctx=null,seen=new Map(),last=null;
const store={get(k,d){try{const v=localStorage.getItem(k);return v===null?d:JSON.parse(v)}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
let mode=store.get('mode','total'),amount=store.get('amount',100),sport=store.get('sport','all'),group=store.get('group','all'),when=store.get('when','all'),tab='arbs';
if(sport==='esports'){sport='all';group='esport'}   // значение от прежней версии страницы
const SPORT={soccer:'Футбол',basketball:'Баскетбол',tennis:'Теннис',hockey:'Хоккей',football:'Ам. футбол',baseball:'Бейсбол',cs2:'CS2',dota2:'Dota 2',lol:'LoL',valorant:'Valorant',r6:'Rainbow Six',hok:'Honor of Kings',mlbb:'Mobile Legends',cod:'Call of Duty',overwatch:'Overwatch',starcraft:'StarCraft',rocketleague:'Rocket League',wot:'World of Tanks',mma:'MMA',boxing:'Бокс',cricket:'Крикет',volleyball:'Волейбол',darts:'Дартс',tabletennis:'Наст. теннис',aussie:'Австрал. футбол',badminton:'Бадминтон',snooker:'Снукер'};
const ESPORT=new Set(['cs2','dota2','lol','valorant','r6','hok','mlbb','cod','overwatch','starcraft','rocketleague','wot','wildrift','aov','pubg','apex','crossfire','warcraft','aoe','deadlock','marvelrivals','freefire','brawlstars','halo','fortnite','tft']);
const isEsport=s=>ESPORT.has(s)||s.startsWith('esport:');const sportName=s=>SPORT[s]||s;
function beep(){try{ctx=ctx||new (window.AudioContext||window.webkitAudioContext)();if(ctx.state==='suspended')ctx.resume();
 [880,1175,1568].forEach((f,i)=>{const o=ctx.createOscillator(),g=ctx.createGain(),t=ctx.currentTime+i*0.13;o.frequency.value=f;o.type='sine';
 g.gain.setValueAtTime(0.0001,t);g.gain.exponentialRampToValueAtTime(0.35,t+0.02);g.gain.exponentialRampToValueAtTime(0.0001,t+0.12);
 o.connect(g).connect(ctx.destination);o.start(t);o.stop(t+0.13)})}catch(e){}}
function notify(title,body,url){beep();if(!('Notification'in window)||Notification.permission!=='granted')return;
 const n=new Notification(title,{body,tag:title,requireInteraction:false});n.onclick=()=>{window.focus();if(url)window.open(url,'_blank');n.close()}}
function perm(){const b=$('enable');if(!('Notification'in window)){b.textContent='Браузер не поддерживает уведомления';b.disabled=true;return}
 if(Notification.permission==='granted'){b.textContent='Уведомления включены';b.classList.remove('primary')}
 else if(Notification.permission==='denied'){b.textContent='Уведомления запрещены в настройках браузера'}}
$('enable').onclick=async()=>{beep();if('Notification'in window&&Notification.permission!=='granted')await Notification.requestPermission();perm()};
$('test').onclick=()=>notify('Проверка: вилка +1.23%','Команда A — Команда B\nТак будет выглядеть уведомление');
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const money=x=>'$'+x.toFixed(2);
function post(path,body){fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).catch(()=>{})}
function copy(text,btn){const done=()=>{const t=btn.textContent;btn.textContent='✓';setTimeout(()=>btn.textContent=t,900)};
 if(navigator.clipboard)navigator.clipboard.writeText(text).then(done,()=>{});}
function ageText(a){if(!a.since)return'';const s=Math.max(0,Date.now()/1000-a.since);
 return s<60?`${s.toFixed(0)} с`:`${Math.floor(s/60)} мин ${Math.floor(s%60)} с`}

// Расчёт ставок по стакану. N — число акций Polymarket = выплата при любом исходе.
// Режимы: leg1 — задана ставка в первое плечо BC, leg2 — сколько потратить на Polymarket,
// total — общая сумма на все плечи.
// Расчёт ставок по стакану. Ставки в BC Game — только целые доллары (копейки в ставке
// выглядят как работа калькулятора вилок). Поэтому: сначала примерная выплата N по
// выбранному режиму, затем ставки BC округляются до долларов, а акций на Polymarket
// покупается столько, сколько гарантирует самая маленькая из выплат BC.
// Режимы: leg1 — задана ставка в первое плечо BC, leg2 — сколько потратить на
// Polymarket, total — общая сумма на все плечи.
const unitCost=(c,p)=>p+c.fee*p*(1-p);
function pmCost(c,N){let left=N,spent=0;for(const [p,sz] of c.asks){const t=Math.min(left,sz);spent+=t*unitCost(c,p);left-=t;if(left<=1e-9)break}
 return{spent,short:left>1e-6}}
function plan(a){const c=a.calc,inv=c.ks.reduce((x,k)=>x+1/k,0);let N=0;
 if(mode==='leg1')N=Math.round(amount)*c.ks[0];
 else{let budget=amount,full=false;for(const [p,sz] of c.asks){const per=unitCost(c,p)+(mode==='total'?inv:0),can=budget/per;
   if(can<=sz){N+=can;full=true;break}N+=sz;budget-=sz*per}
  if(!full)return{N,bc:c.ks.map(k=>Math.max(1,Math.round(N/k))),spent:0,total:0,profit:0,profitMax:0,short:true,over:false}}
 const bc=c.ks.map(k=>Math.max(1,Math.round(N/k))),pays=bc.map((s,i)=>s*c.ks[i]);
 N=Math.floor(Math.min(...pays)*100)/100;               // акции Polymarket: до сотых
 const {spent,short}=pmCost(c,N),total=bc.reduce((x,y)=>x+y,0)+spent;
 return{N,bc,spent,total,profit:N-total,profitMax:Math.max(...pays)-total,short,over:c.max_shares>0&&N>c.max_shares+1e-6}}
const usd0=x=>'$'+Math.round(x);
function bodyHtml(a,isArb){const r=a.stale?null:plan(a),bcLegs=a.legs.slice(0,-1),pm=a.legs[a.legs.length-1];
 let h=`<div class="top"><span class="pct">${a.pct>0?'+':''}${a.pct.toFixed(2)}%</span><span class="match">${esc(a.match)}</span><span class="meta">${esc(sportName(a.sport))} · ${esc(a.when)}</span>${isArb&&a.since?`<span class="age">${ageText(a)}</span>`:''}${a.stale?'<span class="meta">нет данных — матч закончился или рынок закрыт</span>':''}${a.suspicious?'<span class="flag">проверьте вручную — слишком большая доходность</span>':''}${a.weak?`<span class="flag">матч сопоставлен по одной команде — на Polymarket: ${esc(a.pm_match)}</span>`:''}</div><div class="legs">`;
 h+=bcLegs.map((l,i)=>`<div class="leg"><b>BC Game</b><span>${esc(l.what)} · кф ${l.k}</span><span class="amt">${r?usd0(r.bc[i]):''}</span>${r?`<button class="sm" data-copy="${Math.round(r.bc[i])}">коп.</button>`:'<span></span>'}</div>`).join('');
 h+=`<div class="leg"><b>Polymarket</b><span>${esc(pm.what)} · ${esc(pm.price)}</span><span class="amt">${r&&!r.short?r.N.toFixed(2)+' акц ≈ '+money(r.spent):''}</span>${r&&!r.short?`<button class="sm" data-copy="${r.N.toFixed(2)}">коп.</button>`:'<span></span>'}</div></div>`;
 if(r){if(r.short)h+='<div class="bad">В стакане не хватает объёма на такую сумму — уменьшите её.</div>';
  else h+=`<div class="sum ${r.profit<0?'bad':''}">Всего ${money(r.total)} → вернётся ${money(r.N)} · ${r.profit<0?'убыток':'прибыль'} ${money(Math.abs(r.profit))} (${(100*r.profit/r.total).toFixed(2)}%)${r.profitMax-r.profit>=0.01?`, если выиграет BC Game — ${money(r.profitMax)}`:''}${r.over?' <span class="meta">· сумма больше объёма вилки, часть по худшей цене</span>':''}</div>`}
 return h+`<div class="meta">${esc(a.volume)}</div>`}
function saveForm(el){const a=el._a,r=plan(a),bcLegs=a.legs.slice(0,-1),pm=a.legs[a.legs.length-1],f=el.querySelector('.form');
 f.hidden=false;f.innerHTML='<div class="meta">Впишите, что получилось на самом деле, и сохраните.</div>'+
  bcLegs.map((l,i)=>`<div class="frow"><b>BC Game</b><span>${esc(l.what)}</span><label>ставка $<input type="number" step="1" data-f="stake${i}" value="${Math.round(r.bc[i])}"></label><label>кф<input type="number" step="0.01" data-f="k${i}" value="${l.k}"></label></div>`).join('')+
  `<div class="frow"><b>Polymarket</b><span>${esc(pm.what)}</span><label>акций<input type="number" step="0.1" data-f="shares" value="${r.N.toFixed(2)}"></label><label>потрачено $<input type="number" step="0.01" data-f="spent" value="${r.spent.toFixed(2)}"></label></div>
<div class="frow"><input type="text" data-f="note" placeholder="заметка (необязательно)" style="flex:1;min-width:140px"><button class="primary sm" data-act="save-ok">Сохранить</button><button class="sm" data-act="save-cancel">Отмена</button></div>`}
function submitBet(el){const a=el._a,f=el.querySelector('.form'),v=n=>parseFloat((f.querySelector(`[data-f="${n}"]`)||{}).value)||0,bcLegs=a.legs.slice(0,-1),pm=a.legs[a.legs.length-1];
 const legs=bcLegs.map((l,i)=>({site:'BC Game',what:l.what,stake:v('stake'+i),k:v('k'+i),payout:v('stake'+i)*v('k'+i)}));
 const sh=v('shares'),sp=v('spent');legs.push({site:'Polymarket',what:pm.what,stake:sp,k:sp>0?sh/sp:0,payout:sh});
 post('/bet',{match:a.match,sport:a.sport,when:a.when,bc_url:a.bc_url,pm_url:a.pm_url,legs,note:f.querySelector('[data-f="note"]').value});
 f.hidden=true;f.innerHTML='';const b=el.querySelector('[data-act="save"]');b.textContent='Сохранено ✓';setTimeout(()=>b.textContent='Сохранить ставку',1500)}
function makeCard(a){const el=document.createElement('div');el.dataset.id=a.id;
 el.innerHTML=`<div class="dyn"></div><div class="foot"><a class="bc" target="_blank" rel="noopener">BC Game ↗</a><a class="pm" target="_blank" rel="noopener">Polymarket ↗</a><span class="sp"></span>
<button class="sm" data-act="pin"></button><button class="sm" data-act="save">Сохранить ставку</button>
<select class="sm" data-act="hide" style="padding:1px 4px;font-size:12px"><option value="">Скрыть…</option><option value="market">эту связку</option><option value="match">этот матч</option><option value="sport">вид спорта</option></select></div><div class="form" hidden></div>`;
 // pointerdown, а не click: карточка обновляется несколько раз в секунду, и кнопка
 // внутри расчёта может быть перерисована между нажатием и отпусканием
 el.addEventListener('pointerdown',e=>{const b=e.target.closest('button[data-copy]');if(b){e.preventDefault();copy(b.dataset.copy,b)}});
 el.addEventListener('click',e=>{const b=e.target.closest('button[data-act]');if(!b)return;const cur=el._a,act=b.dataset.act;
  if(act==='pin'){if(el._pinned)post('/unhide',{kind:'pin',value:cur.id});else post('/hide',{kind:'pin',value:cur.id,label:cur.match})}
  else if(act==='save')saveForm(el);else if(act==='save-ok')submitBet(el);
  else if(act==='save-cancel'){const f=el.querySelector('.form');f.hidden=true;f.innerHTML=''}});
 el.querySelector('select').addEventListener('change',e=>{const k=e.target.value,cur=el._a;e.target.value='';
  if(k==='match')post('/hide',{kind:'match',value:cur.bc_id,label:cur.match});
  else if(k==='sport')post('/hide',{kind:'sport',value:cur.sport,label:sportName(cur.sport)});
  else if(k==='market')post('/hide',{kind:'market',value:cur.id,label:cur.match+': '+cur.legs[cur.legs.length-1].what})});
 return el}
function fill(el,a,isArb,fresh,pinned){el._a=a;el._pinned=pinned;
 el.className='card'+(isArb?' arb':pinned?' off':'')+(isArb&&!a.announced?' young':'')+(fresh?' fresh':'');
 el.querySelector('.dyn').innerHTML=bodyHtml(a,isArb);
 const pb=el.querySelector('[data-act="pin"]');pb.textContent=pinned?'Открепить':'Закрепить';pb.classList.toggle('on',pinned);
 el.querySelector('.bc').href=a.bc_url;el.querySelector('.pm').href=a.pm_url}
function sync(box,list,fresh,pins,arbIds){const have=new Map([...box.children].map(c=>[c.dataset.id,c])),ids=new Set(list.map(a=>a.id));
 for(const [id,el] of have)if(!ids.has(id)){el.remove();have.delete(id)}
 const busy=box.contains(document.activeElement)&&['INPUT','SELECT'].includes(document.activeElement.tagName);
 list.forEach((a,i)=>{let el=have.get(a.id);if(!el){el=makeCard(a);box.appendChild(el)}
  fill(el,a,arbIds.has(a.id),fresh.has(a.id),pins.has(a.id));
  if(!busy&&box.children[i]!==el)box.insertBefore(el,box.children[i])})}   // порядок не трогаем, пока что-то вводят
function hiddenBar(h){const items=[];for(const kind of ['sport','match','market'])for(const x of (h&&h[kind])||[])items.push([kind,x]);
 const box=$('hidden');box.hidden=!items.length;if(!items.length)return;
 box.innerHTML='Скрыто: '+items.map(([k,x],i)=>`<span class="chip">${esc(x.label||x.value)}<button data-i="${i}" title="Вернуть">×</button></span>`).join('')+'<button class="sm" data-all="1">Показать всё</button>';
 box.onclick=e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.all)post('/unhide',{});else{const [k,x]=items[+b.dataset.i];post('/unhide',{kind:k,value:x.value})}}}
// Три фильтра: когда (лайв / прематч), группа (спорт / киберспорт), дисциплина внутри группы.
const inGroup=s=>group==='all'||(group==='esport')===isEsport(s);
const passWhen=a=>when==='all'||(when==='live')===!!a.live;
const pass=a=>passWhen(a)&&inGroup(a.sport)&&(sport==='all'||a.sport===sport);
function sportTabs(d){const counts=d.sports||{},arbs=d.arbs.filter(passWhen),n={};for(const a of arbs)n[a.sport]=(n[a.sport]||0)+1;
 const nLive=d.arbs.filter(a=>a.live).length,nEs=arbs.filter(a=>isEsport(a.sport)).length;
 const btn=(attr,key,cur,label,k)=>`<button data-${attr}="${esc(key)}" class="${cur===key?'on':''}">${esc(label)}${k?` · ${k}`:''}</button>`;
 let h='<div class="frow"><span class="lbl">Когда</span>'+btn('when','all',when,'Все',d.arbs.length)+btn('when','live',when,'Лайв',nLive)+btn('when','pre',when,'Прематч',d.arbs.length-nLive)+'</div>';
 h+='<div class="frow"><span class="lbl">Раздел</span>'+btn('group','all',group,'Все',arbs.length)+btn('group','sport',group,'Спорт',arbs.length-nEs)+btn('group','esport',group,'Киберспорт',nEs)+'</div>';
 if(group!=='all'){const names=Object.keys(counts).filter(inGroup).sort((x,y)=>counts[y]-counts[x]);
  if(sport!=='all'&&!names.includes(sport))names.push(sport);   // выбранная дисциплина остаётся, даже если матчей сейчас нет
  h+=`<div class="frow"><span class="lbl">${group==='esport'?'Дисциплина':'Вид спорта'}</span>`+btn('sport','all',sport,'Все',names.reduce((x,s)=>x+(n[s]||0),0))+names.map(s=>btn('sport',s,sport,sportName(s),n[s]||0)).join('')+'</div>'}
 const box=$('sports');if(box._h!==h){box.innerHTML=h;box._h=h}}
$('sports').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;
 if(b.dataset.when){when=b.dataset.when;store.set('when',when)}
 else if(b.dataset.group){group=b.dataset.group;sport='all';store.set('group',group);store.set('sport',sport)}   // дисциплины у разделов разные
 else if(b.dataset.sport){sport=b.dataset.sport;store.set('sport',sport)}
 if(last)render(last)});
function filterText(){const parts=[];if(when!=='all')parts.push(when==='live'?'лайв':'прематч');
 if(sport!=='all')parts.push(sportName(sport));else if(group!=='all')parts.push(group==='esport'?'киберспорт':'спорт');return parts.join(', ')}
function betsPage(d){const bets=(d.bets||[]).slice().reverse();$('tabs').querySelector('[data-tab="bets"]').textContent=`Мои ставки${bets.length?' · '+bets.length:''}`;
 if(tab!=='bets')return;const tot=bets.reduce((x,b)=>x+b.total,0),pr=bets.reduce((x,b)=>x+b.profit_min,0);
 $('betsum').textContent=bets.length?`Ставок: ${bets.length} · вложено ${money(tot)} · гарантированный результат ${pr<0?'−':'+'}${money(Math.abs(pr))}`:'';
 const h=bets.length?`<table><tr><th>Когда</th><th>Матч</th><th>Плечи</th><th class="num">Вложено</th><th class="num">Результат</th><th></th></tr>`+bets.map(b=>{
  const dt=new Date(b.ts*1000),same=Math.abs(b.profit_max-b.profit_min)<0.01,sg=x=>(x<0?'−':'+')+money(Math.abs(x));
  return `<tr><td>${dt.toLocaleDateString('ru')} ${dt.toLocaleTimeString('ru',{hour:'2-digit',minute:'2-digit'})}</td>
<td><b>${esc(b.match)}</b><div class="meta">${esc(sportName(b.sport))}${b.note?' · '+esc(b.note):''}</div><div class="meta"><a href="${esc(b.bc_url)}" target="_blank" rel="noopener" style="color:var(--accent)">BC Game ↗</a> <a href="${esc(b.pm_url)}" target="_blank" rel="noopener" style="color:var(--accent)">Polymarket ↗</a></div></td>
<td>${b.legs.map(l=>`${esc(l.site)}: ${esc(l.what)} — ${money(l.stake)}${l.site==='Polymarket'?` (${l.payout.toFixed(1)} акц)`:` × ${l.k}`}`).join('<br>')}</td>
<td class="num">${money(b.total)}</td><td class="num ${b.profit_min<0?'bad':''}">${same?sg(b.profit_min):sg(b.profit_min)+' … '+sg(b.profit_max)}</td>
<td><button class="sm" data-del="${esc(b.id)}" title="Удалить запись">×</button></td></tr>`}).join('')+'</table>'
  :'<div class="empty">Пока ничего не сохранено. На карточке вилки нажмите «Сохранить ставку».</div>';
 const box=$('bets');if(box._h!==h){box.innerHTML=h;box._h=h}}
$('bets').addEventListener('click',e=>{const b=e.target.closest('button[data-del]');if(b&&confirm('Удалить запись о ставке?'))post('/bet/delete',{id:b.dataset.del})});
$('tabs').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;tab=b.dataset.tab;
 for(const x of $('tabs').children)x.classList.toggle('on',x===b);$('page-arbs').hidden=tab!=='arbs';$('page-bets').hidden=tab!=='bets';if(last)render(last)});
function manual(){const k1=parseFloat($('k1').value),k2=parseFloat($('k2').value);if(!(k1>1&&k2>1&&amount>0)){$('manual').textContent='';return}
 const N=mode==='leg1'?Math.round(amount)*k1:mode==='leg2'?Math.round(amount)*k2:amount/(1/k1+1/k2);
 const s1=Math.max(1,Math.round(N/k1)),s2=Math.max(1,Math.round(N/k2)),t=s1+s2,p1=s1*k1-t,p2=s2*k2-t,p=Math.min(p1,p2);
 $('manual').textContent=`1-е плечо $${s1} · 2-е плечо $${s2} · всего $${t} · ${p<0?'убыток':'прибыль'} ${money(Math.abs(p))} (${(100*p/t).toFixed(2)}%)`+
  (Math.abs(p1-p2)>=0.01?` · при выигрыше 1-го ${money(p1)}, 2-го ${money(p2)}`:'')}
$('mode').value=mode;$('amount').value=amount;$('calcbox').open=store.get('calcopen',true);
$('calcbox').addEventListener('toggle',()=>store.set('calcopen',$('calcbox').open));
$('mode').onchange=e=>{mode=e.target.value;store.set('mode',mode);manual();if(last)render(last)};
$('amount').oninput=e=>{const v=parseFloat(e.target.value);if(v>0){amount=v;store.set('amount',v);manual();if(last)render(last)}};
$('k1').oninput=$('k2').oninput=manual;
function render(d){last=d;const now=Date.now(),fresh=new Set(),pins=new Set(d.pins||[]);
 const arbs=d.arbs.filter(pass),arbIds=new Set(d.arbs.map(a=>a.id));
 for(const a of arbs){if(!a.announced)continue;const prev=seen.get(a.id);if(prev===undefined||now-prev>60000){fresh.add(a.id);
   notify(`Вилка ${a.pct>0?'+':''}${a.pct.toFixed(2)}%: ${a.match}`,a.legs.map(l=>`${l.site}: ${l.what} — ${l.price}`).join('\n')+'\n'+a.volume,a.bc_url)}
  seen.set(a.id,now)}
 // закреплённые показываются всегда: зелёные, пока вилка есть, серые — когда её нет
 const pinned=d.arbs.filter(a=>pins.has(a.id)).concat(d.pinned||[]),rest=arbs.filter(a=>!pins.has(a.id));
 $('pinwrap').hidden=!pinned.length;sync($('pinned'),pinned,fresh,pins,arbIds);sync($('arbs'),rest,fresh,pins,arbIds);
 const ready=arbs.filter(a=>a.announced).length;
 $('noarbs').hidden=rest.length>0;$('noarbs').textContent=`Сейчас вилок от ${d.min_profit}% нет`+(filterText()?` (${filterText()}).`:'.');
 const near=d.near.filter(pass);$('nearwrap').hidden=!near.length;sync($('near'),near,fresh,pins,arbIds);
 hiddenBar(d.hidden);sportTabs(d);betsPage(d);
 const bl=Object.entries(d.bc||{}).map(([m,b])=>`BC ${m==='live'?'лайв':'прематч'} ${b.error?'ошибка':(b.lag==null?'…':b.lag.toFixed(1)+' с')}`).join(' · ');
 const ws=d.ws&&d.ws.on?` · Polymarket ${d.ws.alive?Math.max(d.ws.lag_ms,0).toFixed(0)+' мс':'переподключение'}`:'';
 $('st').textContent=`${d.mode} · ${d.time} · матчей ${d.matched} · связок ${d.cands} · отставание: ${bl}${ws}`+(d.hold?` · сигнал, когда вилка держится ${d.hold} с`:'');
 document.title=(ready?`(${ready}) `:'')+'Вилки BC Game + Polymarket'}
function connect(){const es=new EventSource('/events');es.onopen=()=>$('dot').classList.add('on');
 es.onmessage=e=>render(JSON.parse(e.data));es.onerror=()=>{$('dot').classList.remove('on');$('st').textContent='нет связи со сканером — он запущен?'}}
perm();connect();
</script></body></html>
"""

COMMANDS = ("/hide", "/unhide", "/bet", "/bet/delete")


def bets_csv(bets: list) -> bytes:
    """Учёт ставок для Excel: одна строка на ставку."""
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["когда", "вид спорта", "матч", "плечи", "вложено, $", "результат мин., $",
                "результат макс., $", "заметка", "BC Game", "Polymarket"])
    for b in bets:
        legs = " | ".join(f"{l['site']}: {l['what']} — ${l['stake']:.2f}"
                          + (f" ({l['payout']:.1f} акц)" if l["site"] == "Polymarket" else f" x {l['k']:g}")
                          for l in b.get("legs", []))
        w.writerow([time.strftime("%Y-%m-%d %H:%M", time.localtime(b.get("ts", 0))),
                    b.get("sport", ""), b.get("match", ""), legs, f"{b.get('total', 0):.2f}",
                    f"{b.get('profit_min', 0):.2f}", f"{b.get('profit_max', 0):.2f}",
                    b.get("note", ""), b.get("bc_url", ""), b.get("pm_url", "")])
    return out.getvalue().encode("utf-8-sig")


class Hub:
    def __init__(self):
        self.clients: set[queue.Queue] = set()
        self.last: str | None = None
        self.lock = threading.Lock()
        self.on_command = None      # on_command(path, dict) — команды со страницы
        self.bets = lambda: []      # откуда брать ставки для выгрузки в CSV

    def publish(self, payload: dict) -> None:
        msg = json.dumps(payload, ensure_ascii=False)
        with self.lock:
            self.last = msg
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:          # вкладка не успевает — ей хватит следующего
                    pass


def serve(port: int) -> Hub:
    hub = Hub()
    origins = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):          # не засоряем консоль сканера
            pass

        def _empty(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send(self, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # Команды принимаем только с самой страницы: чужой сайт, открытый в том же
            # браузере, не должен уметь что-то скрывать или записывать.
            if (self.path not in COMMANDS
                    or self.headers.get("Origin") not in origins
                    or "application/json" not in (self.headers.get("Content-Type") or "")):
                return self._empty(403)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 16384)) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._empty(400)
            if hub.on_command:
                hub.on_command(self.path, body)
            self._empty(204)

        def do_GET(self):
            if self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q: queue.Queue = queue.Queue(maxsize=5)
                with hub.lock:
                    hub.clients.add(q)
                    if hub.last:
                        q.put_nowait(hub.last)
                try:
                    while True:
                        try:
                            msg = q.get(timeout=15)
                            self.wfile.write(f"data: {msg}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
                finally:
                    with hub.lock:
                        hub.clients.discard(q)
            elif self.path in ("/", "/index.html"):
                self._send(PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/bets.csv":
                self._send(bets_csv(list(hub.bets())), "text/csv; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="bets.csv"'})
            else:
                self._empty(404)

    class Server(ThreadingHTTPServer):
        # В Windows с этим флагом два сканера молча заняли бы один порт, и страница
        # показывала бы данные не того из них.
        allow_reuse_address = False

        def handle_error(self, request, client_address):
            pass                            # вкладку закрыли посреди запроса — не страшно

    srv = Server(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return hub
