"""Локальная страница со списком вилок и мгновенными уведомлениями в браузере.

Сканер после каждого прохода отдаёт результат открытым вкладкам через
Server-Sent Events — без опроса и без внешних сервисов, задержка в миллисекундах.
Страница доступна только с этого компьютера (127.0.0.1).
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
:root{--bg:#f6f7f9;--card:#fff;--text:#15181d;--muted:#667085;--line:#e3e6eb;--good:#0a7d3c;--goodbg:#e6f6ec;--warn:#b54708;--accent:#2457d6}
@media (prefers-color-scheme:dark){:root{--bg:#101216;--card:#191c22;--text:#e8eaee;--muted:#98a2b3;--line:#2a2f38;--good:#4cd787;--goodbg:#12301f;--warn:#f5a25d;--accent:#7da2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;justify-content:space-between;margin-bottom:14px}
h1{font-size:20px;margin:0}h2{font-size:14px;font-weight:600;color:var(--muted);margin:26px 0 8px;text-transform:uppercase;letter-spacing:.04em}
button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.bar{display:flex;gap:8px;flex-wrap:wrap}
#status{color:var(--muted);font-size:13px;margin-bottom:6px;font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#c33;margin-right:6px;vertical-align:1px}.dot.on{background:var(--good)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px}
.card.arb{border-color:var(--good)}.card.fresh{animation:flash 2.5s}
@keyframes flash{0%{background:var(--goodbg)}100%{background:var(--card)}}
.top{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
.pct{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--muted)}.arb .pct{color:var(--good)}
.match{font-weight:600}.meta{color:var(--muted);font-size:13px}.flag{color:var(--warn);font-size:13px}
.legs{margin:10px 0 6px;display:grid;gap:4px}.leg{display:grid;grid-template-columns:92px 1fr auto;gap:10px;font-variant-numeric:tabular-nums}
.leg b{font-weight:600}.leg span:last-child{white-space:nowrap}
.links a{color:var(--accent);margin-right:14px;font-size:13px}.empty{color:var(--muted);padding:14px 2px}
@media (max-width:560px){.leg{grid-template-columns:1fr;gap:0;margin-bottom:6px}}
</style></head><body><main>
<header><h1>Вилки BC Game + Polymarket</h1>
<div class="bar"><button id="enable" class="primary">Включить уведомления и звук</button><button id="test">Проверить</button></div></header>
<div id="status"><span class="dot" id="dot"></span><span id="st">подключение…</span></div>
<h2>Вилки</h2><div id="arbs"><div class="empty">Ждём первый проход сканера…</div></div>
<div id="nearwrap" hidden><h2>Ближайшие к вилке</h2><div id="near"></div></div>
</main><script>
const $=id=>document.getElementById(id);let ctx=null,seen=new Map(),first=true;
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
function card(a,isArb,fresh){return `<div class="card ${isArb?'arb':''} ${fresh?'fresh':''}"><div class="top"><span class="pct">${a.pct>0?'+':''}${a.pct.toFixed(2)}%</span>
<span class="match">${esc(a.match)}</span><span class="meta">${esc(a.sport)} · ${esc(a.when)}</span>${a.suspicious?'<span class="flag">проверьте вручную — слишком большая доходность</span>':''}</div>
<div class="legs">${a.legs.map(l=>`<div class="leg"><b>${esc(l.site)}</b><span>${esc(l.what)}</span><span>${esc(l.price)} · $${l.stake.toFixed(2)}</span></div>`).join('')}</div>
<div class="meta">${esc(a.volume)}</div><div class="links"><a href="${esc(a.bc_url)}" target="_blank" rel="noopener">Открыть BC Game</a><a href="${esc(a.pm_url)}" target="_blank" rel="noopener">Открыть Polymarket</a></div></div>`}
function render(d){const now=Date.now(),fresh=new Set();
 for(const a of d.arbs){const last=seen.get(a.id);if(last===undefined||now-last>60000){fresh.add(a.id);
   notify(`Вилка ${a.pct>0?'+':''}${a.pct.toFixed(2)}%: ${a.match}`,a.legs.map(l=>`${l.site}: ${l.what} — ${l.price}`).join('\n')+'\n'+a.volume,a.bc_url)}
  seen.set(a.id,now)}
 first=false;
 $('arbs').innerHTML=d.arbs.length?d.arbs.map(a=>card(a,true,fresh.has(a.id))).join(''):`<div class="empty">Сейчас вилок от ${d.min_profit}% нет.</div>`;
 $('nearwrap').hidden=!d.near.length;$('near').innerHTML=d.near.map(a=>card(a,false,false)).join('');
 $('st').textContent=`${d.mode} · проход в ${d.time} за ${d.ms} мс · совпало матчей ${d.matched} · связок ${d.cands}`;
 document.title=(d.arbs.length?`(${d.arbs.length}) `:'')+'Вилки BC Game + Polymarket'}
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

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):          # не засоряем консоль сканера
            pass

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
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return hub
