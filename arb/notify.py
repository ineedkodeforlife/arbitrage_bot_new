"""Уведомления о вилках в Telegram.

Нужен свой бот: напишите @BotFather, создайте бота и получите токен; затем напишите
боту любое сообщение и узнайте свой chat_id (например, у @userinfobot).
Токен и chat_id передаются переменными окружения ARB_TG_TOKEN и ARB_TG_CHAT
(или ключами --tg-token / --tg-chat). Сообщения уходят только в этот чат.
"""
from __future__ import annotations

import queue
import threading

from . import http
from .http import fetch_json


def message(d: dict) -> str:
    lines = [f"Вилка {d['pct']:+.2f}% · {d['match']}", f"{d['sport']} · {d['when']}"]
    if d["suspicious"]:
        lines.append("[!] слишком большая доходность — проверьте вручную")
    if d["weak"]:
        lines.append(f"[!] матч сопоставлен по одной команде, на Polymarket: {d['pm_match']}")
    for leg in d["legs"]:
        amt = (f"{leg['shares']:g} акций ≈ ${leg['stake']:,.2f}" if "shares" in leg
               else f"${leg['stake']:,.0f}")
        lines.append(f"{leg['site']}: {leg['what']} — {leg['price']} — {amt}")
    lines += [d["volume"], d["bc_url"], d["pm_url"]]
    return "\n".join(lines)


class Telegram:
    def __init__(self, token: str, chat: str):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat = chat
        self.error = None
        self._proxy = None
        self._q: queue.Queue = queue.Queue(maxsize=50)
        threading.Thread(target=self._run, daemon=True).start()

    def _post(self, text: str) -> None:
        body = {"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": True}
        # Telegram из некоторых сетей доступен только через прокси — пробуем оба пути
        # и запоминаем сработавший.
        ways = [self._proxy] if self._proxy is not None else [http.detect_proxy() or False, False]
        last = None
        for way in dict.fromkeys(ways):
            try:
                fetch_json(self.url, body=body, timeout=8, retries=0, proxy=way or None)
                self._proxy = way
                return
            except Exception as e:
                last = e
                if ": HTTP " in str(e):     # Telegram ответил отказом (неверный токен или
                    break                   # chat_id) — другой путь не поможет
        raise last

    def check(self) -> str | None:
        """Отправить пробное сообщение сразу. Возвращает текст ошибки или None."""
        try:
            self._post("Сканер вилок запущен. Сюда будут приходить уведомления.")
            return None
        except Exception as e:
            # токен в адресе запроса — в текст ошибки он попасть не должен
            return str(e).replace(self.url, "api.telegram.org/…/sendMessage")

    def send(self, d: dict) -> None:
        try:
            self._q.put_nowait(message(d))
        except queue.Full:          # сеть не успевает — пропускаем, не копим очередь
            pass

    def _run(self) -> None:
        while True:
            text = self._q.get()
            try:
                self._post(text)
                self.error = None
            except Exception as e:
                self.error = str(e).replace(self.url, "api.telegram.org/…/sendMessage")
