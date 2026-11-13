"""HTTP-клиент на стандартной библиотеке с постоянными соединениями.

Главное для скорости: соединение с каждым хостом открывается один раз на поток
и переиспользуется (keep-alive), поэтому повторные запросы не тратят время на
TCP- и TLS-рукопожатие (обычно 150-400 мс на запрос).
"""
from __future__ import annotations

import gzip
import http.client
import json
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

_local = threading.local()
# Общий пул потоков: потоки живут всё время работы, вместе с их соединениями.
POOL = ThreadPoolExecutor(8)


def _conn(host: str, timeout: float) -> http.client.HTTPSConnection:
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = _local.pool = {}
    c = pool.get(host)
    if c is None:
        c = pool[host] = http.client.HTTPSConnection(host, timeout=timeout)
    c.timeout = timeout
    if c.sock is not None:
        c.sock.settimeout(timeout)
    return c


def _drop(host: str) -> None:
    c = getattr(_local, "pool", {}).pop(host, None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def fetch_json(url: str, *, body=None, headers: dict | None = None,
               timeout: float = 40, retries: int = 3):
    u = urlsplit(url)
    path = u.path + (f"?{u.query}" if u.query else "")
    h = {"User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "gzip",
         "Connection": "keep-alive"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        h["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries + 1):
        try:
            c = _conn(u.netloc, timeout)
            c.request("POST" if data is not None else "GET", path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
            if r.status == 200:
                if r.getheader("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
            last = f"HTTP {r.status}"
            if r.status not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"{url}: {last} {raw[:200]!r}")
            time.sleep(0.5 * (attempt + 1))
        except (http.client.HTTPException, OSError) as e:
            # соединение закрыто сервером — открываем заново, первый повтор без паузы
            last = e
            _drop(u.netloc)
            if attempt:
                time.sleep(0.5 * attempt)
    raise RuntimeError(f"не удалось получить {url}: {last}")


CURL = shutil.which("curl")


def curl_json(url: str, *, headers: dict | None = None, timeout: float = 10, retries: int = 2):
    """GET через системный curl.

    Нужен для хоста линии BC Game: при подключении из Python поток с этого CDN
    может замирать после первых 16 КБ ответа, а curl читает его без проблем.
    """
    if not CURL:
        raise RuntimeError("не найден curl (в Windows 10/11 он есть в System32)")
    cmd = [CURL, "-sS", "--compressed", "-m", str(timeout), "--connect-timeout", "5"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    last = None
    for _ in range(retries + 1):
        p = subprocess.run(cmd, capture_output=True, creationflags=flags)
        if p.returncode == 0 and p.stdout:
            try:
                return json.loads(p.stdout)
            except ValueError as e:
                last = f"не JSON: {p.stdout[:120]!r} ({e})"
        else:
            last = p.stderr.decode(errors="replace").strip() or f"код {p.returncode}"
    raise RuntimeError(f"не удалось получить {url}: {last}")
