# -*- coding: utf-8 -*-
"""Shared pytest configuration; no application lifecycle or provider startup."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "postgres: requires a disposable real PostgreSQL/pgvector database "
        "supplied through UPGRADE_TEST_DATABASE_URL",
    )
