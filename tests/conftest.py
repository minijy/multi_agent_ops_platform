from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pytest

_PLATFORM_TABLE_PREFIXES = ("ops_", "agent_", "memory_")


def _dsn_from_env() -> str:
    for key in ("TEST_POSTGRES_DSN", "POSTGRES_DSN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("POSTGRES_DSN="):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return "postgresql://ops_agent:ops_agent@127.0.0.1:5432/ops_agent"


def _with_search_path(dsn: str, schema: str) -> str:
    parsed = urlparse(dsn)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    extra = f"-csearch_path={schema}"
    current = query.get("options", "").strip()
    query["options"] = f"{current} {extra}".strip()
    return urlunparse(parsed._replace(query=urlencode(query)))


def _ensure_migrated(dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    previous = os.environ.get("POSTGRES_DSN")
    os.environ["POSTGRES_DSN"] = dsn
    try:
        config = Config(str(root / "alembic.ini"))
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("POSTGRES_DSN", None)
        else:
            os.environ["POSTGRES_DSN"] = previous


def _clone_platform_tables(connection, schema: str) -> None:
    rows = connection.execute(
        """
        SELECT tablename FROM pg_tables
        WHERE schemaname='public'
        ORDER BY tablename
        """
    ).fetchall()
    for row in rows:
        name = str(row[0])
        if not name.startswith(_PLATFORM_TABLE_PREFIXES):
            continue
        connection.execute(
            f'CREATE TABLE "{schema}"."{name}" '
            f'(LIKE public."{name}" INCLUDING ALL EXCLUDING CONSTRAINTS)'
        )


@pytest.fixture(scope="session")
def postgres_admin_dsn() -> str:
    import psycopg

    dsn = _dsn_from_env()
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            connection.execute("SELECT 1")
    except Exception as exc:
        pytest.exit(
            "PostgreSQL is required; SQLite has been removed. "
            "Start `docker compose -f docker-compose.postgres.yml up -d` "
            "or set TEST_POSTGRES_DSN. "
            f"Last error: {exc}",
            returncode=1,
        )
    _ensure_migrated(dsn)
    return dsn


@pytest.fixture
def postgres_dsn(postgres_admin_dsn: str):
    import psycopg

    schema = f"t{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_admin_dsn, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
        _clone_platform_tables(connection, schema)
    try:
        yield _with_search_path(postgres_admin_dsn, schema)
    finally:
        with psycopg.connect(postgres_admin_dsn, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
