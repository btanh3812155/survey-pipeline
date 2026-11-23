import pytest
import base64
from fastapi.testclient import TestClient
from main import app
from core.state import verify_admin_auth


@pytest.fixture
def auth_headers():
    token = base64.b64encode(b"admin:12345678").decode("utf-8")
    app.dependency_overrides[verify_admin_auth] = lambda: "viewer-test-admin"
    yield {"Authorization": f"Basic {token}"}
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/admin/submissions", "/data"])
def test_submissions_viewer_unauthorized(path):
    client = TestClient(app)
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login?next=")


def test_submissions_viewer_authorized(auth_headers):
    client = TestClient(app)
    resp = client.get("/admin/submissions", headers=auth_headers)
    assert resp.status_code == 200


def test_submissions_api_endpoint(auth_headers):
    client = TestClient(app)
    resp = client.get("/api/v1/submissions", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "count" in data
    assert "submissions" in data
    assert "summary" in data
    assert "total" in data["summary"]


def test_submissions_subdomain_retired():
    client = TestClient(app)
    # The submissions surface is retired: root on those hosts no longer lands on /admin/submissions.
    for host in (
        "submissions.146.190.201.32.sslip.io",
        "submissions.trustbridgevn.com",
        "dev-submissions.trustbridgevn.com",
        "submissions-dev.trustbridgevn.com",
    ):
        resp = client.get("/", headers={"Host": host}, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers.get("location") != "/admin/submissions"
        assert resp.headers.get("location", "").startswith("/forms/")


def test_admin_subdomain_routing_defaults_to_forms():
    client = TestClient(app)
    # The admin host lands on the forms list, not the submissions viewer.
    resp = client.get("/", headers={"Host": "admin.trustbridgevn.com"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers.get("location") == "/admin/forms"
