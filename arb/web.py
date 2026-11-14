"""Локальная страница со списком вилок и мгновенными уведомлениями в браузере.

Сканер при каждом изменении отдаёт результат открытым вкладкам через
Server-Sent Events — без опроса и без внешних сервисов, задержка в миллисекундах.
Страница доступна только с этого компьютера (127.0.0.1). С неё же можно скрыть
матч или вид спорта: команда уходит сканеру POST-запросом.
"""
from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вилки BC Game + Polymarket</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#15181d;--muted:#667085;--line:#e3e6eb;--good:#0a7d3c;--goodbg:#e6f6ec;--warn:#b54708;--accent:#2457d6;--soft:#f0f2f5}
@media (prefers-color-scheme:dark){:root{--bg:#101216;--card:#191c22;--text:#e8eaee;--muted:#98a2b3;--line:#2a2f38;--good:#4cd787;--goodbg:#12301f;--warn:#f5a25d;--accent:#7da2ff;--soft:#20242c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;justify-content:space-between;margin-bottom:14px}
h1{font-size:20px;margin:0}h2{font-size:14px;font-weight:600;color:var(--muted);margin:26px 0 8px;text-transform:uppercase;letter-spacing:.04em}
button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.sm{padding:3px 9px;font-size:13px;border-radius:6px}
.bar{display:flex;gap:8px;flex-wrap:wrap}
#status{color:var(--muted);font-size:13px;margin-bottom:6px;font-variant-numeric:tabular-nums}
#hidden{color:var(--muted);font-size:13px;margin:6px 0;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chip{background:var(--soft);border-radius:999px;padding:2px 4px 2px 10px;display:inline-flex;gap:4px;align-items:center}
.chip button{border:0;background:none;padding:0 6px;color:var(--muted);font-size:15px;line-height:1}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#c33;margin-right:6px;vertical-align:1px}.dot.on{background:var(--good)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px}
.card.arb{border-color:var(--good)}.card.young{opacity:.6;border-style:dashed}.card.fresh{animation:flash 2.5s}
@keyframes flash{0%{background:var(--goodbg)}100%{background:var(--card)}}
.top{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
.pct{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--muted)}.arb .pct{color:var(--good)}
.match{font-weight:600}.meta{color:var(--muted);font-size:13px}.flag{color:var(--warn);font-size:13px}
.age{font-size:13px;font-variant-numeric:tabular-nums;background:var(--soft);border-radius:999px;padding:1px 9px}
.legs{margin:10px 0 6px;display:grid;gap:4px}.leg{display:grid;grid-template-columns:92px 1fr auto;gap:10px;font-variant-numeric:tabular-nums}
.leg b{font-weight:600}.leg span:last-child{white-space:nowrap}
.calc{background:var(--soft);border-radius:8px;padding:8px 10px;margin:8px 0;display:grid;gap:6px;font-variant-numeric:tabular-nums}
.calc label{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.calc input{font:inherit;width:96px;padding:4px 8px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--text)}
.res{display:grid;gap:3px}.res .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.res .sum{font-weight:600}.res .bad{color:var(--warn)}
.foot{display:flex;gap:6px 14px;flex-wrap:wrap;align-items:center;margin-top:6px}
.foot a{color:var(--accent);font-size:13px}.foot .sp{flex:1}.empty{color:var(--muted);padding:14px 2px}
@media (max-width:560px){.leg{grid-template-columns:1fr;gap:0;margin-bottom:6px}}
</style></head><body><main>
<header><h1>Вилки BC Game + Polymarket</h1>
<div class="bar"><button id="enable" class="primary">Включить уведомления и звук</button><button id="test">Проверить</button></div></header>
<div id="status"><span class="dot" id="dot"></span><span id="st">подключение…</span></div>
<div id="hidden" hidden></div>
<h2>Вилки</h2><div id="arbs"></div><div class="empty" id="noarbs">Ждём данные сканера…</div>
<div id="nearwrap" hidden><h2>Ближайшие к вилке</h2><div id="near"></div></div>
</main><script>
const $=id=>document.getElementById(id);let ctx=null,seen=new Map(),stakes=new Map(),last=null;
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
function copy(text,btn){const done=()=>{const t=btn.textContent;btn.textContent='скопировано';setTimeout(()=>btn.textContent=t,900)};
 if(navigator.clipboard)navigator.clipboard.writeText(text).then(done,()=>{});}
function ageText(a){if(!a.since)return'';const s=Math.max(0,Date.now()/1000-a.since);
 return s<60?`держится ${s.toFixed(0)} с`:`держится ${Math.floor(s/60)} мин ${Math.floor(s%60)} с`}
// Ставка в первое плечо BC Game -> сколько акций купить и что получится.
function plan(a,stake){const c=a.calc,N=stake*c.ks[0];let left=N,spent=0;
 for(const [p,sz] of c.asks){const t=Math.min(left,sz);spent+=t*(p+c.fee*p*(1-p));left-=t;if(left<=1e-9)break}
 const bc=c.ks.map(k=>N/k),total=bc.reduce((x,y)=>x+y,0)+spent;
 return{N,bc,spent,total,profit:N-total,short:left>1e-6,over:c.max_shares>0&&N>c.max_shares+1e-6}}
function resHtml(a,stake){if(!(stake>0))return'<div class="row meta">Введите сумму ставки в BC Game.</div>';
 const r=plan(a,stake),bcLegs=a.legs.slice(0,-1),pm=a.legs[a.legs.length-1];
 let h=bcLegs.map((l,i)=>`<div class="row"><b>BC Game</b><span>${esc(l.what)} · кф ${l.k}</span><span>${money(r.bc[i])}</span><button class="sm" data-copy="${r.bc[i].toFixed(2)}">копировать</button></div>`).join('');
 h+=`<div class="row"><b>Polymarket</b><span>${esc(pm.what)}</span><span>купить ${r.N.toFixed(1)} акций ≈ ${money(r.spent)}</span><button class="sm" data-copy="${r.N.toFixed(1)}">копировать</button></div>`;
 if(r.short)h+='<div class="row bad">В стакане не хватает объёма на такую сумму — уменьшите ставку.</div>';
 else{h+=`<div class="row sum ${r.profit<0?'bad':''}">Всего ${money(r.total)} · при любом исходе вернётся ${money(r.N)} · прибыль ${money(r.profit)} (${(100*r.profit/r.total).toFixed(2)}%)</div>`;
  if(r.over)h+='<div class="row bad">Сумма больше объёма вилки: часть акций купится по худшей цене (это уже учтено в расчёте).</div>'}
 return h}
function headHtml(a,isArb){return `<span class="pct">${a.pct>0?'+':''}${a.pct.toFixed(2)}%</span>
<span class="match">${esc(a.match)}</span><span class="meta">${esc(a.sport)} · ${esc(a.when)}</span>${isArb&&a.since?`<span class="age">${ageText(a)}</span>`:''}${a.suspicious?'<span class="flag">проверьте вручную — слишком большая доходность</span>':''}${a.weak?`<span class="flag">матч сопоставлен по одной команде — на Polymarket: ${esc(a.pm_match)}</span>`:''}`}
function legsHtml(a){return a.legs.map(l=>`<div class="leg"><b>${esc(l.site)}</b><span>${esc(l.what)}</span><span>${esc(l.price)}</span></div>`).join('')}
function makeCard(a,isArb){const el=document.createElement('div');el.dataset.id=a.id;
 if(!stakes.has(a.id))stakes.set(a.id,Math.max(1,Math.round(a.legs[0].stake)));
 el.innerHTML=`<div class="top"></div><div class="legs"></div>
<div class="calc"><label>Ставка в BC Game${a.legs.length>2?' (первое плечо)':''}, $ <input type="number" min="1" step="1" value="${stakes.get(a.id)}"><span class="meta">сначала BC Game — он может отклонить или урезать ставку; затем Polymarket</span></label><div class="res"></div></div>
<div class="meta vol"></div><div class="foot"><a class="bc" target="_blank" rel="noopener">Открыть BC Game</a><a class="pm" target="_blank" rel="noopener">Открыть Polymarket</a><span class="sp"></span>
<button class="sm" data-hide="market">Скрыть эту связку</button><button class="sm" data-hide="match">Скрыть матч</button><button class="sm" data-hide="sport">Скрыть вид спорта</button></div>`;
 el.querySelector('input').addEventListener('input',e=>{stakes.set(a.id,parseFloat(e.target.value));el.querySelector('.res').innerHTML=resHtml(el._a,stakes.get(a.id))});
 el.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;const cur=el._a;
  if(b.dataset.copy)copy(b.dataset.copy,b);
  else if(b.dataset.hide==='match')post('/hide',{kind:'match',value:cur.bc_id,label:cur.match});
  else if(b.dataset.hide==='sport')post('/hide',{kind:'sport',value:cur.sport,label:cur.sport});
  else if(b.dataset.hide==='market')post('/hide',{kind:'market',value:cur.id,label:cur.match+': '+cur.legs[cur.legs.length-1].what})});
 return el}
function fill(el,a,isArb,fresh){el._a=a;el.className='card'+(isArb?' arb':'')+(isArb&&!a.announced?' young':'')+(fresh?' fresh':'');
 el.querySelector('.top').innerHTML=headHtml(a,isArb);el.querySelector('.legs').innerHTML=legsHtml(a);
 // пока идёт нажатие кнопки внутри расчёта, не перерисовываем его — иначе кнопка «уйдёт из-под мыши»
 const res=el.querySelector('.res');if(!res.matches(':hover'))res.innerHTML=resHtml(a,stakes.get(a.id));
 el.querySelector('.vol').textContent=a.volume;el.querySelector('.bc').href=a.bc_url;el.querySelector('.pm').href=a.pm_url}
function sync(box,list,isArb,fresh){const have=new Map([...box.children].map(c=>[c.dataset.id,c])),ids=new Set(list.map(a=>a.id));
 for(const [id,el] of have)if(!ids.has(id)){el.remove();have.delete(id)}
 const busy=box.contains(document.activeElement)&&document.activeElement.tagName==='INPUT';
 list.forEach((a,i)=>{let el=have.get(a.id);if(!el){el=makeCard(a,isArb);box.appendChild(el)}
  fill(el,a,isArb,fresh.has(a.id));
  if(!busy&&box.children[i]!==el)box.insertBefore(el,box.children[i])})}   // порядок не трогаем, пока вводят сумму
function hiddenBar(h){const items=[];for(const kind of ['sport','match','market'])for(const x of (h&&h[kind])||[])items.push([kind,x]);
 const box=$('hidden');box.hidden=!items.length;if(!items.length)return;
 box.innerHTML='Скрыто: '+items.map(([k,x],i)=>`<span class="chip">${esc(x.label||x.value)}<button data-i="${i}" title="Вернуть">×</button></span>`).join('')+'<button class="sm" data-all="1">Показать всё</button>';
 box.onclick=e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.all)post('/unhide',{});else{const [k,x]=items[+b.dataset.i];post('/unhide',{kind:k,value:x.value})}}}
function render(d){last=d;const now=Date.now(),fresh=new Set();
 for(const a of d.arbs){if(!a.announced)continue;const prev=seen.get(a.id);if(prev===undefined||now-prev>60000){fresh.add(a.id);
   notify(`Вилка ${a.pct>0?'+':''}${a.pct.toFixed(2)}%: ${a.match}`,a.legs.map(l=>`${l.site}: ${l.what} — ${l.price}`).join('\n')+'\n'+a.volume,a.bc_url)}
  seen.set(a.id,now)}
 sync($('arbs'),d.arbs,true,fresh);const ready=d.arbs.filter(a=>a.announced).length;
 $('noarbs').hidden=d.arbs.length>0;$('noarbs').textContent=`Сейчас вилок от ${d.min_profit}% нет.`;
 $('nearwrap').hidden=!d.near.length;sync($('near'),d.near,false,fresh);hiddenBar(d.hidden);
 const bl=Object.entries(d.bc||{}).map(([m,b])=>`BC ${m==='live'?'лайв':'прематч'} ${b.error?'ошибка':(b.lag==null?'…':b.lag.toFixed(1)+' с')}`).join(' · ');
 const ws=d.ws&&d.ws.on?` · Polymarket ${d.ws.alive?Math.max(d.ws.lag_ms,0).toFixed(0)+' мс':'переподключение'}`:'';
 $('st').textContent=`${d.mode} · ${d.time} · матчей ${d.matched} · связок ${d.cands} · отставание: ${bl}${ws}`+(d.hold?` · сигнал о вилке, когда она держится ${d.hold} с`:'');
 document.title=(ready?`(${ready}) `:'')+'Вилки BC Game + Polymarket'}
function connect(){const es=new EventSource('/events');es.onopen=()=>$('dot').classList.add('on');
 es.onmessage=e=>render(JSON.parse(e.data));es.onerror=()=>{$('dot').classList.remove('on');$('st').textContent='нет связи со сканером — он запущен?'}}
perm();connect();
</script></body></html>
"""


class Hub:
    def __init__(self):
        self.clients: set[queue.Queue] = set()
        self.last: str | None = None
        self.lock = threading.Lock()
        self.on_command = None      # on_command(path, dict) — команды со страницы

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

        def do_POST(self):
            # Команды принимаем только с самой страницы: чужой сайт, открытый в том же
            # браузере, не должен уметь что-то скрывать.
            if (self.path not in ("/hide", "/unhide")
                    or self.headers.get("Origin") not in origins
                    or "application/json" not in (self.headers.get("Content-Type") or "")):
                return self._empty(403)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
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
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._empty(404)

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass                            # вкладку закрыли посреди запроса — не страшно

    srv = Server(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return hub
