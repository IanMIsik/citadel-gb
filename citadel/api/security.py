"""Baseline web hardening: response headers, request-rate and websocket caps, DB-credential check.

Pure ASGI middleware (not BaseHTTPMiddleware) so websockets and streaming responses are untouched.
"""
from __future__ import annotations

import ipaddress
import logging
import time
from urllib.parse import urlparse

logger = logging.getLogger("citadel.security")

# The pages load Chart.js from jsDelivr and one webfont from Google Fonts, and use a few inline
# style="" attributes (hence style-src 'unsafe-inline'); there are no inline scripts or handlers.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
HSTS = (b"strict-transport-security", b"max-age=31536000")


def client_ip(scope) -> str:
    """The caller's address. Behind our own reverse proxy (a private/loopback peer) the rightmost
    X-Forwarded-For entry is the one the proxy appended; anywhere else the header is ignorable
    because the client could have written it."""
    peer = (scope.get("client") or ("unknown", 0))[0]
    try:
        trusted = ipaddress.ip_address(peer).is_private
    except ValueError:
        trusted = False
    if trusted:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                last = value.decode("latin-1").split(",")[-1].strip()
                if last:
                    return last
    return peer


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        https = scope.get("scheme") == "https" or any(
            n == b"x-forwarded-proto" and v.split(b",")[0].strip() == b"https" for n, v in scope.get("headers", []))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                present = {n for n, _ in message.get("headers", [])}
                extra = [h for h in SECURITY_HEADERS if h[0] not in present]
                if https:
                    extra.append(HSTS)
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class GuardMiddleware:
    """Per-client request rate limit on /api/* and a cap on concurrent websockets, so one caller
    cannot tie up the recompute-backed endpoints or the broadcast sockets. 0 disables a limit."""

    def __init__(self, app, requests_per_minute: int = 600, ws_max_per_ip: int = 20):
        self.app = app
        self.rpm = requests_per_minute
        self.ws_max = ws_max_per_ip
        self._windows: dict[str, tuple[float, int]] = {}
        self._ws_open: dict[str, int] = {}

    def _allow(self, ip: str, now: float) -> tuple[bool, int]:
        start, count = self._windows.get(ip, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        count += 1
        self._windows[ip] = (start, count)
        if len(self._windows) > 10_000:  # forget idle clients
            self._windows = {k: v for k, v in self._windows.items() if now - v[0] < 60}
        return count <= self.rpm, max(1, int(60 - (now - start)))

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "http" and self.rpm and scope["path"].startswith("/api/"):
            ok, retry = self._allow(client_ip(scope), time.monotonic())
            if not ok:
                body = b'{"detail":"too many requests"}'
                await send({"type": "http.response.start", "status": 429, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(retry).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        elif kind == "websocket" and self.ws_max:
            ip = client_ip(scope)
            if self._ws_open.get(ip, 0) >= self.ws_max:
                await receive()  # the "websocket.connect" event; closing before accept rejects the handshake
                await send({"type": "websocket.close", "code": 1008})
                return
            self._ws_open[ip] = self._ws_open.get(ip, 0) + 1
            try:
                return await self.app(scope, receive, send)
            finally:
                left = self._ws_open.get(ip, 1) - 1
                if left > 0:
                    self._ws_open[ip] = left
                else:
                    self._ws_open.pop(ip, None)
        await self.app(scope, receive, send)


_LOCAL_DB_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres"}


def check_db_credentials(database_url: str) -> None:
    """The shipped default login (citadel/citadel) is only acceptable for a database on this machine
    or inside the compose network. Anywhere else, refuse to start; on the compose network, warn."""
    u = urlparse(database_url)
    if u.password != "citadel":
        return
    if (u.hostname or "") not in _LOCAL_DB_HOSTS:
        raise RuntimeError("DATABASE_URL uses the default 'citadel' password on a non-local host; set a strong password")
    logger.warning("database is using the default 'citadel' password -- fine on a developer machine, never on a server")
