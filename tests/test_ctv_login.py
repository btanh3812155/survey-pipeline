"""Browser authentication boundaries independent of Telegram availability."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from core import ctv_auth
from core.admin_sessions import COOKIE_NAME as ADMIN_COOKIE
from main import app


@pytest.mark.parametrize("host,target", [
    ("survey.trustbridgevn.com", "/ctv"),
    ("dev.survey.trustbridgevn.com", "/ctv"),
    ("admin.trustbridgevn.com", "/admin/forms"),
    ("audit.trustbridgevn.com", "/admin/audit"),
    ("data.trustbridgevn.com", "/admin/submissions"),
])
def test_root_routes_each_surface(host, target):
    response = TestClient(app).get("/", headers={"host": host}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == target


@pytest.mark.parametrize("path,data", [
    ("/ctv/login/request", {"code": "CTV_PUBLIC"}),
    ("/ctv/login/verify", {"challenge_id": "x" * 32, "otp": "123456"}),
    ("/ctv/logout", {}),
])
def test_cross_origin_ctv_auth_is_rejected_before_effects(monkeypatch, path, data):
    start, finish = AsyncMock(), AsyncMock()
    monkeypatch.setattr(ctv_auth, "start_login", start)
    monkeypatch.setattr(ctv_auth, "finish_login", finish)
    response = TestClient(app).post(path, data=data, headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    start.assert_not_awaited()
    finish.assert_not_awaited()


def test_ctv_cookie_is_persistent_scoped_secure_and_logout_keeps_admin(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(ctv_auth, "finish_login", AsyncMock(return_value="verified-session-token"))
    client = TestClient(app, base_url="https://survey.example")
    client.cookies.set(ADMIN_COOKIE, "independent-admin-session")
    response = client.post(
        "/ctv/login/verify", data={"challenge_id": "x" * 32, "otp": "123456"},
        headers={"origin": "https://survey.example"}, follow_redirects=False,
    )
    assert response.status_code == 303
    cookie = response.headers.get_list("set-cookie")[0]
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/ctv", "Max-Age=2592000"):
        assert flag in cookie
    assert "Domain=" not in cookie
    assert client.cookies.get(ctv_auth.COOKIE_NAME)
    response = client.post("/ctv/logout", headers={"origin": "https://survey.example"}, follow_redirects=False)
    assert response.status_code == 303
    assert not client.cookies.get(ctv_auth.COOKIE_NAME)
    assert client.cookies.get(ADMIN_COOKIE) == "independent-admin-session"
