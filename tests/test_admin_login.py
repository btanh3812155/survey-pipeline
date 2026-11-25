"""Admin browser login: signed cookie sessions replace the Basic Auth popup."""
import base64

import pytest
from fastapi.testclient import TestClient

import core.state as state
from core.admin_sessions import COOKIE_NAME
from core.passwords import hash_password
from main import app

PASSWORD = "fixture-password"


@pytest.fixture
def fake_admin(monkeypatch):
    encoded = hash_password(PASSWORD, iterations=1_000)

    async def load(username):
        if username == "owner":
            return {
                "password_hash": encoded,
                "is_active": True,
                "role": "manager",
                "deleted_at": None,
            }
        return None

    monkeypatch.setattr(state, "_load_admin_account", load)
    monkeypatch.setattr(state, "get_redis_client", lambda: None)
    monkeypatch.setattr(state.settings, "ENVIRONMENT", "test")
    return encoded


def _client():
    return TestClient(app, base_url="http://testserver")


def _auth_app():
    from fastapi import Depends, FastAPI
    from core.state import verify_admin_auth
    from routers import login as login_routes

    app = FastAPI()
    app.include_router(login_routes.router)

    @app.post("/admin/forms")
    async def write_form(admin_user: str = Depends(verify_admin_auth)):
        return {"admin": admin_user}

    return app


def _login(client, password=PASSWORD, next_url="/admin/forms"):
    return client.post(
        "/login",
        data={"username": "owner", "password": password, "next": next_url},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )


def test_login_page_is_public(fake_admin):
    response = _client().get("/login")
    assert response.status_code == 200
    assert "Đăng nhập quản trị" in response.text
    assert 'name="username"' in response.text
    assert 'name="password"' in response.text


def test_browser_admin_request_redirects_to_login(fake_admin):
    client = _client()
    response = client.get("/admin/forms", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    assert "www-authenticate" not in {key.lower() for key in response.headers}


def test_api_request_keeps_basic_challenge(fake_admin):
    response = _client().get("/api/v1/submissions")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].lower().startswith("basic")


def test_login_sets_cookie_and_grants_admin_access(fake_admin):
    client = _client()
    response = _login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/forms"
    cookie = response.headers.get("set-cookie", "")
    assert COOKIE_NAME in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert client.cookies.get(COOKIE_NAME)
    assert client.get("/admin/forms").status_code == 200


def test_invalid_login_does_not_prompt_basic_auth(fake_admin):
    client = _client()
    response = _login(client, password="wrong")
    assert response.status_code == 401
    assert "Tên đăng nhập hoặc mật khẩu không đúng" in response.text
    assert "www-authenticate" not in {key.lower() for key in response.headers}
    assert not client.cookies.get(COOKIE_NAME)


def test_logout_requires_post_and_same_origin(fake_admin):
    client = _client()
    assert _login(client).status_code == 303
    assert client.get("/admin/forms").status_code == 200
    assert client.get("/logout", follow_redirects=False).status_code == 405
    assert client.post("/logout", follow_redirects=False).status_code == 403
    response = client.post(
        "/logout", headers={"Origin": "http://testserver"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert not client.cookies.get(COOKIE_NAME)
    assert client.get("/admin/forms", follow_redirects=False).status_code == 303


def test_cookie_post_requires_same_origin(fake_admin):
    client = TestClient(_auth_app(), base_url="http://testserver")
    assert _login(client).status_code == 303
    assert client.post("/admin/forms").status_code == 403
    assert client.post("/admin/forms", headers={"Origin": "http://testserver"}).status_code == 200


def test_basic_post_allows_headerless_cli_but_rejects_browser_cross_site(fake_admin):
    client = TestClient(_auth_app(), base_url="http://testserver")
    auth = ("owner", PASSWORD)
    assert client.post("/admin/forms", auth=auth).status_code == 200
    assert client.post(
        "/admin/forms", auth=auth, headers={"Origin": "https://evil.test"}
    ).status_code == 403
    assert client.post(
        "/admin/forms", auth=auth, headers={"Sec-Fetch-Site": "cross-site"}
    ).status_code == 403


def test_login_next_is_percent_encoded(fake_admin):
    response = _client().get(
        "/admin/forms?page=2&sort=name_asc", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/login?next=%2Fadmin%2Fforms%3Fpage%3D2%26sort%3Dname_asc"
    )


def test_tampered_cookie_is_rejected(fake_admin):
    client = _client()
    assert _login(client).status_code == 303
    token = client.cookies.get(COOKIE_NAME)
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, token[:-1] + ("0" if token[-1] != "0" else "1"))
    response = client.get("/admin/forms", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_password_change_invalidates_existing_session(fake_admin, monkeypatch):
    client = _client()
    assert _login(client).status_code == 303

    async def load_new_hash(username):
        return {
            "password_hash": hash_password("new-password", iterations=1_000),
            "is_active": True,
            "role": "manager",
            "deleted_at": None,
        }

    monkeypatch.setattr(state, "_load_admin_account", load_new_hash)
    response = client.get("/admin/forms", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_login_next_rejects_external_redirect(fake_admin):
    client = _client()
    response = _login(client, next_url="https://evil.example/steal")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/forms"


def test_session_survives_browser_restart_and_expires_after_thirty_days(fake_admin, monkeypatch):
    import core.admin_sessions as sessions

    now = 1_800_000_000
    monkeypatch.setattr(sessions.time, "time", lambda: now)
    client = _client()
    response = _login(client)
    assert "Max-Age=2592000" in response.headers["set-cookie"]
    token = client.cookies.get(COOKIE_NAME)

    restarted = _client()
    restarted.cookies.set(COOKIE_NAME, token)
    now += 29 * 24 * 60 * 60
    assert restarted.get("/admin/forms").status_code == 200
    now += 24 * 60 * 60
    assert restarted.get("/admin/forms", follow_redirects=False).status_code == 303
