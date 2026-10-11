from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from citadel.auth import passwords, routes
from citadel.auth.middleware import AuthMiddleware, safe_next
from citadel.auth.routes import router
from citadel.auth.store import MemoryAuthStore, token_hash
from citadel.config import settings

EMAIL, PASSWORD = "analyst@example.com", "correct-horse-battery-9"


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    routes._attempts.clear()
    return MemoryAuthStore()


def make_app(store, idle_seconds=1800):
    app = FastAPI()
    app.state.auth_store = store
    app.add_middleware(AuthMiddleware, enabled=True, cookie_name=settings.auth_cookie_name, idle_seconds=idle_seconds)
    app.include_router(router)

    @app.get("/api/secret")
    def secret():
        return {"secret": 1}

    @app.get("/page")
    def page():
        return {"page": 1}

    @app.post("/api/change")
    def change():
        return {"changed": True}

    @app.websocket("/ws/x")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    return app


async def add_user(store, email=EMAIL, password=PASSWORD, role="client", **kw):
    uid = await store.create_user(email, passwords.hash_password(password), role, kw.get("expires_at"))
    if kw.get("inactive"):
        store.users[email]["is_active"] = False
    return uid


def login(client, email=EMAIL, password=PASSWORD, **kw):
    return client.post("/api/auth/login", json={"email": email, "password": password}, **kw)


@pytest.mark.asyncio
async def test_everything_but_the_allow_list_needs_a_session(store):
    c = TestClient(make_app(store), follow_redirects=False)
    r = c.get("/api/secret")
    assert r.status_code == 401 and r.json() == {"detail": "login required"}
    page = c.get("/page", headers={"accept": "text/html"})
    assert page.status_code == 302 and page.headers["location"] == "/login?next=/page"
    assert c.get("/api/health").status_code == 200                      # the health check stays open
    assert c.get("/login").status_code == 200                           # and so does the sign-in page
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/x"):
            pass


@pytest.mark.asyncio
async def test_login_sets_a_hardened_cookie_and_unlocks_the_app(store):
    await add_user(store)
    c = TestClient(make_app(store))
    r = login(c)
    assert r.status_code == 200 and r.json() == {"email": EMAIL, "role": "client"}
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie       # plain http
    assert c.get("/api/secret").status_code == 200
    me = c.get("/api/auth/me").json()
    assert me["email"] == EMAIL and me["auth_enabled"] is True
    with c.websocket_connect("/ws/x") as ws:
        assert ws.receive_text() == "hi"
    secure = login(TestClient(make_app(store)), headers={"x-forwarded-proto": "https"})
    assert "secure" in secure.headers["set-cookie"].lower()                                    # behind the https proxy
    assert token_hash(c.cookies.get(settings.auth_cookie_name)) in store.sessions              # stored hashed, not raw


@pytest.mark.asyncio
async def test_wrong_password_and_unknown_user_look_identical(store):
    await add_user(store)
    c = TestClient(make_app(store))
    a, b = login(c, password="not-the-password-1"), login(c, email="nobody@example.com")
    assert a.status_code == b.status_code == 401 and a.json() == b.json() == {"detail": routes.GENERIC_FAILURE}
    assert [e[0] for e in store.events] == ["login_failed", "login_failed"]


@pytest.mark.asyncio
async def test_account_locks_after_repeated_failures_even_for_the_right_password(store, monkeypatch):
    monkeypatch.setattr(settings, "auth_login_attempts_per_5min", 100)
    await add_user(store)
    c = TestClient(make_app(store))
    for _ in range(settings.auth_max_failed_attempts):
        assert login(c, password="wrong-password-123").status_code == 401
    assert login(c).status_code == 401                                   # right password, but locked
    assert "account_locked" in [e[0] for e in store.events] and "login_blocked" in [e[0] for e in store.events]
    store.users[EMAIL]["locked_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert login(c).status_code == 200                                   # lock has run out


@pytest.mark.asyncio
async def test_login_attempts_are_rate_limited_per_address(store, monkeypatch):
    monkeypatch.setattr(settings, "auth_login_attempts_per_5min", 3)
    c = TestClient(make_app(store))
    codes = [login(c, email="x@example.com", password="whatever-password").status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]


@pytest.mark.asyncio
async def test_logout_ends_the_session_for_good(store):
    await add_user(store)
    c = TestClient(make_app(store))
    login(c)
    token = c.cookies.get(settings.auth_cookie_name)
    assert c.post("/api/auth/logout").status_code == 200
    stolen = TestClient(make_app(store))
    stolen.cookies.set(settings.auth_cookie_name, token)
    assert stolen.get("/api/secret").status_code == 401                  # the old cookie is dead server-side too


@pytest.mark.asyncio
async def test_idle_sessions_expire(store):
    await add_user(store)
    c = TestClient(make_app(store, idle_seconds=60))
    login(c)
    next(iter(store.sessions.values()))["last_seen_at"] = datetime.now(timezone.utc) - timedelta(seconds=120)
    assert c.get("/api/secret").status_code == 401
    assert "session_idle_timeout" in [e[0] for e in store.events]


@pytest.mark.asyncio
async def test_disabled_or_expired_users_cannot_log_in_and_lose_their_session(store):
    await add_user(store)
    c = TestClient(make_app(store))
    login(c)
    store.users[EMAIL]["is_active"] = False
    assert c.get("/api/secret").status_code == 401                       # an open session stops working at once
    assert login(c).status_code == 401
    store.users[EMAIL]["is_active"] = True
    store.users[EMAIL]["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert login(c).status_code == 401


@pytest.mark.asyncio
async def test_cross_site_requests_are_refused(store):
    await add_user(store)
    c = TestClient(make_app(store), base_url="http://citadel.example")
    assert login(c, headers={"origin": "https://evil.example"}).status_code == 403
    assert login(c, headers={"origin": "http://citadel.example"}).status_code == 200
    assert c.post("/api/change", headers={"origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/change", headers={"origin": "http://citadel.example"}).status_code == 200
    with pytest.raises(WebSocketDisconnect):                              # cross-site websocket hijack
        with c.websocket_connect("/ws/x", headers={"origin": "https://evil.example"}):
            pass


def test_next_cannot_point_off_site():
    assert safe_next("/fpn?x=1") == "/fpn?x=1"
    for bad in (None, "", "https://evil.example", "//evil.example", "/\\evil.example", "fpn", "/a\r\nb"):
        assert safe_next(bad) == "/"


def test_password_rules_and_hashing():
    assert passwords.password_problems(PASSWORD, EMAIL) == []
    assert passwords.password_problems("short1") and passwords.password_problems("aaaaaaaaaaaaaaaa")
    assert passwords.password_problems("password123456") or passwords.password_problems("Password123")
    assert passwords.password_problems("analyst-is-my-name-1", EMAIL)            # contains the email name
    h = passwords.hash_password(PASSWORD)
    assert h.startswith("$argon2id$") and passwords.verify_password(h, PASSWORD) and not passwords.verify_password(h, "x" * 14)
    assert passwords.password_problems(passwords.generate_password()) == []


def test_every_route_of_the_real_app_is_behind_the_login():
    """Walks the real app's routes: with login on, nothing but the allow-list answers without a session."""
    from fastapi.routing import APIRoute, APIWebSocketRoute

    from citadel.api.app import app as real

    real.state.auth_store = MemoryAuthStore()
    inner = AuthMiddleware(real, enabled=True, cookie_name=settings.auth_cookie_name)

    async def shim(scope, receive, send):                                # Starlette sets scope["app"] itself in production
        scope["app"] = real
        await inner(scope, receive, send)

    c = TestClient(shim, follow_redirects=False)
    public = {"/login", "/api/auth/login", "/api/auth/logout", "/api/health"}
    checked = 0
    for route in real.routes:
        path = getattr(route, "path", "")
        concrete = path.replace("{", "").replace("}", "") if "{" in path else path
        if isinstance(route, APIRoute):
            if path in public or "GET" not in route.methods:
                continue
            for h in ({}, {"accept": "text/html"}):
                r = c.get(concrete, headers=h)
                assert r.status_code in (401, 302), f"{path} answered {r.status_code} without a session"
                checked += 1
        elif isinstance(route, APIWebSocketRoute):
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect(path):
                    pass
            checked += 1
    assert c.get("/static/fpn.js").status_code == 401 and c.get("/static/trips.js").status_code == 401
    assert c.get("/static/login.js").status_code == 200 and c.get("/static/login.css").status_code == 200
    assert checked > 25
