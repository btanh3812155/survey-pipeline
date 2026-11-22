"""Cross-router admin page matrix: auth, redirects, and fail-closed pages."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.state import verify_admin_auth
from routers import (
    admin_accounts,
    admin_audit,
    admin_ctvs,
    admin_data,
    admin_exports,
    admin_forms,
    admin_image_objects,
    admin_media,
    admin_modules,
    admin_recovery,
)

PROTECTED_PAGES = (
    "/admin",
    "/admin/samples",
    "/admin/accounts",
    "/admin/accounts/password",
    "/admin/account",
    "/admin/audit",
    "/audit",
    "/admin/ctvs",
    "/admin/data",
    "/data",
    "/admin/data/income_survey",
    "/admin/exports",
    "/admin/forms",
    "/admin/forms/create",
    "/admin/forms/edit/TB_GSM_2026_Nov_Test",
    "/admin/image-objects",
    "/admin/media",
    "/admin/modules",
    "/admin/recovery/email",
    "/admin/submissions",
    "/submissions",
    "/admin/trash",
)

# status: 200 rendered, 302 redirect, 503 deliberately unavailable without a DB.
EXPECTED_AUTHENTICATED = {
    "/admin": 302,
    "/admin/samples": 302,
    "/admin/accounts": 503,
    "/admin/accounts/password": 303,
    "/admin/account": 503,
    "/admin/audit": 200,
    "/audit": 200,
    "/admin/ctvs": 200,
    "/admin/data": 200,
    "/data": 200,
    "/admin/data/income_survey": 200,
    "/admin/exports": 503,
    "/admin/forms": 200,
    "/admin/forms/create": 200,
    "/admin/forms/edit/TB_GSM_2026_Nov_Test": 200,
    "/admin/image-objects": 200,
    "/admin/media": 200,
    "/admin/modules": 503,
    "/admin/recovery/email": 200,
    "/admin/submissions": 200,
    "/submissions": 200,
    "/admin/trash": 200,
}


def _app() -> FastAPI:
    app = FastAPI()
    for router in (
        admin_forms.router, admin_data.router, admin_audit.router, admin_media.router,
        admin_ctvs.router, admin_image_objects.router, admin_accounts.router,
        admin_modules.router, admin_exports.router, admin_recovery.router,
    ):
        app.include_router(router)
    return app


def test_every_admin_page_requires_authentication():
    client = TestClient(_app(), raise_server_exceptions=False)
    for path in PROTECTED_PAGES:
        response = client.get(path, follow_redirects=False)
        assert response.status_code in (401, 403), (path, response.status_code)
        if response.status_code == 401:
            assert response.headers.get("www-authenticate", "").lower().startswith("basic")


def test_public_recovery_page_is_reachable_without_login():
    response = TestClient(_app()).get("/admin/recovery")
    assert response.status_code == 200
    assert "Khôi phục mật khẩu quản trị" in response.text


def test_authenticated_admin_page_matrix_is_accounted_for(monkeypatch):
    import core.state as state
    import main
    monkeypatch.setattr(state, "db_pool", None)
    monkeypatch.setattr(main, "db_pool", None)
    monkeypatch.setattr(admin_forms, "refresh_form_registry_from_disk", lambda *a, **k: None)
    app = _app()
    app.dependency_overrides[verify_admin_auth] = lambda: "matrix-admin"
    client = TestClient(app, raise_server_exceptions=False)
    for path, expected in EXPECTED_AUTHENTICATED.items():
        response = client.get(path, follow_redirects=False)
        assert response.status_code == expected, (path, response.status_code, response.text[:160])
        if expected == 200:
            assert response.text.strip(), path
        if expected == 302:
            assert response.headers["location"], path
