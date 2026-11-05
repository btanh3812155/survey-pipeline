import json
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
import pytest
from fastapi.testclient import TestClient

from main import app, DEFAULT_GEO_FALLBACK


@pytest.fixture
def client():
    return TestClient(app)


def test_geo_ip_fallback_returns_200_and_expected_fields(client):
    """Calling /api/v1/geo/ip-fallback returns 200 with latitude, longitude, accuracy, and status: success."""
    resp = client.get("/api/v1/geo/ip-fallback")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert isinstance(data["latitude"], float)
    assert isinstance(data["longitude"], float)
    assert isinstance(data["accuracy"], float)
    assert isinstance(data["city"], str)
    assert data["source"] == "ip_fallback"
    # Default coordinates for test client (loopback/private host)
    assert data["latitude"] == DEFAULT_GEO_FALLBACK["latitude"]
    assert data["longitude"] == DEFAULT_GEO_FALLBACK["longitude"]
    assert data["city"] == DEFAULT_GEO_FALLBACK["city"]


def test_geo_ip_fallback_handles_loopback_ips(client):
    """Handles loopback IP addresses (IPv4 and IPv6) gracefully without crash."""
    for loopback_ip in ["127.0.0.1", "127.0.0.2", "127.1.2.3", "::1"]:
        resp = client.get("/api/v1/geo/ip-fallback", headers={"X-Forwarded-For": loopback_ip})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["latitude"] == DEFAULT_GEO_FALLBACK["latitude"]
        assert data["longitude"] == DEFAULT_GEO_FALLBACK["longitude"]
        assert data["accuracy"] == DEFAULT_GEO_FALLBACK["accuracy"]
        assert data["city"] == DEFAULT_GEO_FALLBACK["city"]
        assert data["source"] == "ip_fallback"


def test_geo_ip_fallback_handles_private_subnets(client):
    """Handles private subnets (10.x, 192.168.x, 172.16.x) gracefully returning default Vietnam coordinates."""
    private_ips = [
        "10.0.0.1",
        "10.254.0.1",
        "192.168.1.1",
        "192.168.100.50",
        "172.16.0.1",
        "172.31.255.254",
        "169.254.169.254",  # Link-local / metadata IP
    ]
    for priv_ip in private_ips:
        resp = client.get("/api/v1/geo/ip-fallback", headers={"X-Forwarded-For": priv_ip})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["latitude"] == DEFAULT_GEO_FALLBACK["latitude"]
        assert data["longitude"] == DEFAULT_GEO_FALLBACK["longitude"]


def test_geo_ip_fallback_cloudflare_headers(client):
    """Parses Cloudflare / Caddy headers (cf-iplatitude, cf-iplongitude, cf-ipcity) when provided."""
    headers = {
        "X-Forwarded-For": "203.0.113.195",
        "cf-iplatitude": "10.8231",
        "cf-iplongitude": "106.6297",
        "cf-ipcity": "Ho Chi Minh City",
    }
    resp = client.get("/api/v1/geo/ip-fallback", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["latitude"] == 10.8231
    assert data["longitude"] == 106.6297
    assert data["city"] == "Ho Chi Minh City"
    assert data["accuracy"] == 5000.0
    assert data["source"] == "ip_fallback"


def test_geo_ip_fallback_public_ip_api_lookup(client):
    """Queries ip-api.com for public IPs and caches result in Redis if available."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "success",
        "country": "Vietnam",
        "city": "Da Nang",
        "lat": 16.0544,
        "lon": 108.2022,
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.is_closed = False

    with patch("main.http_client", mock_client), patch("main.redis_client", None):
        resp = client.get("/api/v1/geo/ip-fallback", headers={"X-Forwarded-For": "14.161.0.1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["latitude"] == 16.0544
        assert data["longitude"] == 108.2022
        assert data["city"] == "Da Nang"
        assert data["accuracy"] == 5000.0
        assert data["source"] == "ip_fallback"
        mock_client.get.assert_called_once()


def test_geo_ip_fallback_redis_cache_hit(client):
    """Returns cached coordinates from Redis when present without making external HTTP requests."""
    mock_redis = AsyncMock()
    cached_payload = json.dumps({
        "latitude": 10.762622,
        "longitude": 106.660172,
        "accuracy": 3000.0,
        "city": "Ho Chi Minh City",
    })
    mock_redis.get = AsyncMock(return_value=cached_payload)

    with patch("main.redis_client", mock_redis):
        resp = client.get("/api/v1/geo/ip-fallback", headers={"X-Forwarded-For": "113.160.0.1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["latitude"] == 10.762622
        assert data["longitude"] == 106.660172
        assert data["accuracy"] == 3000.0
        assert data["city"] == "Ho Chi Minh City"
        assert data["source"] == "ip_fallback"
        mock_redis.get.assert_called_once_with("geo:ip:113.160.0.1")


def test_geo_ip_fallback_network_failure_never_500(client):
    """Network failure or timeout must never raise 500 error; always gracefully returns default Vietnam coordinates."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectTimeout("Connection timed out to ip-api"))
    mock_client.is_closed = False

    with patch("main.http_client", mock_client), patch("main.redis_client", None):
        resp = client.get("/api/v1/geo/ip-fallback", headers={"X-Forwarded-For": "14.161.0.1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["latitude"] == DEFAULT_GEO_FALLBACK["latitude"]
        assert data["longitude"] == DEFAULT_GEO_FALLBACK["longitude"]
        assert data["accuracy"] == DEFAULT_GEO_FALLBACK["accuracy"]
        assert data["city"] == DEFAULT_GEO_FALLBACK["city"]
        assert data["source"] == "ip_fallback"


def test_geo_ip_fallback_malformed_and_multi_hop_xff(client):
    """Handles malformed IP strings and multi-hop X-Forwarded-For gracefully."""
    # Malformed IP
    resp_bad = client.get("/api/v1/geo/ip-fallback", headers={"X-Forwarded-For": "totally_invalid_ip_string"})
    assert resp_bad.status_code == 200
    assert resp_bad.json()["status"] == "success"
    assert resp_bad.json()["latitude"] == DEFAULT_GEO_FALLBACK["latitude"]

    # Multi-hop XFF with loopback first
    resp_multi = client.get(
        "/api/v1/geo/ip-fallback",
        headers={"X-Forwarded-For": "127.0.0.1, 10.0.0.1, 203.0.113.1"}
    )
    assert resp_multi.status_code == 200
    assert resp_multi.json()["status"] == "success"
    assert resp_multi.json()["latitude"] == DEFAULT_GEO_FALLBACK["latitude"]

def test_geo_ip_fallback_live_public_ip_e2e(client):
    """Calling with a real public IP returns valid latitude and longitude."""
    resp = client.get("/api/v1/geo/ip-fallback", headers={"X-Forwarded-For": "1.1.1.1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert isinstance(data["latitude"], float)
    assert isinstance(data["longitude"], float)
    assert isinstance(data["accuracy"], float)
    assert isinstance(data["city"], str)
    assert data["source"] == "ip_fallback"
