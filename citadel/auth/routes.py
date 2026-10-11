"""/login, /api/auth/login, /api/auth/logout, /api/auth/me."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from ..api.security import client_ip
from ..config import settings
from . import passwords
from .middleware import parse_cookies, request_is_https
from .store import new_token

router = APIRouter()
LOGIN_PAGE = Path(__file__).resolve().parents[1] / "web" / "login.html"

GENERIC_FAILURE = "Invalid email or password."
_attempts: dict[str, deque] = defaultdict(deque)     # per client address: when it tried to log in


def _login_allowed(ip: str, now: float | None = None) -> bool:
    """At most `auth_login_attempts_per_5min` login attempts from one address in any five minutes."""
    now = now or time.monotonic()
    q = _attempts[ip]
    while q and now - q[0] > 300:
        q.popleft()
    if len(q) >= settings.auth_login_attempts_per_5min:
        return False
    q.append(now)
    if len(_attempts) > 5000:                        # forget idle clients
        for k in [k for k, v in _attempts.items() if not v or now - v[-1] > 300]:
            _attempts.pop(k, None)
    return True


def _no_store(resp: JSONResponse) -> JSONResponse:
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(LOGIN_PAGE, headers={"Cache-Control": "no-store"})


@router.post("/api/auth/login")
async def login(request: Request):
    store = request.app.state.auth_store
    ip = client_ip(request.scope)
    if not _login_allowed(ip):
        await store.audit("login_rate_limited", ip=ip)
        return _no_store(JSONResponse({"detail": "Too many attempts. Try again in a few minutes."}, status_code=429))
    try:
        body = await request.json()
        email, password = str(body.get("email", "")).strip().lower(), str(body.get("password", ""))
    except Exception:
        return _no_store(JSONResponse({"detail": GENERIC_FAILURE}, status_code=401))
    if not email or len(email) > 254 or not password or len(password) > passwords.MAX_LENGTH:
        return _no_store(JSONResponse({"detail": GENERIC_FAILURE}, status_code=401))

    now = datetime.now(timezone.utc)
    user = await store.get_user_by_email(email)
    if user is None:
        passwords.verify_password(passwords.DUMMY_HASH, password)          # same time as a real check
        await store.audit("login_failed", email=email, ip=ip, detail="unknown user")
        return _no_store(JSONResponse({"detail": GENERIC_FAILURE}, status_code=401))

    usable = user["is_active"] and not (user["expires_at"] and user["expires_at"] <= now)
    locked = bool(user["locked_until"] and user["locked_until"] > now)
    if locked or not usable:
        passwords.verify_password(passwords.DUMMY_HASH, password)
        await store.audit("login_blocked", email=email, user_id=user["id"], ip=ip, detail="locked" if locked else "inactive or expired")
        return _no_store(JSONResponse({"detail": GENERIC_FAILURE}, status_code=401))

    if not passwords.verify_password(user["password_hash"], password):
        just_locked = await store.record_failure(user["id"], settings.auth_max_failed_attempts, settings.auth_lock_minutes)
        await store.audit("account_locked" if just_locked else "login_failed", email=email, user_id=user["id"], ip=ip)
        return _no_store(JSONResponse({"detail": GENERIC_FAILURE}, status_code=401))

    if passwords.needs_rehash(user["password_hash"]):
        await store.update_hash(user["id"], passwords.hash_password(password))
    await store.record_success(user["id"])
    token = new_token()
    lifetime = timedelta(hours=settings.auth_session_hours)
    await store.create_session(user["id"], token, now + lifetime, ip, request.headers.get("user-agent", ""))
    await store.audit("login_ok", email=email, user_id=user["id"], ip=ip)
    await store.purge_sessions()

    resp = _no_store(JSONResponse({"email": email, "role": user["role"]}))
    resp.set_cookie(settings.auth_cookie_name, token, max_age=int(lifetime.total_seconds()), httponly=True,
                    samesite="lax", secure=request_is_https(request.scope), path="/")
    return resp


@router.post("/api/auth/logout")
async def logout(request: Request):
    store = request.app.state.auth_store
    token = parse_cookies(request.scope).get(settings.auth_cookie_name)
    if token:
        session = await store.get_session(token)
        await store.revoke_session(token)
        if session:
            await store.audit("logout", email=session["email"], ip=client_ip(request.scope))
    resp = _no_store(JSONResponse({"ok": True}))
    resp.delete_cookie(settings.auth_cookie_name, path="/")
    return resp


@router.get("/api/auth/me")
async def me(request: Request):
    user = request.scope.get("state", {}).get("user")
    if not settings.auth_enabled:
        return {"auth_enabled": False}
    return _no_store(JSONResponse({"auth_enabled": True, **(user or {})}))
