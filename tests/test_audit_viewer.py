import pytest
import base64
from fastapi.testclient import TestClient
from main import app


@pytest.fixture
def auth_headers():
    token = base64.b64encode(b"admin:12345678").decode("utf-8")
    return {"Authorization": f"Basic {token}"}


def test_audit_viewer_unauthorized():
    client = TestClient(app)
    resp = client.get("/admin/audit")
    assert resp.status_code == 401


def test_audit_viewer_authorized(auth_headers):
    client = TestClient(app)
    resp = client.get("/admin/audit", headers=auth_headers)
    assert resp.status_code == 200
    assert "Nhật Ký Kiểm Toán Toàn Hệ Thống" in resp.text
    assert "KPI Summary Cards" in resp.text or "stats-grid" in resp.text
    assert "tableWrap" in resp.text or "auditTable" in resp.text


def test_audit_api_endpoint(auth_headers):
    client = TestClient(app)
    resp = client.get("/api/v1/audit/logs", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "count" in data
    assert "logs" in data
    assert "summary" in data
    assert "total" in data["summary"]
    assert "ingress_normalizer" in data["summary"]


def test_audit_subdomain_routing():
    client = TestClient(app)
    resp = client.get("/", headers={"Host": "audit.146.190.201.32.sslip.io"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers.get("location") == "/admin/audit"
