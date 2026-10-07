import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from citadel.api.security import CSP, GuardMiddleware, SecurityHeadersMiddleware, check_db_credentials, client_ip


def _app(rpm=3, ws_max=1):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GuardMiddleware, requests_per_minute=rpm, ws_max_per_ip=ws_max)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    @app.get("/page")
    def page():
        return {"ok": True}

    @app.websocket("/ws/x")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        try:
            await websocket.receive_text()
        except WebSocketDisconnect:
            pass

    return app


def test_security_headers_on_every_response():
    r = TestClient(_app()).get("/page")
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in CSP and "object-src 'none'" in CSP
    assert "strict-transport-security" not in r.headers  # plain http


def test_hsts_only_behind_https_proxy():
    r = TestClient(_app()).get("/page", headers={"x-forwarded-proto": "https"})
    assert "max-age" in r.headers["strict-transport-security"]


def test_rate_limit_returns_429_with_headers_and_only_on_api():
    c = TestClient(_app(rpm=3))
    assert [c.get("/api/ping").status_code for _ in range(4)] == [200, 200, 200, 429]
    limited = c.get("/api/ping")
    assert limited.status_code == 429 and "retry-after" in limited.headers
    assert "content-security-policy" in limited.headers      # headers wrap the 429 too
    assert c.get("/page").status_code == 200                    # non-API paths are not counted


def test_websocket_cap_rejects_second_connection_then_frees_the_slot():
    c = TestClient(_app(ws_max=1))
    with c.websocket_connect("/ws/x"):
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/x"):
                pass
    with c.websocket_connect("/ws/x"):   # slot released when the first closed
        pass


def test_client_ip_trusts_forwarded_for_only_from_private_peers():
    mk = lambda peer, xff: {"client": (peer, 1), "headers": [(b"x-forwarded-for", xff)]}
    assert client_ip(mk("172.18.0.5", b"9.9.9.9, 203.0.113.7")) == "203.0.113.7"
    assert client_ip(mk("8.8.4.4", b"1.2.3.4")) == "8.8.4.4"   # a public peer could be lying


def test_db_default_password_rules():
    check_db_credentials("postgresql://citadel:citadel@localhost:5432/citadel")
    check_db_credentials("postgresql://citadel:citadel@postgres:5432/citadel")
    check_db_credentials("postgresql://citadel:Str0ng-and-random@db.example.com/citadel")
    with pytest.raises(RuntimeError):
        check_db_credentials("postgresql://citadel:citadel@db.example.com:5432/citadel")


def test_real_app_hides_docs_and_rejects_out_of_range_params():
    from citadel.api.app import app
    c = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404
    assert c.get("/api/natgrid?days=100000").status_code == 422
    assert c.get("/api/stack/recent?count=100000").status_code == 422
    assert c.get("/api/fpn/generation-by-fuel?hours=99999").status_code == 422
    assert c.get("/api/trips/recent?limit=10000000").status_code == 422
    assert c.get("/api/trips/0/revisions").status_code == 422
    assert c.get("/api/stack/2026-10-01/999").status_code == 422
    assert c.get("/api/trips/mel-plan?bm_unit=" + "A" * 500).status_code == 422
    assert c.get("/api/trips/mel-plan?bm_unit=x';drop%20table").status_code == 422
