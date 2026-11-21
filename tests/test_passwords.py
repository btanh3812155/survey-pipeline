"""Admin password hashing and HTTP Basic verification."""
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from pydantic import ValidationError

from config import Settings, settings as runtime_settings
from core.passwords import (
    hash_password,
    is_supported_password_hash,
    verify_password,
)
from core.state import verify_admin_auth


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
async def test_admin_auth_uses_hash(monkeypatch):
    encoded = hash_password("admin-secret", iterations=1_000)
    monkeypatch.setattr(runtime_settings, "ADMIN_PASSWORD_HASH", encoded)
    monkeypatch.setattr(runtime_settings, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(runtime_settings, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr("core.state.get_db_pool", lambda: None)

    assert await verify_admin_auth(
        HTTPBasicCredentials(username="admin", password="admin-secret")
    ) == "admin"
    with pytest.raises(HTTPException) as exc:
        await verify_admin_auth(HTTPBasicCredentials(username="admin", password="wrong"))
    assert exc.value.status_code == 401
