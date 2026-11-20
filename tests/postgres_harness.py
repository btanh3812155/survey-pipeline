# -*- coding: utf-8 -*-
"""Isolated PostgreSQL/pgvector test harness helpers.

Wave 0 only: this module deliberately has no application imports, no production
credentials fallback, and no network/provider clients. Tests must opt in through
``UPGRADE_TEST_DATABASE_URL``; the effective libpq database name must end in
``_test`` and every effective host/address is validated, not just the URL text.
"""

import ipaddress
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema.sql"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres"}
PRODUCTION_HOSTS = {"146.190.201.32", "survey.trustbridgevn.com", "admin.trustbridgevn.com"}
PRODUCTION_NAME_MARKERS = ("production", "prod", "surveydb")
UNSUPPORTED_ROUTING_KEYS = {"service", "passfile"}

# libpq reads these even when the supplied conninfo omits their fields. The
# harness must not inherit a routing/auth target from the process environment.
INHERITED_LIBPQ_ENV = (
    "PGHOST",
    "PGHOSTADDR",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGPASSFILE",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGSYSCONFDIR",
    "PGOPTIONS",
    "PGSSLMODE",
)


def _reject_inherited_libpq_env() -> None:
    present = [key for key in INHERITED_LIBPQ_ENV if os.environ.get(key)]
    if present:
        raise RuntimeError(
            "refusing inherited libpq environment routing/auth: " + ", ".join(present)
        )


def _validate_effective_params(params: Dict[str, Any], original: str) -> None:
    """Validate what libpq will actually use, including query-string overrides."""
    for key in UNSUPPORTED_ROUTING_KEYS:
        if params.get(key):
            raise RuntimeError(f"unsupported PostgreSQL routing parameter {key!r}")

    database = str(params.get("dbname") or "")
    if not database:
        raise RuntimeError("PostgreSQL test target has no effective dbname")
    if not params.get("host") and not params.get("hostaddr"):
        raise RuntimeError("PostgreSQL test target must specify an explicit host or hostaddr")
    if not database.endswith("_test"):
        raise RuntimeError(f"isolated test database must end in _test, got {database!r}")
    if any(marker in database.lower() for marker in PRODUCTION_NAME_MARKERS):
        raise RuntimeError(f"refusing production-looking test database {database!r}")

    hosts = str(params.get("host") or "").split(",")
    for host in (value.strip() for value in hosts):
        if not host:
            continue
        lowered = host.lower()
        if lowered in PRODUCTION_HOSTS:
            raise RuntimeError(f"refusing production PostgreSQL host {host!r}")
        if lowered not in LOCAL_HOSTS and os.environ.get("UPGRADE_TEST_ALLOW_REMOTE") != "1":
            raise RuntimeError(
                f"remote PostgreSQL host {host!r} is not allowed; set UPGRADE_TEST_ALLOW_REMOTE=1 "
                "only for an explicitly disposable test server"
            )

    for address in (value.strip() for value in str(params.get("hostaddr") or "").split(",")):
        if not address:
            continue
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise RuntimeError(f"invalid PostgreSQL hostaddr {address!r}") from exc
        if str(ip) in PRODUCTION_HOSTS:
            raise RuntimeError(f"refusing production PostgreSQL hostaddr {address!r}")
        if not ip.is_loopback and os.environ.get("UPGRADE_TEST_ALLOW_REMOTE") != "1":
            raise RuntimeError(
                f"remote PostgreSQL hostaddr {address!r} is not allowed; set "
                "UPGRADE_TEST_ALLOW_REMOTE=1 only for an explicitly disposable test server"
            )

    # URL inputs must use a PostgreSQL scheme; conninfo inputs are parsed above.
    if "://" in original and not re.match(r"^postgres(ql)?://", original):
        raise RuntimeError("UPGRADE_TEST_DATABASE_URL must be a PostgreSQL URL")


def resolve_test_database_url(dsn: Optional[str] = None) -> str:
    """Return a normalized libpq conninfo after validating its effective target."""
    _reject_inherited_libpq_env()
    raw = (dsn or os.environ.get("UPGRADE_TEST_DATABASE_URL") or "").strip()
    if not raw:
        raise RuntimeError("UPGRADE_TEST_DATABASE_URL is required for PostgreSQL tests")
    parsed = urlparse(raw)
    if "://" in raw and parsed.scheme not in ("postgres", "postgresql"):
        raise RuntimeError("UPGRADE_TEST_DATABASE_URL must be a PostgreSQL URL")
    try:
        params = conninfo_to_dict(raw)
    except Exception as exc:
        raise RuntimeError(f"invalid PostgreSQL connection string: {exc}") from exc
    _validate_effective_params(params, raw)
    return make_conninfo(**params)


def connect(dsn: Optional[str] = None) -> psycopg.Connection:
    """Validate the effective target at every boundary, then open a real connection."""
    return psycopg.connect(resolve_test_database_url(dsn), autocommit=False)


def apply_schema(conn: psycopg.Connection) -> None:
    """Apply the checked-in schema to the disposable database (not a production migration)."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with conn.transaction():
        conn.execute(sql)


def database_name(conn: psycopg.Connection) -> str:
    row = conn.execute("SELECT current_database()").fetchone()
    return str(row[0] if row else "")


def has_pgvector(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()
    return bool(row and row[0])


def reset_harness_table(conn: psycopg.Connection, table: str = "wave0_harness_rows") -> None:
    """Create/truncate a small non-production table used to prove connection behavior."""
    if not re.fullmatch(r"wave0_[a-z0-9_]+", table):
        raise ValueError("harness table must start with wave0_ and contain only [a-z0-9_]")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY)")
    conn.execute(f"TRUNCATE {table}")
    conn.commit()
