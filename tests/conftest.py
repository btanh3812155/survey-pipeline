# -*- coding: utf-8 -*-
"""Shared pytest configuration; no application lifecycle or provider startup."""


import pytest


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
