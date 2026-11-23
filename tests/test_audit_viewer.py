import pytest
import base64
from fastapi.testclient import TestClient
from main import app
from core.state import verify_admin_auth


@pytest.fixture
def auth_headers():
    token = base64.b64encode(b"admin:12345678").decode("utf-8")
    app.dependency_overrides[verify_admin_auth] = lambda: "audit-test-admin"
    yield {"Authorization": f"Basic {token}"}
    app.dependency_overrides.clear()


def test_audit_viewer_unauthorized():
    client = TestClient(app)
    resp = client.get("/admin/audit", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login?next=")


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

def test_admin_data_viewer_page(auth_headers):
    client = TestClient(app)
    resp = client.get("/admin/data", headers=auth_headers)
    assert resp.status_code == 200
    assert "Trung Tâm Dữ Liệu Khảo Sát & Bảng Truy Vấn" in resp.text
    assert "formTabsBar" in resp.text
    assert "dataTable" in resp.text
    assert "RAW_SUBMISSIONS" in resp.text
    assert "exportFilteredCSV" in resp.text

def test_admin_data_per_form_page(auth_headers):
    client = TestClient(app)
    resp = client.get("/admin/data/income_survey", headers=auth_headers)
    assert resp.status_code == 200
    assert "Trung Tâm Dữ Liệu Khảo Sát" in resp.text
    assert "income_survey" in resp.text
