"""Proxy-path regressions: peer DNS resolution and independent OTP abuse buckets."""
import socket
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from core import ctv_auth as auth
from core import server
from tests.test_ctv_auth import MemoryRedis


def _scope(peer, forwarded):
    return {
        "type": "http", "method": "POST", "path": "/ctv/login/request",
        "scheme": "http", "server": ("app-dev", 8000), "client": (peer, 1234),
        "headers": [(b"host", b"survey.example"), (b"x-forwarded-for", forwarded.encode()),
                    (b"x-forwarded-proto", b"https")],
    }


@pytest.mark.asyncio
async def test_docker_proxy_clients_keep_independent_otp_limits(monkeypatch):
    def resolve(host, port, *, type):
        assert host == "caddy" and type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.20.0.2", 0))]

    monkeypatch.setattr(server.socket, "getaddrinfo", resolve)
    peer_ips = server.resolve_proxy_ips("127.0.0.1,::1,caddy")
    received = []

    async def target(scope, receive, send):
        received.append(Request(scope))

    middleware = ProxyHeadersMiddleware(target, trusted_hosts=peer_ips)
    await middleware(_scope("172.20.0.2", "198.51.100.99, 203.0.113.11"), None, None)
    await middleware(_scope("172.20.0.2", "203.0.113.22"), None, None)
    attacker, other_user = received
    assert attacker.client.host == "203.0.113.11"
    assert other_user.client.host == "203.0.113.22"
    assert attacker.url.scheme == other_user.url.scheme == "https"
    cache = MemoryRedis([1_800_000_000])
    identity = AsyncMock(return_value=None)
    monkeypatch.setattr(auth, "get_redis_client", lambda: cache)
    monkeypatch.setattr(auth, "_identity", identity)
    for index in range(11):
        with pytest.raises(HTTPException) as failure:
            await auth.start_login(attacker, code="NOT_A_CTV")
        assert failure.value.status_code == (401 if index < 10 else 429)
    with pytest.raises(HTTPException) as failure:
        await auth.start_login(other_user, code="ANOTHER_CTV")
    assert failure.value.status_code == 401
    assert identity.await_count == 11


@pytest.mark.asyncio
async def test_untrusted_peer_cannot_spoof_limiter_identity_or_scheme():
    received = []

    async def target(scope, receive, send):
        received.append(Request(scope))

    middleware = ProxyHeadersMiddleware(target, trusted_hosts=server.resolve_proxy_ips("172.20.0.2"))
    await middleware(_scope("203.0.113.33", "203.0.113.44"), None, None)
    assert received[0].client.host == "203.0.113.33"
    assert received[0].url.scheme == "http"


@pytest.mark.parametrize("value", ["*", "172.20.0.0/16", "127.0.0.1,*"])
def test_proxy_config_rejects_implicit_network_trust(value):
    with pytest.raises(ValueError):
        server.resolve_proxy_ips(value)


def test_unknown_configured_proxy_fails_before_server_listens(monkeypatch):
    import uvicorn

    def unavailable(*args, **kwargs):
        raise socket.gaierror("unresolved configured peer")

    run = AsyncMock()
    monkeypatch.setattr(server.socket, "getaddrinfo", unavailable)
    monkeypatch.setattr(server.settings, "FORWARDED_ALLOW_IPS", "caddy")
    monkeypatch.setattr(uvicorn, "run", run)
    with pytest.raises(RuntimeError, match="Cannot resolve trusted proxy"):
        server.run_server()
    run.assert_not_called()
