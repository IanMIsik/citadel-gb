"""Sign-in and account routes: /login, /api/auth/*, /account, /admin and /api/admin/*, and /demo/<token>."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from ..api.security import client_ip
from ..config import settings
from . import passwords, service, totp
from .middleware import parse_cookies, request_is_https
from .store import ROLES, new_token

router = APIRouter()
WEB = Path(__file__).resolve().parents[1] / "web"

GENERIC_FAILURE = "Invalid email or password."
MAX_CODE_ATTEMPTS = 5
_attempts: dict[str, deque] = defaultdict(deque)     # per client address: when it tried to sign in


def _login_allowed(ip: str, now: float | None = None) -> bool:
    """At most `auth_login_attempts_per_5min` sign-in attempts from one address in any five minutes."""
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


def _json(payload: dict, status: int = 200) -> JSONResponse:
    resp = JSONResponse(payload, status_code=status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _fail(detail: str, status: int = 401) -> JSONResponse:
    return _json({"detail": detail}, status)


def _pending_cookie() -> str:
    return f"{settings.auth_cookie_name}_2fa"


def _required_roles() -> tuple[str, ...]:
    return service.two_factor_roles(settings.auth_2fa_required_roles)


def _is_usable(user: dict, now: datetime) -> bool:
    return bool(user["is_active"]) and not (user["expires_at"] and user["expires_at"] <= now)


def _is_locked(user: dict, now: datetime) -> bool:
    return bool(user["locked_until"] and user["locked_until"] > now)


async def _start_session(request: Request, store, user: dict, lifetime: timedelta, resp: JSONResponse, ip: str) -> None:
    token = new_token()
    await store.create_session(user["id"], token, datetime.now(timezone.utc) + lifetime, ip, request.headers.get("user-agent", ""))
    resp.set_cookie(settings.auth_cookie_name, token, max_age=int(lifetime.total_seconds()), httponly=True,
                    samesite="lax", secure=request_is_https(request.scope), path="/")


# -- pages ------------------------------------------------------------------------------------------------
@router.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(WEB / "login.html", headers={"Cache-Control": "no-store"})


@router.get("/account", include_in_schema=False)
async def account_page():
    return FileResponse(WEB / "account.html", headers={"Cache-Control": "no-store"})


@router.get("/admin", include_in_schema=False)
async def admin_page():
    return FileResponse(WEB / "admin.html", headers={"Cache-Control": "no-store"})


# -- sign in ----------------------------------------------------------------------------------------------
@router.post("/api/auth/login")
async def login(request: Request):
    store = request.app.state.auth_store
    ip = client_ip(request.scope)
    if not _login_allowed(ip):
        await store.audit("login_rate_limited", ip=ip)
        return _fail("Too many attempts. Try again in a few minutes.", 429)
    try:
        body = await request.json()
        email, password = str(body.get("email", "")).strip().lower(), str(body.get("password", ""))
    except Exception:
        return _fail(GENERIC_FAILURE)
    if not email or len(email) > 254 or not password or len(password) > passwords.MAX_LENGTH:
        return _fail(GENERIC_FAILURE)

    now = datetime.now(timezone.utc)
    user = await store.get_user_by_email(email)
    if user is None:
        await asyncio.to_thread(passwords.verify_password, passwords.DUMMY_HASH, password)     # same time as a real check
        await store.audit("login_failed", email=email, ip=ip, detail="unknown user")
        return _fail(GENERIC_FAILURE)
    if _is_locked(user, now) or not _is_usable(user, now):
        await asyncio.to_thread(passwords.verify_password, passwords.DUMMY_HASH, password)
        await store.audit("login_blocked", email=email, user_id=user["id"], ip=ip,
                          detail="locked" if _is_locked(user, now) else "inactive or expired")
        return _fail(GENERIC_FAILURE)
    # argon2 is deliberately slow (tens of ms of CPU): do it off the event loop so a login never stalls the live feeds
    if not await asyncio.to_thread(passwords.verify_password, user["password_hash"], password):
        just_locked = await store.record_failure(user["id"], settings.auth_max_failed_attempts, settings.auth_lock_minutes)
        await store.audit("account_locked" if just_locked else "login_failed", email=email, user_id=user["id"], ip=ip)
        return _fail(GENERIC_FAILURE)
    if passwords.needs_rehash(user["password_hash"]):
        await store.update_hash(user["id"], await asyncio.to_thread(passwords.hash_password, password))

    if user.get("totp_enabled"):
        # Right password; now the code. Nothing but the right to try a code is issued until it is entered.
        pending = new_token()
        await store.create_pending(user["id"], pending, now + timedelta(minutes=settings.auth_pending_minutes))
        await store.audit("password_ok_awaiting_2fa", email=email, user_id=user["id"], ip=ip)
        resp = _json({"needs_totp": True})
        resp.set_cookie(_pending_cookie(), pending, max_age=settings.auth_pending_minutes * 60, httponly=True,
                        samesite="strict", secure=request_is_https(request.scope), path="/api/auth")
        return resp

    await store.record_success(user["id"])
    resp = _json({"email": email, "role": user["role"]})
    await _start_session(request, store, user, timedelta(hours=settings.auth_session_hours), resp, ip)
    await store.audit("login_ok", email=email, user_id=user["id"], ip=ip)
    await store.purge_sessions()
    return resp


@router.post("/api/auth/login/totp")
async def login_totp(request: Request):
    store = request.app.state.auth_store
    ip = client_ip(request.scope)
    if not _login_allowed(ip):
        return _fail("Too many attempts. Try again in a few minutes.", 429)
    pending_token = parse_cookies(request.scope).get(_pending_cookie())
    pending = await store.get_pending(pending_token) if pending_token else None
    if pending is None:
        return _fail("That sign-in has timed out. Start again.")
    try:
        code = str((await request.json()).get("code", "")).strip()
    except Exception:
        return _fail("Invalid code.")
    user = await store.get_user_by_id(pending["user_id"])
    now = datetime.now(timezone.utc)
    if user is None or not _is_usable(user, now) or _is_locked(user, now) or await store.bump_pending(pending_token) > MAX_CODE_ATTEMPTS:
        await store.delete_pending(pending_token)
        return _fail("That sign-in has timed out. Start again.")

    digits = "".join(ch for ch in code if ch.isdigit())
    used_recovery = False
    step = totp.verify(user["totp_secret"], code, user.get("totp_last_step")) if len(digits) == totp.DIGITS and user.get("totp_secret") else None
    if step is not None:
        await store.totp_set_last_step(user["id"], step)
    elif totp.normalise_recovery_code(code) and await store.consume_recovery_code(user["id"], totp.hash_recovery_code(code)):
        used_recovery = True
    else:
        just_locked = await store.record_failure(user["id"], settings.auth_max_failed_attempts, settings.auth_lock_minutes)
        await store.audit("account_locked" if just_locked else "totp_failed", email=user["email"], user_id=user["id"], ip=ip)
        return _fail("Invalid code.")

    await store.delete_pending(pending_token)
    await store.record_success(user["id"])
    resp = _json({"email": user["email"], "role": user["role"]})
    resp.delete_cookie(_pending_cookie(), path="/api/auth")
    await _start_session(request, store, user, timedelta(hours=settings.auth_session_hours), resp, ip)
    await store.audit("login_ok_recovery_code" if used_recovery else "login_ok", email=user["email"], user_id=user["id"], ip=ip,
                      detail=f"{await store.recovery_codes_left(user['id'])} recovery codes left" if used_recovery else None)
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
    resp = _json({"ok": True})
    resp.delete_cookie(settings.auth_cookie_name, path="/")
    return resp


@router.get("/demo/{token}", include_in_schema=False)
async def demo_entry(token: str, request: Request):
    """A demo link: signs in as its read-only demo user, for a few hours, without a password or two-factor."""
    store = request.app.state.auth_store
    ip = client_ip(request.scope)
    link = await store.get_demo_link(token) if _login_allowed(ip) else None
    if link is None:
        await store.audit("demo_link_refused", ip=ip)
        resp = RedirectResponse("/login?demo=expired", status_code=302)
    else:
        remaining = link["expires_at"] - datetime.now(timezone.utc)
        lifetime = min(remaining, timedelta(hours=settings.auth_demo_session_hours))
        resp = RedirectResponse("/", status_code=302)
        await _start_session(request, store, {"id": link["user_id"]}, lifetime, resp, ip)
        await store.touch_demo_link(link["id"])
        await store.audit("demo_link_used", email=link["email"], user_id=link["user_id"], ip=ip, detail=link["label"])
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"       # the secret is in the address; never pass it on
    return resp


# -- the signed-in person's own account -------------------------------------------------------------------------
def _me(request: Request) -> dict | None:
    return request.scope.get("state", {}).get("user")


@router.get("/api/auth/me")
async def me(request: Request):
    if not settings.auth_enabled:
        return {"auth_enabled": False}
    user = _me(request) or {}
    left = await request.app.state.auth_store.recovery_codes_left(user["user_id"]) if user.get("totp_enabled") else None
    return _json({"auth_enabled": True, **{k: user.get(k) for k in ("email", "role", "must_change_password", "totp_enabled", "setup_needed")},
                  "twofa_required": user.get("role") in _required_roles(), "recovery_codes_left": left})


@router.post("/api/auth/change-password")
async def change_password(request: Request):
    store = request.app.state.auth_store
    me = _me(request)
    try:
        body = await request.json()
        current, new = str(body.get("current_password", "")), str(body.get("new_password", ""))
    except Exception:
        return _fail("Invalid request.", 400)
    user = await store.get_user_by_id(me["user_id"])
    if not await asyncio.to_thread(passwords.verify_password, user["password_hash"], current):
        await store.record_failure(user["id"], settings.auth_max_failed_attempts, settings.auth_lock_minutes)
        await store.audit("password_change_failed", email=user["email"], user_id=user["id"], ip=client_ip(request.scope))
        return _fail("The current password is incorrect.", 400)
    problems = passwords.password_problems(new, user["email"])
    if new == current:
        problems.append("must be different from the current one")
    if problems:
        return _fail("The new password " + "; ".join(problems) + ".", 400)
    await store.set_password_by_id(user["id"], await asyncio.to_thread(passwords.hash_password, new))
    token = parse_cookies(request.scope).get(settings.auth_cookie_name, "")
    ended = await store.revoke_other_sessions(user["id"], token)
    await store.audit("password_changed", email=user["email"], user_id=user["id"], ip=client_ip(request.scope), detail=f"{ended} other sessions ended")
    return _json({"ok": True, "other_sessions_ended": ended})


@router.post("/api/auth/2fa/setup")
async def twofa_setup(request: Request):
    store = request.app.state.auth_store
    me = _me(request)
    user = await store.get_user_by_id(me["user_id"])
    if user.get("totp_enabled"):
        return _fail("Two-factor is already on. Turn it off first to set up a new device.", 409)
    secret = totp.generate_secret()
    await store.totp_begin(user["id"], secret)
    uri = totp.otpauth_uri(user["email"], secret)
    return _json({"secret": secret, "uri": uri, "qr": totp.qr_data_uri(uri)})


@router.post("/api/auth/2fa/enable")
async def twofa_enable(request: Request):
    store = request.app.state.auth_store
    me = _me(request)
    user = await store.get_user_by_id(me["user_id"])
    if user.get("totp_enabled") or not user.get("totp_secret"):
        return _fail("Start the setup first.", 409)
    try:
        code = str((await request.json()).get("code", ""))
    except Exception:
        return _fail("Invalid request.", 400)
    step = totp.verify(user["totp_secret"], code)
    if step is None:
        await store.audit("totp_setup_failed", email=user["email"], user_id=user["id"], ip=client_ip(request.scope))
        return _fail("That code is not right. Check the time on your phone and try the next code.", 400)
    codes = totp.new_recovery_codes()
    await store.totp_enable(user["id"], step)
    await store.replace_recovery_codes(user["id"], [totp.hash_recovery_code(c) for c in codes])
    await store.audit("totp_enabled", email=user["email"], user_id=user["id"], ip=client_ip(request.scope))
    return _json({"ok": True, "recovery_codes": codes})


@router.post("/api/auth/2fa/disable")
async def twofa_disable(request: Request):
    store = request.app.state.auth_store
    me = _me(request)
    user = await store.get_user_by_id(me["user_id"])
    if user["role"] in _required_roles():
        return _fail("Two-factor is required for your role. An admin can reset it if you lose your device.", 403)
    try:
        body = await request.json()
        password, code = str(body.get("password", "")), str(body.get("code", ""))
    except Exception:
        return _fail("Invalid request.", 400)
    if not await asyncio.to_thread(passwords.verify_password, user["password_hash"], password) \
            or totp.verify(user.get("totp_secret") or "", code, user.get("totp_last_step")) is None:
        await store.record_failure(user["id"], settings.auth_max_failed_attempts, settings.auth_lock_minutes)
        return _fail("The password or the code is not right.", 400)
    await store.totp_disable(user["id"])
    await store.audit("totp_disabled", email=user["email"], user_id=user["id"], ip=client_ip(request.scope))
    return _json({"ok": True})


@router.post("/api/auth/2fa/recovery-codes")
async def new_recovery(request: Request):
    store = request.app.state.auth_store
    me = _me(request)
    user = await store.get_user_by_id(me["user_id"])
    if not user.get("totp_enabled"):
        return _fail("Two-factor is not on.", 409)
    try:
        password = str((await request.json()).get("password", ""))
    except Exception:
        return _fail("Invalid request.", 400)
    if not await asyncio.to_thread(passwords.verify_password, user["password_hash"], password):
        return _fail("The password is not right.", 400)
    codes = totp.new_recovery_codes()
    await store.replace_recovery_codes(user["id"], [totp.hash_recovery_code(c) for c in codes])
    await store.audit("recovery_codes_regenerated", email=user["email"], user_id=user["id"], ip=client_ip(request.scope))
    return _json({"recovery_codes": codes})


# -- admin ------------------------------------------------------------------------------------------------------
def _public_user(u: dict) -> dict:
    keys = ("id", "email", "role", "is_active", "expires_at", "locked_until", "last_login_at", "created_at", "must_change_password", "totp_enabled")
    return {k: (u[k].isoformat() if hasattr(u.get(k), "isoformat") else u.get(k)) for k in keys}


@router.get("/api/admin/users")
async def admin_users(request: Request):
    return _json({"users": [_public_user(u) for u in await request.app.state.auth_store.list_users()],
                  "roles": [r for r in ROLES if r != "demo"], "twofa_required_roles": list(_required_roles())})


@router.post("/api/admin/users")
async def admin_create_user(request: Request):
    store = request.app.state.auth_store
    body = await request.json()
    email, role = str(body.get("email", "")).strip().lower(), str(body.get("role", "client"))
    if not service.valid_email(email) or role not in ROLES or role == "demo":
        return _fail("Give a valid email and a role of admin or client.", 400)
    if await store.get_user_by_email(email):
        return _fail("That email already has a login.", 409)
    days = int(body["expires_days"]) if body.get("expires_days") else None
    _, password = await service.create_user(store, email, role, expires_days=days)
    await store.audit("user_created", email=email, ip=client_ip(request.scope), detail=f"role={role} by {_me(request)['email']}")
    return _json({"email": email, "role": role, "temporary_password": password})


@router.post("/api/admin/users/{action}")
async def admin_user_action(action: str, request: Request):
    store = request.app.state.auth_store
    me = _me(request)
    email = str((await request.json()).get("email", "")).strip().lower()
    user = await store.get_user_by_email(email)
    if user is None:
        return _fail("No such user.", 404)
    if user["id"] == me["user_id"] and action in ("disable", "reset-password", "reset-2fa"):
        return _fail("Use your own account page for that; this would lock you out.", 400)
    result: dict = {"ok": True}
    if action == "disable":
        await store.set_active(email, False)
        result["sessions_ended"] = await store.revoke_user_sessions(email)
    elif action == "enable":
        await store.set_active(email, True)
    elif action == "logout":
        result["sessions_ended"] = await store.revoke_user_sessions(email)
    elif action == "reset-password":
        password = passwords.generate_password()
        await store.set_password(email, await asyncio.to_thread(passwords.hash_password, password), must_change=True)
        result.update(temporary_password=password, sessions_ended=await store.revoke_user_sessions(email))
    elif action == "reset-2fa":
        await store.totp_disable(user["id"])
        result["sessions_ended"] = await store.revoke_user_sessions(email)
    else:
        return _fail("Unknown action.", 404)
    await store.audit(f"admin_{action.replace('-', '_')}", email=email, user_id=user["id"], ip=client_ip(request.scope), detail=f"by {me['email']}")
    return _json(result)


def _base_url(request: Request) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return f"{'https' if request_is_https(request.scope) else 'http'}://{request.headers.get('host', '')}"


@router.get("/api/admin/demo-links")
async def admin_demo_links(request: Request):
    links = await request.app.state.auth_store.list_demo_links()
    return _json({"links": [{k: (l[k].isoformat() if hasattr(l.get(k), "isoformat") else l.get(k))
                             for k in ("id", "label", "created_at", "expires_at", "revoked_at", "use_count", "last_used_at")} for l in links]})


@router.post("/api/admin/demo-links")
async def admin_create_demo_link(request: Request):
    store = request.app.state.auth_store
    body = await request.json()
    label = str(body.get("label", "")).strip()[:80]
    try:
        days = float(body.get("days", 3))
    except (TypeError, ValueError):
        days = 0
    if not label or not 0 < days <= 60:
        return _fail("Give a label and a lifetime between a fraction of a day and 60 days.", 400)
    link = await service.create_demo_link(store, label, days)
    await store.audit("demo_link_created", email=link["email"], ip=client_ip(request.scope), detail=f"{label} by {_me(request)['email']}")
    return _json({"id": link["id"], "url": _base_url(request) + link["path"], "expires_at": link["expires_at"].isoformat()})


@router.post("/api/admin/demo-links/revoke")
async def admin_revoke_demo_link(request: Request):
    store = request.app.state.auth_store
    link_id = int((await request.json()).get("id", 0))
    ok = await store.revoke_demo_link(link_id)
    if ok:
        await store.audit("demo_link_revoked", ip=client_ip(request.scope), detail=f"#{link_id} by {_me(request)['email']}")
    return _json({"ok": ok}, 200 if ok else 404)


@router.get("/api/admin/audit")
async def admin_audit(request: Request):
    rows = await request.app.state.auth_store.recent_audit(150)
    return _json({"events": [{**r, "at": r["at"].isoformat()} for r in rows]})
