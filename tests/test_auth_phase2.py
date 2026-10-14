import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from citadel.auth import service, totp
from citadel.auth.middleware import AuthMiddleware
from citadel.auth.routes import _attempts, router
from citadel.auth.store import MemoryAuthStore
from citadel.config import settings

PASSWORD = "correct-horse-battery-9"
NEW_PASSWORD = "an-even-better-passphrase-7"


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_login_attempts_per_5min", 1000)
    _attempts.clear()
    return MemoryAuthStore()


def make_app(store, idle_seconds=1800, recheck_seconds=60):
    app = FastAPI()
    app.state.auth_store = store
    app.add_middleware(AuthMiddleware, enabled=True, cookie_name=settings.auth_cookie_name, idle_seconds=idle_seconds,
                       require_2fa_roles=service.two_factor_roles(settings.auth_2fa_required_roles), recheck_seconds=recheck_seconds)
    app.include_router(router)

    @app.get("/api/secret")
    def secret():
        return {"secret": 1}

    @app.post("/api/change")
    def change():
        return {"changed": True}

    @app.websocket("/ws/x")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass

    return app


async def add_user(store, email, role="client", must_change=False, totp_on=False):
    uid, _ = await service.create_user(store, email, role, PASSWORD, force_change=must_change)
    if totp_on:
        secret = totp.generate_secret()
        await store.totp_begin(uid, secret)
        await store.totp_enable(uid, 0)
        await store.replace_recovery_codes(uid, [totp.hash_recovery_code("abcd-efgh")])
        return uid, secret
    return uid, None


def login(c, email, password=PASSWORD):
    return c.post("/api/auth/login", json={"email": email, "password": password})


# -- TOTP primitives -----------------------------------------------------------------------------------------
def test_totp_matches_the_rfc_6238_test_vector_and_refuses_replays():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"            # ASCII "12345678901234567890"
    assert totp.code_now(secret, now=59) == "287082"
    assert totp.verify(secret, "287082", now=59) == 1
    assert totp.verify(secret, "287082", last_used_step=1, now=59) is None           # the same step cannot be used twice
    assert totp.verify(secret, "287 082", now=59 + 30) == 1                           # one step of drift is accepted
    assert totp.verify(secret, "287082", now=59 + 120) is None                        # but not two minutes of it
    assert totp.verify(secret, "12345", now=59) is None and totp.verify(secret, "abcdef", now=59) is None


def test_recovery_codes_are_normalised_and_hashed():
    codes = totp.new_recovery_codes()
    assert len(set(codes)) == 10 and all(len(c) == 9 and c[4] == "-" for c in codes)
    assert totp.hash_recovery_code(codes[0].upper()) == totp.hash_recovery_code(codes[0].replace("-", " "))
    assert totp.normalise_recovery_code("nope") == ""


def test_qr_code_is_an_inline_svg():
    uri = totp.otpauth_uri("a@example.com", "JBSWY3DPEHPK3PXP")
    assert uri.startswith("otpauth://totp/Citadel%3Aa%40example.com?secret=JBSWY3DPEHPK3PXP")
    assert totp.qr_data_uri(uri).startswith("data:image/svg+xml")


# -- forced password change -----------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_temporary_password_must_be_changed_before_anything_else(store, monkeypatch):
    monkeypatch.setattr(settings, "auth_2fa_required_roles", "")                # isolate the password rule from the 2FA rule
    await add_user(store, "new@example.com", role="client", must_change=True)
    c = TestClient(make_app(store), follow_redirects=False)
    assert login(c, "new@example.com").status_code == 200
    r = c.get("/api/secret")
    assert r.status_code == 403 and r.json()["required"] == ["change_password"]
    assert c.get("/", headers={"accept": "text/html"}).status_code in (302, 404)       # pages go to /account (no / route here)
    assert c.get("/account").status_code == 200 and c.get("/api/auth/me").json()["must_change_password"] is True
    other = TestClient(make_app(store))
    login(other, "new@example.com")
    weak = c.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": "short"})
    assert weak.status_code == 400 and "12 characters" in weak.json()["detail"]
    assert c.post("/api/auth/change-password", json={"current_password": "wrong-wrong-wrong-1", "new_password": NEW_PASSWORD}).status_code == 400
    same = c.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": PASSWORD})
    assert same.status_code == 400 and "different" in same.json()["detail"]
    ok = c.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert ok.status_code == 200 and ok.json()["other_sessions_ended"] == 1
    assert c.get("/api/secret").status_code == 200                                # now free to use the app
    assert other.get("/api/secret").status_code == 401                            # the other device was signed out
    fresh = TestClient(make_app(store))
    assert login(fresh, "new@example.com", PASSWORD).status_code == 401 and login(fresh, "new@example.com", NEW_PASSWORD).status_code == 200


# -- two-factor ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_two_factor_setup_is_forced_for_clients_and_then_required_at_every_sign_in(store):
    await add_user(store, "client@example.com", role="client")
    c = TestClient(make_app(store))
    assert login(c, "client@example.com").status_code == 200
    gate = c.get("/api/secret")
    assert gate.status_code == 403 and gate.json()["required"] == ["setup_2fa"]
    assert c.post("/api/auth/2fa/enable", json={"code": "123456"}).status_code == 409          # nothing started yet
    setup = c.post("/api/auth/2fa/setup").json()
    assert setup["qr"].startswith("data:image/svg+xml") and setup["secret"]
    assert c.post("/api/auth/2fa/enable", json={"code": "000000"}).status_code == 400
    enabled = c.post("/api/auth/2fa/enable", json={"code": totp.code_now(setup["secret"])})
    codes = enabled.json()["recovery_codes"]
    assert enabled.status_code == 200 and len(codes) == 10
    assert c.get("/api/secret").status_code == 200
    me = c.get("/api/auth/me").json()
    assert me["totp_enabled"] is True and me["recovery_codes_left"] == 10 and me["twofa_required"] is True
    assert c.post("/api/auth/2fa/disable", json={"password": PASSWORD, "code": "000000"}).status_code == 403   # required for the role

    # next sign-in: password alone is not enough
    d = TestClient(make_app(store))
    first = login(d, "client@example.com")
    assert first.status_code == 200 and first.json() == {"needs_totp": True}
    assert d.get("/api/secret").status_code == 401                                   # only the right to try a code so far
    assert d.post("/api/auth/login/totp", json={"code": "000000"}).status_code == 401
    # the code already used at setup cannot be replayed to sign in
    assert d.post("/api/auth/login/totp", json={"code": totp.code_now(setup["secret"])}).status_code == 401
    user = await store.get_user_by_email("client@example.com")
    user["totp_last_step"] = totp.current_step() - 5                                  # as if the last use was a while ago
    good = d.post("/api/auth/login/totp", json={"code": totp.code_now(setup["secret"])})
    assert good.status_code == 200 and d.get("/api/secret").status_code == 200
    # recovery code: works once
    e = TestClient(make_app(store))
    login(e, "client@example.com")
    assert e.post("/api/auth/login/totp", json={"code": codes[0]}).status_code == 200
    f = TestClient(make_app(store))
    login(f, "client@example.com")
    assert f.post("/api/auth/login/totp", json={"code": codes[0]}).status_code == 401
    assert "login_ok_recovery_code" in [ev[0] for ev in store.events]


@pytest.mark.asyncio
async def test_wrong_codes_lock_the_account_and_a_stale_sign_in_expires(store):
    uid, secret = await add_user(store, "locked@example.com", totp_on=True)
    c = TestClient(make_app(store))
    for _ in range(settings.auth_max_failed_attempts):
        login(c, "locked@example.com")
        assert c.post("/api/auth/login/totp", json={"code": "000000"}).status_code == 401
    assert login(c, "locked@example.com").status_code == 401                          # locked now, even with the right password
    store.users["locked@example.com"]["locked_until"] = None
    login(c, "locked@example.com")
    list(store.pending.values())[-1]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)      # the newest sign-in
    r = c.post("/api/auth/login/totp", json={"code": totp.code_now(secret)})
    assert r.status_code == 401 and "timed out" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_user_can_turn_two_factor_off_only_where_it_is_not_required(store, monkeypatch):
    monkeypatch.setattr(settings, "auth_2fa_required_roles", "admin")
    uid, secret = await add_user(store, "optional@example.com", role="client", totp_on=True)
    store.users["optional@example.com"]["totp_secret"] = secret
    store.users["optional@example.com"]["totp_last_step"] = 0
    c = TestClient(make_app(store))
    login(c, "optional@example.com")
    c.post("/api/auth/login/totp", json={"code": totp.code_now(secret)})
    store.users["optional@example.com"]["totp_last_step"] = totp.current_step() - 5          # a code is single-use: pretend time has passed
    assert c.post("/api/auth/2fa/disable", json={"password": "wrong-wrong-wrong-1", "code": totp.code_now(secret)}).status_code == 400
    ok = c.post("/api/auth/2fa/disable", json={"password": PASSWORD, "code": totp.code_now(secret)})
    assert ok.status_code == 200 and store.users["optional@example.com"]["totp_enabled"] is False


# -- demo links and roles -----------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_demo_link_gives_read_only_access_and_can_be_revoked(store):
    await add_user(store, "boss@example.com", role="admin", totp_on=True)
    link = await service.create_demo_link(store, "Acme Energy", 3)
    c = TestClient(make_app(store), follow_redirects=False)
    r = c.get(link["path"])
    assert r.status_code == 302 and r.headers["location"] == "/" and r.headers["referrer-policy"] == "no-referrer"
    assert c.get("/api/secret").status_code == 200                                    # reads work
    assert c.post("/api/change").status_code == 403                                   # anything that changes things does not
    assert c.get("/account", headers={"accept": "text/html"}).status_code == 302      # no account or admin pages
    assert c.get("/api/admin/users").status_code == 403
    assert c.post("/api/auth/change-password", json={}).status_code == 403
    assert c.get("/api/auth/me").json()["role"] == "demo"
    assert (await store.list_demo_links())[0]["use_count"] == 1
    # nothing marks it as a password login: the unusable password cannot be guessed into a session
    assert login(c, link["email"], "anything-at-all-123").status_code == 401
    assert await store.revoke_demo_link(link["id"])
    assert c.get("/api/secret").status_code == 401                                    # revoking ends the session at once
    again = c.get(link["path"])
    assert again.status_code == 302 and again.headers["location"] == "/login?demo=expired"
    expired = await service.create_demo_link(store, "Old", 0.0001)
    store.demo_links[expired["id"]]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert c.get(expired["path"]).headers["location"] == "/login?demo=expired"
    assert c.get("/demo/not-a-real-token").headers["location"] == "/login?demo=expired"


@pytest.mark.asyncio
async def test_demo_session_is_capped_at_the_session_hours_setting(store, monkeypatch):
    monkeypatch.setattr(settings, "auth_demo_session_hours", 1)
    link = await service.create_demo_link(store, "Long link", 30)
    c = TestClient(make_app(store))
    c.get(link["path"])
    session = next(iter(store.sessions.values()))
    assert session["expires_at"] - datetime.now(timezone.utc) < timedelta(hours=1, minutes=1)


@pytest.mark.asyncio
async def test_admin_api_manages_users_and_is_for_admins_only(store):
    await add_user(store, "boss@example.com", role="admin", totp_on=True)
    await add_user(store, "client@example.com", role="client", totp_on=True)
    admin = TestClient(make_app(store))
    login(admin, "boss@example.com")
    admin.post("/api/auth/login/totp", json={"code": totp.code_now(store.users["boss@example.com"]["totp_secret"])})
    client = TestClient(make_app(store))
    login(client, "client@example.com")
    client.post("/api/auth/login/totp", json={"code": totp.code_now(store.users["client@example.com"]["totp_secret"])})
    assert client.get("/api/admin/users").status_code == 403 and client.get("/admin").status_code == 403
    assert admin.get("/api/admin/users").status_code == 200 and admin.get("/admin").status_code == 200

    made = admin.post("/api/admin/users", json={"email": "Fresh@Example.com", "role": "client", "expires_days": 30}).json()
    assert made["email"] == "fresh@example.com" and made["temporary_password"]
    assert store.users["fresh@example.com"]["must_change_password"] is True and store.users["fresh@example.com"]["expires_at"]
    assert admin.post("/api/admin/users", json={"email": "fresh@example.com", "role": "client"}).status_code == 409
    assert admin.post("/api/admin/users", json={"email": "not-an-email", "role": "client"}).status_code == 400
    assert admin.post("/api/admin/users", json={"email": "x@example.com", "role": "demo"}).status_code == 400
    assert login(TestClient(make_app(store)), "fresh@example.com", made["temporary_password"]).status_code == 200

    assert admin.post("/api/admin/users/disable", json={"email": "client@example.com"}).status_code == 200
    assert client.get("/api/secret").status_code == 401                               # an open session ends at once
    assert admin.post("/api/admin/users/disable", json={"email": "boss@example.com"}).status_code == 400   # not yourself
    reset = admin.post("/api/admin/users/reset-password", json={"email": "client@example.com"}).json()
    assert reset["temporary_password"] and store.users["client@example.com"]["must_change_password"] is True
    assert admin.post("/api/admin/users/reset-2fa", json={"email": "client@example.com"}).status_code == 200
    assert store.users["client@example.com"]["totp_enabled"] is False
    assert admin.post("/api/admin/users/nope", json={"email": "client@example.com"}).status_code == 404
    link = admin.post("/api/admin/demo-links", json={"label": "Acme", "days": 2}).json()
    assert link["url"].endswith("/demo/" + link["url"].rsplit("/", 1)[1]) and admin.get("/api/admin/demo-links").json()["links"]
    assert admin.post("/api/admin/demo-links", json={"label": "", "days": 2}).status_code == 400
    assert admin.post("/api/admin/demo-links/revoke", json={"id": link and 1}).status_code == 200
    events = [e["event"] for e in admin.get("/api/admin/audit").json()["events"]]
    assert "user_created" in events and "admin_disable" in events and "demo_link_created" in events


# -- websockets end with the session ------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_open_websocket_is_closed_when_its_session_is_revoked(store):
    await add_user(store, "watch@example.com", role="client", totp_on=True)
    app = make_app(store, recheck_seconds=0.3)
    c = TestClient(app)
    login(c, "watch@example.com")
    c.post("/api/auth/login/totp", json={"code": totp.code_now(store.users["watch@example.com"]["totp_secret"])})
    with c.websocket_connect("/ws/x") as ws:
        ws.send_text("ping")
        await store.revoke_user_sessions("watch@example.com")
        time.sleep(0.8)
        with pytest.raises(WebSocketDisconnect):
            for _ in range(5):
                ws.send_text("ping")
                ws.receive_text()


@pytest.mark.asyncio
async def test_a_websocket_is_refused_while_account_setup_is_outstanding(store):
    await add_user(store, "setup@example.com", role="client")                        # 2FA required but not yet set up
    c = TestClient(make_app(store))
    login(c, "setup@example.com")
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/x"):
            pass
