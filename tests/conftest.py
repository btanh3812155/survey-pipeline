# -*- coding: utf-8 -*-
"""Shared pytest configuration; no application lifecycle or provider startup."""


import pytest


@pytest.fixture
def admin_basic_auth(monkeypatch):
    """Provide a real-looking DB admin row for non-auth unit tests."""
    from config import settings
    from core import state
    from core.passwords import hash_password

    encoded = hash_password(settings.ADMIN_PASSWORD, iterations=1000)

    async def load(username):
        if username != settings.ADMIN_USERNAME:
            return None
        return {
            "password_hash": encoded,
            "is_active": True,
            "role": "superadmin",
            "deleted_at": None,
        }

    monkeypatch.setattr(state, "_load_admin_account", load)
    monkeypatch.setattr(state, "get_redis_client", lambda: None)
    return encoded


@pytest.fixture
def telegram_webhook_client(monkeypatch):
    from fastapi.testclient import TestClient
    from config import settings
    from main import app

    secret = "callback-test-secret"
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", secret)
    return TestClient(app, headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "postgres: requires a disposable real PostgreSQL/pgvector database "
        "supplied through UPGRADE_TEST_DATABASE_URL",
    )
