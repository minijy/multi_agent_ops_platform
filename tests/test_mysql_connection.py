from __future__ import annotations

import pytest

from ops_agent.mysql_connection import mysql_connection_options


def test_mysql_connection_options_parse_dsn_and_timeouts():
    options = mysql_connection_options(
        "mysql://report%40user:p%40ss@mysql.example.com:3307/finance%20db?charset=utf8mb4",
        timeout_ms=1500,
    )

    assert options == {
        "host": "mysql.example.com",
        "port": 3307,
        "user": "report@user",
        "password": "p@ss",
        "database": "finance db",
        "charset": "utf8mb4",
        "connect_timeout": 2,
        "read_timeout": 2,
        "write_timeout": 2,
        "autocommit": False,
    }


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://reader:secret@db/analytics",
        "mysql://reader:secret@/analytics",
        "mysql://reader:secret@db/",
    ],
)
def test_mysql_connection_options_reject_invalid_dsn(dsn):
    with pytest.raises(ValueError):
        mysql_connection_options(dsn, timeout_ms=5000)
