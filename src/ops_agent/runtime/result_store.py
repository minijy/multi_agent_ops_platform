from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from ..config import Settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _number_text(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.000001")) if value.as_tuple().exponent < -6 else value
    return format(normalized.normalize(), "f")


def calculate_result_profile(
    rows: list[dict[str, Any]],
    *,
    columns: list[str] | None = None,
    source_rows: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved_columns = list(columns or [])
    if not resolved_columns and rows:
        resolved_columns = list(rows[0])
    nulls: dict[str, int] = {}
    numeric: dict[str, dict[str, Any]] = {}
    for name in resolved_columns[:50]:
        values = [row.get(name) for row in rows]
        missing = sum(value is None or value == "" for value in values)
        if missing:
            nulls[name] = missing
        numbers = [number for value in values if (number := _decimal(value)) is not None]
        if numbers and len(numbers) >= max(1, len(values) - missing):
            total = sum(numbers, Decimal(0))
            numeric[name] = {
                "count": len(numbers),
                "sum": _number_text(total),
                "min": _number_text(min(numbers)),
                "max": _number_text(max(numbers)),
                "avg": _number_text(total / len(numbers)),
            }
    statistics = {
        "numeric_columns": dict(list(numeric.items())[:20]),
    }
    quality = {
        "returned_rows": len(rows),
        "source_rows": source_rows if source_rows is not None else len(rows),
        "column_count": len(resolved_columns),
        "null_cells": sum(nulls.values()),
        "nulls_by_column": dict(list(nulls.items())[:20]),
    }
    return statistics, quality


@dataclass(frozen=True)
class StoredResult:
    result_ref: str
    tenant_id: str
    user_id: str
    session_id: str
    tool_name: str
    payload: dict[str, Any]
    created_at: str


class ResultStore(Protocol):
    def put(self, record: StoredResult) -> None: ...

    def get(self, result_ref: str, tenant_id: str) -> StoredResult | None: ...

    def delete_session(self, session_id: str, tenant_id: str) -> int: ...


class SQLiteResultStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_tool_results(
                    result_ref TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tool_results_session
                    ON agent_tool_results(tenant_id, session_id, created_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def put(self, record: StoredResult) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_tool_results(
                    result_ref,tenant_id,user_id,session_id,tool_name,payload_json,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    record.result_ref,
                    record.tenant_id,
                    record.user_id,
                    record.session_id,
                    record.tool_name,
                    json.dumps(record.payload, ensure_ascii=False, default=str),
                    record.created_at,
                ),
            )

    def get(self, result_ref: str, tenant_id: str) -> StoredResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_tool_results WHERE result_ref=? AND tenant_id=?",
                (result_ref, tenant_id),
            ).fetchone()
        if row is None:
            return None
        return StoredResult(
            result_ref=row["result_ref"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            tool_name=row["tool_name"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
        )

    def delete_session(self, session_id: str, tenant_id: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM agent_tool_results WHERE session_id=? AND tenant_id=?",
                (session_id, tenant_id),
            )
            return int(cursor.rowcount or 0)


class PostgresResultStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_tool_results(
                    result_ref TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    payload_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_agent_tool_results_session
                ON agent_tool_results(tenant_id,session_id,created_at)"""
            )

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    def put(self, record: StoredResult) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_tool_results(
                    result_ref,tenant_id,user_id,session_id,tool_name,payload_json,created_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    record.result_ref,
                    record.tenant_id,
                    record.user_id,
                    record.session_id,
                    record.tool_name,
                    Jsonb(record.payload),
                    record.created_at,
                ),
            )

    def get(self, result_ref: str, tenant_id: str) -> StoredResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_tool_results WHERE result_ref=%s AND tenant_id=%s",
                (result_ref, tenant_id),
            ).fetchone()
        if row is None:
            return None
        return StoredResult(
            result_ref=row["result_ref"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            tool_name=row["tool_name"],
            payload=row["payload_json"],
            created_at=row["created_at"].isoformat(),
        )

    def delete_session(self, session_id: str, tenant_id: str) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "DELETE FROM agent_tool_results WHERE session_id=%s AND tenant_id=%s",
                    (session_id, tenant_id),
                ).rowcount
                or 0
            )


def create_result_store(settings: Settings) -> ResultStore:
    if settings.session_event_backend == "postgres":
        return PostgresResultStore(settings.postgres_dsn)
    return SQLiteResultStore(settings.session_event_path)


def materialize_tool_output(
    store: ResultStore,
    output: Any,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    tool_name: str,
    preview_rows: int,
) -> Any:
    if not isinstance(output, dict) or not isinstance(output.get("rows"), list):
        return output
    full = dict(output)
    rows = [dict(row) for row in full.get("rows", []) if isinstance(row, dict)]
    columns = [str(item) for item in full.get("columns", [])]
    source_rows_raw = full.get("total_rows", full.get("total"))
    try:
        source_rows = int(source_rows_raw) if source_rows_raw is not None else len(rows)
    except (TypeError, ValueError):
        source_rows = len(rows)
    statistics, quality = calculate_result_profile(
        rows, columns=columns, source_rows=source_rows
    )
    statistics = {**statistics, **dict(full.get("statistics") or {})}
    quality = {**quality, **dict(full.get("data_quality") or {})}
    full["statistics"] = statistics
    full["data_quality"] = quality
    result_ref = f"result-{uuid.uuid4().hex}"
    store.put(
        StoredResult(
            result_ref=result_ref,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            tool_name=tool_name,
            payload=full,
            created_at=_now(),
        )
    )
    projection = {key: value for key, value in full.items() if key != "rows"}
    projection.update(
        {
            "result_ref": result_ref,
            "result_endpoint": f"/v1/agent/results/{result_ref}",
            "returned_rows": len(rows),
            "rows": rows[:preview_rows],
            "rows_truncated": len(rows) > preview_rows,
        }
    )
    return projection


def result_page(record: StoredResult, *, offset: int, limit: int) -> dict[str, Any]:
    payload = record.payload
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    page = rows[offset : offset + limit]
    return {
        "result_ref": record.result_ref,
        "session_id": record.session_id,
        "tool_name": record.tool_name,
        "columns": payload.get("columns") or (list(page[0]) if page else []),
        "rows": page,
        "offset": offset,
        "limit": limit,
        "returned_rows": len(rows),
        "source_rows": (payload.get("data_quality") or {}).get("source_rows", len(rows)),
        "has_more": offset + len(page) < len(rows),
        "summary": payload.get("summary", ""),
        "statistics": payload.get("statistics", {}),
        "data_quality": payload.get("data_quality", {}),
        "calculation": payload.get("calculation", {}),
        "created_at": record.created_at,
    }
