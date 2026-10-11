"""Keeps everything behind a login: every page, API route, static file and websocket needs a live session,
except a short allow-list (the login page and its two assets, the login/logout calls, the health check).

Pure ASGI (like api/security.py) so websockets pass through the same gate. Default-deny: a route added
later is protected without anyone remembering to protect it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

logger = logging.getLogger("citadel.auth")

PUBLIC_PATHS = {"/login", "/api/auth/login", "/api/auth/logout", "/api/health"}
PUBLIC_PREFIXES = ("/static/login.",)          # login.js, login.css
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def parse_cookies(scope) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k:
                    out[k] = v
    return out


def header(scope, name: bytes) -> str:
    for n, v in scope.get("headers", []):
        if n == name:
            return v.decode("latin-1")
    return ""


def request_is_https(scope) -> bool:
    if scope.get("scheme") in ("https", "wss"):
        return True
    return header(scope, b"x-forwarded-proto").split(",")[0].strip() == "https"


def safe_next(value: str | None) -> str:
    """Where to send the person after login: a path on this site only, never another host."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value or "\n" in value or "\r" in value:
        return "/"
    return value


def origin_allowed(scope) -> bool:
    """A browser request that names a different site as its Origin is not ours (cross-site request forgery,
    or a cross-site websocket). No Origin at all is not a browser page, which has no cookie to abuse."""
    origin = header(scope, b"origin")
    if not origin:
        return True
    host = header(scope, b"host").lower()
    return bool(host) and urlparse(origin).netloc.lower() == host


class AuthMiddleware:
    def __init__(self, app, enabled: bool = False, cookie_name: str = "citadel_session",
                 idle_seconds: int = 1800, touch_every: int = 60) -> None:
        self.app = app
        self.enabled = enabled
        self.cookie_name = cookie_name
        self.idle_seconds = idle_seconds
        self.touch_every = touch_every

    def _is_public(self, path: str) -> bool:
        return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)

    async def _session(self, scope):
        token = parse_cookies(scope).get(self.cookie_name)
        store = getattr(scope["app"].state, "auth_store", None)
        if not token or store is None:
            return None, None
        session = await store.get_session(token)
        if session is None:
            return None, token
        now = datetime.now(timezone.utc)
        idle = (now - session["last_seen_at"]).total_seconds()
        if idle > self.idle_seconds:
            await store.revoke_session(token)
            await store.audit("session_idle_timeout", email=session["email"])
            return None, token
        if idle > self.touch_every:
            await store.touch_session(token)
        return session, token

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if not self.enabled or kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        if kind == "websocket":
            if not origin_allowed(scope):
                return await self._close(receive, send)
            session, _ = await self._session(scope)
            if session is None:
                return await self._close(receive, send)
            scope.setdefault("state", {})["user"] = {"email": session["email"], "role": session["role"]}
            return await self.app(scope, receive, send)

        method, path = scope["method"], scope["path"]
        if method not in SAFE_METHODS and not origin_allowed(scope):
            return await self._json(send, 403, "cross-site request refused")
        if self._is_public(path):
            return await self.app(scope, receive, send)

        session, _ = await self._session(scope)
        if session is None:
            wants_page = method in ("GET", "HEAD") and not path.startswith("/api/") and "text/html" in header(scope, b"accept")
            if wants_page:
                target = path + (("?" + scope["query_string"].decode("latin-1")) if scope.get("query_string") else "")
                return await self._redirect(send, "/login?next=" + quote(safe_next(target), safe="/"))
            return await self._json(send, 401, "login required")
        scope.setdefault("state", {})["user"] = {"email": session["email"], "role": session["role"]}
        return await self.app(scope, receive, send)

    async def _close(self, receive, send):
        await receive()                                    # the "websocket.connect"; closing before accept rejects it
        await send({"type": "websocket.close", "code": 1008})

    async def _json(self, send, status: int, detail: str):
        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    async def _redirect(self, send, location: str):
        await send({"type": "http.response.start", "status": 302, "headers": [
            (b"location", location.encode()), (b"content-length", b"0"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b""})
