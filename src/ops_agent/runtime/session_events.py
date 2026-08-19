from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator

from ..config import Settings


class SessionEvent(BaseModel):
    session_id: str
    sequence: int
    tenant_id: str
    user_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str

    @field_validator("payload", mode="before")
    @classmethod
    def remove_legacy_seller_fields(cls, value: Any) -> dict[str, Any]:
        def clean(item: Any) -> Any:
            if isinstance(item, dict):
                return {
                    key: clean(child)
                    for key, child in item.items()
                    if key not in {"seller_id", "seller_ids"}
                }
            if isinstance(item, list):
                return [clean(child) for child in item]
            return item

        return clean(value) if isinstance(value, dict) else {}


class SessionSummary(BaseModel):
    session_id: str
    title: str
    updated_at: str


class SessionEventStore(Protocol):
    def append(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> SessionEvent: ...

    def list_events(
        self, *, session_id: str, tenant_id: str
    ) -> list[SessionEvent]: ...

    def list_sessions(
        self, *, tenant_id: str, user_id: str, limit: int = 50
    ) -> list[SessionSummary]: ...

    def delete_session(self, *, session_id: str, tenant_id: str) -> int: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_summaries(
    events: list[SessionEvent], limit: int
) -> list[SessionSummary]:
    grouped: dict[str, dict[str, Any]] = {}
    for event in events:
        item = grouped.setdefault(
            event.session_id,
            {"title": "", "updated_at": event.created_at, "is_child": False},
        )
        if event.created_at > item["updated_at"]:
            item["updated_at"] = event.created_at
        if event.event_type == "session.created":
            item["is_child"] = bool(event.payload.get("parent_session_id"))
        elif event.event_type == "user.message" and not item["title"]:
            content = event.payload.get("content", "")
            item["title"] = content.strip() if isinstance(content, str) else str(content)
    summaries = [
        SessionSummary(
            session_id=session_id,
            title=(str(item["title"]) or "未命名会话")[:80],
            updated_at=str(item["updated_at"]),
        )
        for session_id, item in grouped.items()
        if not item["is_child"]
    ]
    summaries.sort(key=lambda item: item.updated_at, reverse=True)
    return summaries[:limit]


class SQLiteSessionEventStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_session_events(
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_session_events_tenant
                    ON agent_session_events(tenant_id, session_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_agent_session_events_user
                    ON agent_session_events(tenant_id, user_id, created_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def append(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> SessionEvent:
        created_at = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT coalesce(max(sequence), 0) + 1 AS next_sequence
                FROM agent_session_events WHERE session_id=?
                """,
                (session_id,),
            ).fetchone()
            sequence = int(row["next_sequence"])
            connection.execute(
                """
                INSERT INTO agent_session_events(
                    session_id, sequence, tenant_id, user_id, event_type,
                    payload_json, created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    session_id,
                    sequence,
                    tenant_id,
                    user_id,
                    event_type,
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                    created_at,
                ),
            )
        return SessionEvent(
            session_id=session_id,
            sequence=sequence,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type=event_type,
            payload=payload or {},
            created_at=created_at,
        )

    def list_events(
        self, *, session_id: str, tenant_id: str
    ) -> list[SessionEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_session_events
                WHERE session_id=? AND tenant_id=? ORDER BY sequence
                """,
                (session_id, tenant_id),
            ).fetchall()
        return [
            SessionEvent(
                session_id=row["session_id"],
                sequence=row["sequence"],
                tenant_id=row["tenant_id"],
                user_id=row["user_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def list_sessions(
        self, *, tenant_id: str, user_id: str, limit: int = 50
    ) -> list[SessionSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_session_events
                WHERE tenant_id=? AND user_id=? ORDER BY session_id, sequence
                """,
                (tenant_id, user_id),
            ).fetchall()
        events = [
            SessionEvent(
                session_id=row["session_id"],
                sequence=row["sequence"],
                tenant_id=row["tenant_id"],
                user_id=row["user_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]
        return _session_summaries(events, limit)

    def delete_session(self, *, session_id: str, tenant_id: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM agent_session_events
                WHERE session_id=? AND tenant_id=?
                """,
                (session_id, tenant_id),
            )
            return int(cursor.rowcount or 0)


class PostgresSessionEventStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_session_events(
                    session_id TEXT NOT NULL,
                    sequence BIGINT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY(session_id, sequence)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_session_events_tenant
                ON agent_session_events(tenant_id, session_id, sequence)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_session_events_user
                ON agent_session_events(tenant_id, user_id, created_at)
                """
            )

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    def append(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> SessionEvent:
        from psycopg.types.json import Jsonb

        with self._connect() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (session_id,),
            )
            row = connection.execute(
                """
                INSERT INTO agent_session_events(
                    session_id, sequence, tenant_id, user_id, event_type, payload_json
                )
                SELECT %s, coalesce(max(sequence), 0) + 1, %s, %s, %s, %s
                FROM agent_session_events WHERE session_id=%s
                RETURNING *
                """,
                (
                    session_id,
                    tenant_id,
                    user_id,
                    event_type,
                    Jsonb(payload or {}),
                    session_id,
                ),
            ).fetchone()
        return self._row(row)

    def list_events(
        self, *, session_id: str, tenant_id: str
    ) -> list[SessionEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_session_events
                WHERE session_id=%s AND tenant_id=%s ORDER BY sequence
                """,
                (session_id, tenant_id),
            ).fetchall()
        return [self._row(row) for row in rows]

    def list_sessions(
        self, *, tenant_id: str, user_id: str, limit: int = 50
    ) -> list[SessionSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_session_events
                WHERE tenant_id=%s AND user_id=%s ORDER BY session_id, sequence
                """,
                (tenant_id, user_id),
            ).fetchall()
        return _session_summaries([self._row(row) for row in rows], limit)

    def delete_session(self, *, session_id: str, tenant_id: str) -> int:
        with self._connect() as connection:
            result = connection.execute(
                """
                DELETE FROM agent_session_events
                WHERE session_id=%s AND tenant_id=%s
                """,
                (session_id, tenant_id),
            )
            return int(result.rowcount or 0)

    @staticmethod
    def _row(row: dict[str, Any]) -> SessionEvent:
        return SessionEvent(
            session_id=row["session_id"],
            sequence=row["sequence"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            event_type=row["event_type"],
            payload=row["payload_json"],
            created_at=row["created_at"].isoformat(),
        )


def create_session_event_store(settings: Settings) -> SessionEventStore:
    if settings.session_event_backend == "postgres":
        return PostgresSessionEventStore(settings.postgres_dsn)
    return SQLiteSessionEventStore(settings.session_event_path)
