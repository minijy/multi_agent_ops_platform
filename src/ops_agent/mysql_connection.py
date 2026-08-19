from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlparse


MYSQL_SCHEMES = {"mysql", "mysql+pymysql"}


def mysql_connection_options(dsn: str, *, timeout_ms: int) -> dict[str, Any]:
    parsed = urlparse(str(dsn or "").strip())
    if parsed.scheme.lower() not in MYSQL_SCHEMES:
        raise ValueError("MySQL DSN 必须以 mysql:// 开头")
    database = unquote(parsed.path.lstrip("/"))
    if not parsed.hostname or not database:
        raise ValueError("MySQL DSN 必须包含 host 和 database")
    query = parse_qs(parsed.query)
    charset = str(query.get("charset", ["utf8mb4"])[0] or "utf8mb4")
    timeout_seconds = max(1, int((timeout_ms + 999) / 1000))
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": database,
        "charset": charset,
        "connect_timeout": timeout_seconds,
        "read_timeout": timeout_seconds,
        "write_timeout": timeout_seconds,
        "autocommit": False,
    }


@contextmanager
def mysql_read_only_connection(
    dsn: str, *, timeout_ms: int
) -> Iterator[Any]:
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:  # pragma: no cover - depends on deployment extras
        raise RuntimeError(
            "MySQL 驱动未安装，请安装项目依赖后重启服务"
        ) from exc

    options = mysql_connection_options(dsn, timeout_ms=timeout_ms)
    connection = pymysql.connect(**options, cursorclass=DictCursor)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
        connection.begin()
        yield connection
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()
