"""Стаканы Polymarket (CLOB): разовая загрузка по HTTP и поток изменений по WebSocket.

Поток — главный способ: сервер сам присылает каждое изменение стакана, задержка —
десятки миллисекунд. HTTP нужен для первого заполнения и для контрольной сверки.
"""
from __future__ import annotations

import json
import socket
import threading
import time

from .http import POOL, fetch_json
from .ws import WebSocket

CLOB = "https://clob.polymarket.com"
WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PER_CONN = 400      # токенов на одно соединение: при больших подписках сервер
                    # присылает начальные стаканы не по всем токенам
PING_EVERY = 8.0


def _levels(raw) -> dict:
    out = {}
    for lvl in raw or []:
        try:
            size = float(lvl["size"])
            if size > 0:
                out[float(lvl["price"])] = size
        except (KeyError, TypeError, ValueError):
            pass
    return out


class Books:
    """Продажные стороны стаканов: token -> [(цена, объём)] по возрастанию цены."""

    def __init__(self, on_change=None):
        self.on_change = on_change      # on_change(set токенов) — из потоков соединений
        self._asks: dict[str, dict] = {}
        self._sorted: dict[str, list] = {}
        self.lock = threading.Lock()
        self.conns: list[_Conn] = []
        self.subscribed: set[str] = set()
        self.last_msg = 0.0             # когда пришло последнее сообщение потока
        self.lag_ms = 0.0               # сглаженная задержка «биржа -> мы» по меткам времени
        self.messages = 0
        self.streaming = False

    # --- чтение ---
    def asks(self, token: str) -> list:
        s = self._sorted.get(token)
        if s is None:
            with self.lock:             # поток соединения может менять стакан прямо сейчас
                d = self._asks.get(token)
                if d is None:
                    return []
                s = self._sorted[token] = sorted(d.items())
        return s

    def known(self, token: str) -> bool:
        return token in self._asks

    # --- HTTP ---
    def load(self, tokens, chunk: int = 100, timeout: float = 10) -> set:
        """Скачать стаканы целиком. Возвращает токены, у которых стакан изменился."""
        ids = list(dict.fromkeys(tokens))
        parts = [ids[i:i + chunk] for i in range(0, len(ids), chunk)]
        results = list(POOL.map(
            lambda part: fetch_json(f"{CLOB}/books", body=[{"token_id": t} for t in part],
                                    timeout=timeout), parts))
        changed, got = set(), set()
        with self.lock:
            for books in results:
                for b in books or []:
                    tok = str(b.get("asset_id"))
                    got.add(tok)
                    if self._set(tok, _levels(b.get("asks"))):
                        changed.add(tok)
            for tok in ids:                 # стакана нет вовсе — значит, он пуст
                if tok not in got and self._set(tok, {}):
                    changed.add(tok)
        return changed

    def _set(self, tok: str, levels: dict) -> bool:
        if self._asks.get(tok) == levels:
            return False
        self._asks[tok] = levels
        self._sorted.pop(tok, None)
        return True

    # --- поток ---
    def subscribe(self, tokens) -> None:
        """Подписаться на новые токены (уже подписанные пропускаются)."""
        new = [t for t in dict.fromkeys(tokens) if t not in self.subscribed]
        if not new:
            return
        self.streaming = True
        self.subscribed.update(new)
        for c in self.conns:
            room = PER_CONN - len(c.tokens)
            if room > 0 and new:
                c.add(new[:room])
                new = new[room:]
        while new:
            c = _Conn(self, new[:PER_CONN])
            self.conns.append(c)
            new = new[PER_CONN:]

    def alive(self) -> bool:
        return bool(self.conns) and all(c.connected for c in self.conns)

    def _handle(self, text: str) -> None:
        if text == "PONG" or not text or text[0] not in "[{":
            return
        try:
            data = json.loads(text)
        except ValueError:
            return
        now = time.time()
        self.last_msg = now
        self.messages += 1
        changed = set()
        with self.lock:
            for m in data if isinstance(data, list) else (data,):
                kind = m.get("event_type")
                if kind == "book":
                    tok = str(m.get("asset_id"))
                    if self._set(tok, _levels(m.get("asks"))):
                        changed.add(tok)
                elif kind == "price_change":
                    for ch in m.get("price_changes") or ():
                        if ch.get("side") != "SELL":
                            continue
                        tok = str(ch.get("asset_id"))
                        book = self._asks.get(tok)
                        if book is None:
                            book = self._asks[tok] = {}
                        try:
                            price, size = float(ch["price"]), float(ch["size"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if size > 0:
                            if book.get(price) == size:
                                continue
                            book[price] = size
                        elif book.pop(price, None) is None:
                            continue
                        self._sorted.pop(tok, None)
                        changed.add(tok)
                    ts = m.get("timestamp")
                    if ts:
                        try:
                            lag = now * 1000 - float(ts)
                            self.lag_ms += 0.05 * (lag - self.lag_ms)
                        except ValueError:
                            pass
        if changed and self.on_change:
            self.on_change(changed)


class _Conn:
    """Одно соединение WebSocket со своим потоком; само переподключается."""

    def __init__(self, books: Books, tokens: list):
        self.books = books
        self.tokens = list(tokens)
        self.ws: WebSocket | None = None
        self.connected = False
        self.error = None
        self._pending: list = []
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, tokens: list) -> None:
        with self._lock:
            self.tokens.extend(tokens)
            ws = self.ws if self.connected else None
        if ws:
            try:
                ws.send(json.dumps({"assets_ids": tokens, "operation": "subscribe"}))
            except Exception:
                pass                    # соединение рвётся — при переподключении подпишемся на всё

    def _run(self) -> None:
        delay = 0.5
        while True:
            try:
                ws = WebSocket(WSS, timeout=15)
                with self._lock:
                    ws.send(json.dumps({"assets_ids": self.tokens, "type": "market"}))
                    self.ws, self.connected = ws, True
                self.error = None
                delay = 0.5
                ws.settimeout(PING_EVERY)
                last_ping = time.time()
                while True:
                    try:
                        self.books._handle(ws.recv())
                    except socket.timeout:
                        pass
                    if time.time() - last_ping >= PING_EVERY:
                        ws.send("PING")
                        last_ping = time.time()
            except Exception as e:
                self.error = e
            self.connected = False
            try:
                if self.ws:
                    self.ws.close()
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 2, 10)
