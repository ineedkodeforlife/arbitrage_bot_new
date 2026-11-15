"""Покупка второго плеча на Polymarket по кнопке на странице.

Работает через официальный SDK Polymarket (пакет polymarket-client, Python 3.11+):

    pip install polymarket-client

Включается ключом --pm-trade. Настройки — только через переменные окружения,
чтобы ключ не попадал в историю команд и в файлы проекта:

    ARB_PM_KEY      приватный ключ кошелька-подписанта (обязательно)
    ARB_PM_WALLET   адрес кошелька Polymarket, если он не определяется сам
    ARB_PM_MAX      потолок одной заявки в долларах (по умолчанию 25)

Заявка отправляется только по нажатию кнопки и после подтверждения. Это рыночная
заявка FAK с потолком цены: исполняется сразу то, что есть в стакане не дороже
потолка, остаток отменяется — в стакане ничего не остаётся висеть.
"""
from __future__ import annotations

import os
import threading
import time
from decimal import ROUND_DOWN, Decimal

DEFAULT_MAX = 25.0
REPEAT_GUARD = 5.0          # секунд: защита от двойного нажатия по тому же исходу
LOG_FILE = "orders.log"


class TradeError(Exception):
    """Понятная человеку причина, почему заявка не отправлена."""


def _money(x) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def check(amount, max_price, limit: float) -> tuple[float, float]:
    """Проверить параметры заявки. Возвращает (сумма, потолок цены) или бросает TradeError."""
    try:
        amount, max_price = _money(amount), float(max_price)
    except Exception:
        raise TradeError("сумма или цена заданы неверно")
    if not (amount >= 1.0):
        raise TradeError("сумма меньше $1")
    if amount > limit:
        raise TradeError(f"сумма ${amount:.2f} больше потолка одной заявки ${limit:.2f} "
                         f"(переменная ARB_PM_MAX)")
    if not (0.0 < max_price < 1.0):
        raise TradeError("потолок цены должен быть между 0 и 1")
    return amount, round(max_price, 4)


class Trader:
    def __init__(self, client=None, limit: float | None = None):
        self.limit = float(limit if limit is not None else os.environ.get("ARB_PM_MAX") or DEFAULT_MAX)
        self.client = client
        self.wallet = ""
        self.lock = threading.Lock()
        self._recent: dict[str, float] = {}
        if client is None:
            self._connect()

    def _connect(self) -> None:
        key = os.environ.get("ARB_PM_KEY")
        if not key:
            raise TradeError("не задан ключ: переменная окружения ARB_PM_KEY")
        try:
            from polymarket import SecureClient
        except ImportError:
            raise TradeError("не установлен SDK Polymarket: pip install polymarket-client "
                             "(нужен Python 3.11 или новее)")
        try:
            self.client = SecureClient.create(private_key=key,
                                              wallet=os.environ.get("ARB_PM_WALLET") or None)
        except Exception as e:
            raise TradeError(f"Polymarket не принял ключ или кошелёк: {type(e).__name__}: {e}")
        self.wallet = str(getattr(self.client, "wallet", ""))

    def balance(self) -> float | None:
        """Свободный остаток в долларах; None — узнать не удалось."""
        try:
            b = self.client.get_balance_allowance(asset_type="COLLATERAL")
            return int(b.balance) / 1e6
        except Exception:
            return None

    def buy(self, token: str, amount, max_price, label: str = "") -> dict:
        """Купить исход на сумму amount долларов не дороже max_price за акцию."""
        amount, max_price = check(amount, max_price, self.limit)
        with self.lock:                         # заявки строго по одной
            now = time.time()
            if now - self._recent.get(token, 0) < REPEAT_GUARD:
                raise TradeError("по этому исходу заявка только что отправлена — "
                                 "подождите несколько секунд")
            self._recent[token] = now
            t0 = time.perf_counter()
            try:
                r = self.client.place_market_order(asset_id=token, side="BUY", amount=str(amount),
                                                   max_price=str(max_price), order_type="FAK")
            except Exception as e:
                self._log(label, token, amount, max_price, f"ошибка {type(e).__name__}: {e}")
                raise TradeError(f"заявка не отправлена: {type(e).__name__}: {e}")
            ms = round((time.perf_counter() - t0) * 1000)
        if not getattr(r, "ok", False):
            code, msg = getattr(r, "code", "unknown"), getattr(r, "message", "")
            self._log(label, token, amount, max_price, f"отклонена {code}: {msg}")
            reason = {"fak_not_filled": "по такой цене в стакане уже ничего нет",
                      "fok_not_filled": "по такой цене в стакане уже ничего нет",
                      "not_enough_balance": "на счёте не хватает денег",
                      "market_not_ready": "рынок сейчас не принимает заявки"}.get(code, msg or code)
            return {"ok": False, "error": f"Polymarket отклонил заявку: {reason}"}
        spent, shares = float(r.making_amount), float(r.taking_amount)
        out = {"ok": True, "status": r.status, "order_id": str(r.order_id), "ms": ms,
               "spent": spent, "shares": shares, "asked": amount, "max_price": max_price,
               "price": spent / shares if shares else None}
        self._log(label, token, amount, max_price,
                  f"{r.status}: куплено {shares:g} акций за ${spent:.2f}, заявка {r.order_id}")
        return out

    def _log(self, label: str, token: str, amount: float, max_price: float, result: str) -> None:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{label}\ttoken {token}\t"
                        f"${amount:.2f} не дороже {max_price}\t{result}\n")
        except OSError:
            pass
