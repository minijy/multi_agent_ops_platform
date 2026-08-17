from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlatformStore:
    """Small control-plane store for local development and a single API replica."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs(
                    thread_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    risk_level TEXT,
                    context_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_tenant_updated
                    ON runs(tenant_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_status
                    ON runs(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS audit_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_tenant_created
                    ON audit_events(tenant_id, created_at DESC);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def create_run(
        self,
        *,
        thread_id: str,
        tenant_id: str,
        user_id: str,
        objective: str,
        context: dict[str, Any],
    ) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    thread_id, tenant_id, user_id, objective, status, risk_level,
                    context_json, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    thread_id, tenant_id, user_id, objective, "started", None,
                    json.dumps(context, ensure_ascii=False), now, now,
                ),
            )

    def update_run(self, thread_id: str, state: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs SET status=?, risk_level=?, updated_at=? WHERE thread_id=?
                """,
                (
                    state.get("status", "unknown"), state.get("risk_level"), _now(), thread_id,
                ),
            )

    def get_run_record(self, thread_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE thread_id=?", (thread_id,)
            ).fetchone()
        return self._run_row(row) if row else None

    def list_runs(
        self, *, tenant_id: str, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs WHERE tenant_id=?"
        values: list[Any] = [tenant_id]
        if status:
            sql += " AND status=?"
            values.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [self._run_row(row) for row in rows]

    def summary(self, tenant_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            audit_total = connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE tenant_id=?",
                (tenant_id,),
            ).fetchone()[0]
        return {
            "audit_events": audit_total,
        }

    def audit(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        actor_role: str,
        action: str,
        resource_type: str,
        resource_id: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, actor_id, actor_role, action, resource_type,
                    resource_id, detail_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    tenant_id, actor_id, actor_role, action, resource_type, resource_id,
                    json.dumps(detail or {}, ensure_ascii=False, default=str), _now(),
                ),
            )

    def list_audit(self, *, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM audit_events WHERE tenant_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (tenant_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "tenant_id": row["tenant_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "detail": json.loads(row["detail_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "thread_id": row["thread_id"],
            "tenant_id": row["tenant_id"],
            "user_id": row["user_id"],
            "objective": row["objective"],
            "status": row["status"],
            "risk_level": row["risk_level"],
            "context": json.loads(row["context_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


class PostgresPlatformStore:
    """PostgreSQL control-plane store suitable for multiple API replicas."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ops_runs(
                        thread_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        objective TEXT NOT NULL,
                        status TEXT NOT NULL,
                        risk_level TEXT,
                        context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ops_runs_tenant_updated
                    ON ops_runs(tenant_id, updated_at DESC)
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ops_runs_status
                    ON ops_runs(status, updated_at DESC)
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ops_audit_events(
                        id BIGSERIAL PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        actor_role TEXT NOT NULL,
                        action TEXT NOT NULL,
                        resource_type TEXT NOT NULL,
                        resource_id TEXT NOT NULL,
                        detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ops_audit_tenant_created
                    ON ops_audit_events(tenant_id, created_at DESC)
                    """
                )

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    def create_run(
        self,
        *,
        thread_id: str,
        tenant_id: str,
        user_id: str,
        objective: str,
        context: dict[str, Any],
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ops_runs(
                    thread_id, tenant_id, user_id, objective, status, context_json
                ) VALUES(%s,%s,%s,%s,%s,%s)
                """,
                (thread_id, tenant_id, user_id, objective, "started", Jsonb(context)),
            )

    def update_run(self, thread_id: str, state: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ops_runs
                SET status=%s, risk_level=%s, updated_at=NOW()
                WHERE thread_id=%s
                """,
                (state.get("status", "unknown"), state.get("risk_level"), thread_id),
            )

    def get_run_record(self, thread_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ops_runs WHERE thread_id=%s", (thread_id,)
            ).fetchone()
        return self._run_row(row) if row else None

    def list_runs(
        self, *, tenant_id: str, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM ops_runs WHERE tenant_id=%s"
        values: list[Any] = [tenant_id]
        if status:
            sql += " AND status=%s"
            values.append(status)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [self._run_row(row) for row in rows]

    def summary(self, tenant_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            audit_total = connection.execute(
                "SELECT COUNT(*) AS amount FROM ops_audit_events WHERE tenant_id=%s",
                (tenant_id,),
            ).fetchone()["amount"]
        return {
            "audit_events": audit_total,
        }

    def audit(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        actor_role: str,
        action: str,
        resource_type: str,
        resource_id: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ops_audit_events(
                    tenant_id, actor_id, actor_role, action, resource_type,
                    resource_id, detail_json
                ) VALUES(%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    tenant_id, actor_id, actor_role, action, resource_type,
                    resource_id, Jsonb(detail or {}),
                ),
            )

    def list_audit(self, *, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM ops_audit_events WHERE tenant_id=%s
                ORDER BY id DESC LIMIT %s
                """,
                (tenant_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"], "tenant_id": row["tenant_id"],
                "actor_id": row["actor_id"], "actor_role": row["actor_role"],
                "action": row["action"], "resource_type": row["resource_type"],
                "resource_id": row["resource_id"], "detail": row["detail_json"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]

    @staticmethod
    def _run_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "thread_id": row["thread_id"], "tenant_id": row["tenant_id"],
            "user_id": row["user_id"], "objective": row["objective"],
            "status": row["status"], "risk_level": row["risk_level"],
            "context": row["context_json"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }


def create_platform_store(settings: Settings) -> PlatformStore | PostgresPlatformStore:
    if settings.control_plane_backend == "postgres":
        return PostgresPlatformStore(settings.postgres_dsn)
    return PlatformStore(settings.platform_db_path)
