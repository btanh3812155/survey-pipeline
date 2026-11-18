import pytest
import base64
from fastapi.testclient import TestClient
from main import app


@pytest.fixture
def auth_headers():
    token = base64.b64encode(b"admin:12345678").decode("utf-8")
    return {"Authorization": f"Basic {token}"}


def test_submissions_viewer_unauthorized():
    client = TestClient(app)
    resp = client.get("/admin/submissions")
    assert resp.status_code == 401


def test_submissions_viewer_authorized(auth_headers):
    client = TestClient(app)
    resp = client.get("/admin/submissions", headers=auth_headers)
    assert resp.status_code == 200
    assert "Dữ Liệu Khảo Sát Đã Nộp" in resp.text
    assert "KPI Summary Cards" in resp.text or "stats-grid" in resp.text


def test_submissions_api_endpoint(auth_headers):
    client = TestClient(app)
    resp = client.get("/api/v1/submissions", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "count" in data
    assert "submissions" in data
    assert "summary" in data
    assert "total" in data["summary"]


def test_submissions_subdomain_routing():
    client = TestClient(app)
    # When accessing root / with a submissions/view subdomain header, it redirects to /admin/submissions
    resp = client.get("/", headers={"Host": "submissions.146.190.201.32.sslip.io"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers.get("location") == "/admin/submissions"


def test_admin_subdomain_routing_defaults_to_forms():
    client = TestClient(app)
    # The admin host lands on the forms list, not the submissions viewer.
    resp = client.get("/", headers={"Host": "admin.trustbridgevn.com"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers.get("location") == "/admin/forms"
