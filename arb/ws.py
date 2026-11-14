"""Минимальный клиент WebSocket (RFC 6455) на стандартной библиотеке.

Умеет ровно то, что нужно для потока стаканов Polymarket: подключиться по wss
(напрямую или через HTTP-прокси), отправлять текстовые сообщения и читать
входящие, включая фрагментированные. Сжатие не запрашивается.
"""
from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlsplit


class WsClosed(ConnectionError):
    pass


class WebSocket:
    def __init__(self, url: str, *, proxy: tuple[str, int] | None = None,
                 timeout: float = 20, headers: dict | None = None):
        u = urlsplit(url)
        host, port = u.hostname, u.port or 443
        raw = socket.create_connection(proxy or (host, port), timeout=timeout)
        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if proxy:
                raw.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
                status = self._read_head(raw)
                if b" 200" not in status.split(b"\r\n", 1)[0]:
                    raise ConnectionError(f"прокси отказал: {status[:80]!r}")
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket",
                 "Connection: Upgrade", f"Sec-WebSocket-Key: {key}",
                 "Sec-WebSocket-Version: 13", "Origin: https://polymarket.com",
                 "User-Agent: Mozilla/5.0"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = self._read_head(self.sock)
        if b" 101" not in head.split(b"\r\n", 1)[0]:
            self.sock.close()
            raise ConnectionError(f"сервер не принял WebSocket: {head[:120]!r}")
        self._buf = b""
        self._parts: list = []
        self._wlock = threading.Lock()
        self.closed = False

    @staticmethod
    def _read_head(sock) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(1)        # побайтно, чтобы не съесть первый кадр
            if not chunk:
                raise ConnectionError("соединение закрыто во время рукопожатия")
            data += chunk
        return data

    def settimeout(self, t: float | None) -> None:
        self.sock.settimeout(t)

    def _send(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        # XOR с маской одним действием над большими целыми — быстрее цикла по байтам
        full = (mask * (n // 4 + 1))[:n]
        body = (int.from_bytes(payload, "big") ^ int.from_bytes(full, "big")).to_bytes(n, "big")
        with self._wlock:
            self.sock.sendall(head + mask + body)

    def send(self, text: str) -> None:
        self._send(0x1, text.encode())

    def _frame(self):
        """Разобрать один кадр из буфера. None — кадр ещё не пришёл целиком
        (буфер при этом не трогаем, чтобы тайм-аут чтения ничего не ломал)."""
        buf = self._buf
        if len(buf) < 2:
            return None
        b1, b2 = buf[0], buf[1]
        n, pos = b2 & 0x7F, 2
        if n == 126:
            if len(buf) < 4:
                return None
            n, pos = struct.unpack(">H", buf[2:4])[0], 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n, pos = struct.unpack(">Q", buf[2:10])[0], 10
        mask = None
        if b2 & 0x80:
            if len(buf) < pos + 4:
                return None
            mask, pos = buf[pos:pos + 4], pos + 4
        if len(buf) < pos + n:
            return None
        data = buf[pos:pos + n]
        self._buf = buf[pos + n:]
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return b1 & 0x80, b1 & 0x0F, data

    def recv(self) -> str:
        """Следующее текстовое сообщение. Бросает socket.timeout / WsClosed;
        после тайм-аута можно просто вызвать recv() ещё раз."""
        while True:
            frame = self._frame()
            if frame is None:
                chunk = self.sock.recv(262144)
                if not chunk:
                    raise WsClosed("сервер закрыл соединение")
                self._buf += chunk
                continue
            fin, opcode, data = frame
            if opcode == 0x8:
                self.closed = True
                raise WsClosed(f"сервер закрыл соединение: {data[2:].decode(errors='replace')}")
            if opcode == 0x9:
                self._send(0xA, data)
                continue
            if opcode == 0xA:
                continue
            self._parts.append(data)
            if fin:
                out = b"".join(self._parts).decode("utf-8", errors="replace")
                self._parts = []
                return out

    def close(self) -> None:
        self.closed = True
        try:
            self._send(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
