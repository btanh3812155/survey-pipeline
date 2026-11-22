"""Admin password hashing and HTTP Basic verification."""
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from pydantic import ValidationError

from config import Settings
from core.passwords import (
    hash_password,
    is_supported_password_hash,
    verify_password,
)
from core.state import _admin_roles_for_path, require_admin_same_origin, verify_admin_auth


def test_password_hash_roundtrip():
    encoded = hash_password("correct horse battery staple", iterations=1_000)
    assert encoded.startswith("pbkdf2_sha256:1000:")
    assert verify_password("correct horse battery staple", encoded)
    legacy = "$".join(encoded.split(":"))
    assert verify_password("correct horse battery staple", legacy)
    assert not verify_password("wrong password", encoded)
    assert not verify_password("correct horse battery staple", "not-a-hash")


def test_password_hash_format_validation():
    encoded = hash_password("x" * 20, iterations=100_000)
    assert is_supported_password_hash(encoded, min_iterations=100_000)
    assert not is_supported_password_hash(encoded, min_iterations=100_001)
    assert not is_supported_password_hash("pbkdf2_sha256$abc$salt$digest")
    assert not is_supported_password_hash(None)


def _production_settings(**overrides):
    values = {
        "ENVIRONMENT": "production",
        "ADMIN_PASSWORD": "",
        "APP_SECRET": "a" * 48,
        "DATABASE_URL": "postgresql://user:pass@db:5432/db",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_accepts_hash_without_plaintext():
    encoded = hash_password("strong-password", iterations=100_000)
    cfg = _production_settings(ADMIN_PASSWORD_HASH=encoded)
    assert cfg.ADMIN_PASSWORD_HASH == encoded


def test_production_rejects_malformed_or_weak_hash():
    with pytest.raises(ValidationError):
        _production_settings(ADMIN_PASSWORD_HASH="nope")
    weak = hash_password("strong-password", iterations=1_000)
    with pytest.raises(ValidationError):
        _production_settings(ADMIN_PASSWORD_HASH=weak)


@pytest.mark.asyncio
async def test_admin_auth_uses_database_hash(monkeypatch):
    from unittest.mock import AsyncMock
    from starlette.requests import Request

    encoded = hash_password("admin-secret", iterations=1_000)
    monkeypatch.setattr("core.state._load_admin_account", AsyncMock(return_value={
        "password_hash": encoded, "is_active": True, "role": "superadmin", "deleted_at": None,
    }))
    monkeypatch.setattr("core.state._check_admin_auth_rate_limit", AsyncMock(return_value=(None, "", "")))
    request = Request({
        "type": "http", "method": "GET", "scheme": "https", "path": "/admin/forms",
        "headers": [(b"host", b"admin.test")],
    })
    credentials = HTTPBasicCredentials(username="admin", password="admin-secret")
    assert await verify_admin_auth(request, credentials) == "admin"
    assert request.state.admin_role == "superadmin"
    assert request.state.admin_password_hash == encoded
    with pytest.raises(HTTPException) as exc:
        await verify_admin_auth(request, HTTPBasicCredentials(username="admin", password="wrong"))
    assert exc.value.status_code == 401


def test_admin_role_map_fails_closed_and_scopes_writes():
    everyone = {"superadmin", "manager", "reviewer", "viewer"}
    operators = {"superadmin", "manager"}
    reviewers = {"superadmin", "manager", "reviewer"}
    assert _admin_roles_for_path("/admin/accounts/password", "POST") == everyone
    assert _admin_roles_for_path("/admin/accounts", "GET") == {"superadmin"}
    assert _admin_roles_for_path("/api/v1/submissions", "GET") == everyone
    assert _admin_roles_for_path("/api/v1/submissions/ABC/actions", "GET") == reviewers
    assert _admin_roles_for_path("/api/v1/submissions/ABC", "DELETE") == operators
    assert _admin_roles_for_path("/api/v1/unclassified-admin-tool", "GET") == {"superadmin"}


def test_admin_posts_require_exact_same_origin():
    from starlette.requests import Request

    def request(headers):
        return Request({
            "type": "http", "method": "POST", "scheme": "https", "path": "/admin/accounts",
            "headers": [(b"host", b"admin.test"), *headers],
        })

    require_admin_same_origin(request([(b"origin", b"https://admin.test")]))
    for headers in (
        [],
        [(b"origin", b"http://admin.test")],
        [(b"origin", b"https://attacker.test")],
        [(b"origin", b"https://admin.test@attacker.test")],
        [(b"origin", b"https://admin.test"), (b"sec-fetch-site", b"cross-site")],
    ):
        with pytest.raises(HTTPException) as denied:
            require_admin_same_origin(request(headers))
        assert denied.value.status_code == 403
