from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from ..config import Settings
from .domain import ToolCall


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lease_expiry(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class ToolApprovalRecord(BaseModel):
    approval_id: str
    session_id: str
    tenant_id: str
    user_id: str
    role: str
    call: ToolCall
    status: Literal["pending", "approved", "rejected"]
    decided_by: str | None = None
    comment: str = ""
    created_at: str
    decided_at: str | None = None


class SubagentTaskRecord(BaseModel):
    task_id: str
    parent_session_id: str
    child_session_id: str
    tenant_id: str
    user_id: str
    role: str
    model_id: str | None = None
    connection_ids: list[str] = Field(default_factory=list)
    resource_scope: dict[str, list[str]] = Field(default_factory=dict)
    memory_snapshot: list[dict[str, Any]] = Field(default_factory=list)
    objective: str
    status: Literal[
        "queued",
        "running",
        "completed",
        "failed",
        "cancel_requested",
        "cancelled",
        "timed_out",
        "waiting_approval",
        "budget_exceeded",
    ]
    depth: int
    agent_id: str = "analyst"
    allowed_tools: list[str] = Field(default_factory=list)
    token_budget: int
    timeout_seconds: float
    answer: str = ""
    error: str | None = None
    worker_id: str | None = None
    lease_expires_at: str | None = None
    attempt: int = 0
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None


TERMINAL_SUBAGENT_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "timed_out",
        "waiting_approval",
        "budget_exceeded",
    }
)


class RuntimeGovernanceStore(Protocol):
    def create_approval(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        role: str,
        call: ToolCall,
    ) -> ToolApprovalRecord: ...

    def get_approval(
        self, approval_id: str, tenant_id: str
    ) -> ToolApprovalRecord | None: ...

    def list_pending_approvals(
        self, tenant_id: str
    ) -> list[ToolApprovalRecord]: ...

    def decide_approval(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        approved: bool,
        decided_by: str,
        comment: str,
    ) -> ToolApprovalRecord: ...

    def create_task(self, record: SubagentTaskRecord) -> None: ...
    def update_task(self, record: SubagentTaskRecord) -> None: ...
    def get_task(
        self, task_id: str, tenant_id: str
    ) -> SubagentTaskRecord | None: ...
    def list_tasks(
        self, tenant_id: str, parent_session_id: str | None = None
    ) -> list[SubagentTaskRecord]: ...
    def claim_next_task(
        self, *, worker_id: str, lease_seconds: float
    ) -> SubagentTaskRecord | None: ...
    def renew_lease(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: float,
    ) -> SubagentTaskRecord | None: ...
    def requeue_expired_leases(
        self, *, max_attempts: int
    ) -> int: ...


class SQLiteRuntimeGovernanceStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_tool_approvals(
                    approval_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tool_approvals_pending
                    ON agent_tool_approvals(tenant_id, status, created_at);
                CREATE TABLE IF NOT EXISTS agent_subagent_tasks(
                    task_id TEXT PRIMARY KEY,
                    parent_session_id TEXT NOT NULL,
                    child_session_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_subagent_tasks_parent
                    ON agent_subagent_tasks(tenant_id, parent_session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_subagent_tasks_status
                    ON agent_subagent_tasks(status, created_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def create_approval(
        self, *, session_id: str, tenant_id: str, user_id: str, role: str,
        call: ToolCall,
    ) -> ToolApprovalRecord:
        record = ToolApprovalRecord(
            approval_id=str(uuid.uuid4()), session_id=session_id,
            tenant_id=tenant_id, user_id=user_id, role=role, call=call,
            status="pending", created_at=_now(),
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_tool_approvals(
                    approval_id,session_id,tenant_id,payload_json,status,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (
                    record.approval_id, session_id, tenant_id,
                    record.model_dump_json(), record.status, record.created_at,
                ),
            )
        return record

    def get_approval(
        self, approval_id: str, tenant_id: str
    ) -> ToolApprovalRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM agent_tool_approvals WHERE approval_id=? AND tenant_id=?",
                (approval_id, tenant_id),
            ).fetchone()
        return ToolApprovalRecord.model_validate_json(row[0]) if row else None

    def list_pending_approvals(
        self, tenant_id: str
    ) -> list[ToolApprovalRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT payload_json FROM agent_tool_approvals
                WHERE tenant_id=? AND status='pending' ORDER BY created_at""",
                (tenant_id,),
            ).fetchall()
        return [ToolApprovalRecord.model_validate_json(row[0]) for row in rows]

    def decide_approval(
        self, *, approval_id: str, tenant_id: str, approved: bool,
        decided_by: str, comment: str,
    ) -> ToolApprovalRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT payload_json,status FROM agent_tool_approvals
                WHERE approval_id=? AND tenant_id=?""",
                (approval_id, tenant_id),
            ).fetchone()
            if not row:
                raise KeyError("approval not found")
            if row["status"] != "pending":
                raise ValueError("approval already decided")
            record = ToolApprovalRecord.model_validate_json(row["payload_json"])
            record = record.model_copy(
                update={
                    "status": "approved" if approved else "rejected",
                    "decided_by": decided_by,
                    "comment": comment,
                    "decided_at": _now(),
                }
            )
            connection.execute(
                """UPDATE agent_tool_approvals
                SET payload_json=?,status=? WHERE approval_id=?""",
                (record.model_dump_json(), record.status, approval_id),
            )
        return record

    def create_task(self, record: SubagentTaskRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_subagent_tasks(
                    task_id,parent_session_id,child_session_id,tenant_id,
                    payload_json,status,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    record.task_id, record.parent_session_id,
                    record.child_session_id, record.tenant_id,
                    record.model_dump_json(), record.status, record.created_at,
                ),
            )

    def update_task(self, record: SubagentTaskRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE agent_subagent_tasks SET payload_json=?,status=?
                WHERE task_id=? AND tenant_id=?""",
                (
                    record.model_dump_json(), record.status,
                    record.task_id, record.tenant_id,
                ),
            )

    def get_task(
        self, task_id: str, tenant_id: str
    ) -> SubagentTaskRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM agent_subagent_tasks WHERE task_id=? AND tenant_id=?",
                (task_id, tenant_id),
            ).fetchone()
        return SubagentTaskRecord.model_validate_json(row[0]) if row else None

    def list_tasks(
        self, tenant_id: str, parent_session_id: str | None = None
    ) -> list[SubagentTaskRecord]:
        query = "SELECT payload_json FROM agent_subagent_tasks WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if parent_session_id:
            query += " AND parent_session_id=?"
            params.append(parent_session_id)
        query += " ORDER BY created_at DESC LIMIT 200"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [SubagentTaskRecord.model_validate_json(row[0]) for row in rows]

    def claim_next_task(
        self, *, worker_id: str, lease_seconds: float
    ) -> SubagentTaskRecord | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT task_id, tenant_id, payload_json FROM agent_subagent_tasks
                WHERE status='queued' ORDER BY created_at LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            record = SubagentTaskRecord.model_validate_json(row["payload_json"])
            claimed = record.model_copy(
                update={
                    "status": "running",
                    "worker_id": worker_id,
                    "lease_expires_at": _lease_expiry(lease_seconds),
                    "attempt": record.attempt + 1,
                    "started_at": record.started_at or _now(),
                    "error": None,
                }
            )
            cursor = connection.execute(
                """UPDATE agent_subagent_tasks SET payload_json=?,status=?
                WHERE task_id=? AND status='queued'""",
                (claimed.model_dump_json(), claimed.status, claimed.task_id),
            )
            if cursor.rowcount == 0:
                return None
            return claimed

    def renew_lease(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: float,
    ) -> SubagentTaskRecord | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json,status FROM agent_subagent_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            record = SubagentTaskRecord.model_validate_json(row["payload_json"])
            if record.worker_id != worker_id:
                return None
            if record.status not in {"running", "cancel_requested"}:
                return record
            updated = record.model_copy(
                update={"lease_expires_at": _lease_expiry(lease_seconds)}
            )
            connection.execute(
                """UPDATE agent_subagent_tasks SET payload_json=?,status=?
                WHERE task_id=?""",
                (updated.model_dump_json(), updated.status, task_id),
            )
            return updated

    def requeue_expired_leases(self, *, max_attempts: int) -> int:
        now = datetime.now(timezone.utc)
        changed = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT task_id, payload_json FROM agent_subagent_tasks
                WHERE status IN ('running','cancel_requested')"""
            ).fetchall()
            for row in rows:
                record = SubagentTaskRecord.model_validate_json(row["payload_json"])
                expires = _parse_iso(record.lease_expires_at)
                if expires is None or expires > now:
                    continue
                if record.status == "cancel_requested":
                    updated = record.model_copy(
                        update={
                            "status": "cancelled",
                            "worker_id": None,
                            "lease_expires_at": None,
                            "completed_at": _now(),
                            "error": record.error or "cancelled after worker lease expiry",
                        }
                    )
                elif record.attempt >= max_attempts:
                    updated = record.model_copy(
                        update={
                            "status": "failed",
                            "worker_id": None,
                            "lease_expires_at": None,
                            "completed_at": _now(),
                            "error": "subagent lease expired too many times",
                        }
                    )
                else:
                    updated = record.model_copy(
                        update={
                            "status": "queued",
                            "worker_id": None,
                            "lease_expires_at": None,
                            "started_at": None,
                            "error": "requeued after worker lease expiry",
                        }
                    )
                connection.execute(
                    """UPDATE agent_subagent_tasks SET payload_json=?,status=?
                    WHERE task_id=?""",
                    (updated.model_dump_json(), updated.status, updated.task_id),
                )
                changed += 1
        return changed


class PostgresRuntimeGovernanceStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_tool_approvals(
                    approval_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL, payload_json JSONB NOT NULL,
                    status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_agent_tool_approvals_pending
                ON agent_tool_approvals(tenant_id,status,created_at)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_subagent_tasks(
                    task_id TEXT PRIMARY KEY, parent_session_id TEXT NOT NULL,
                    child_session_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    payload_json JSONB NOT NULL, status TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_agent_subagent_tasks_parent
                ON agent_subagent_tasks(tenant_id,parent_session_id,created_at)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_agent_subagent_tasks_status
                ON agent_subagent_tasks(status,created_at)"""
            )

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _json(record: BaseModel):
        from psycopg.types.json import Jsonb
        return Jsonb(record.model_dump(mode="json"))

    def create_approval(
        self, *, session_id: str, tenant_id: str, user_id: str, role: str,
        call: ToolCall,
    ) -> ToolApprovalRecord:
        record = ToolApprovalRecord(
            approval_id=str(uuid.uuid4()), session_id=session_id,
            tenant_id=tenant_id, user_id=user_id, role=role, call=call,
            status="pending", created_at=_now(),
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_tool_approvals(
                    approval_id,session_id,tenant_id,payload_json,status,created_at
                ) VALUES(%s,%s,%s,%s,%s,%s)""",
                (
                    record.approval_id, session_id, tenant_id,
                    self._json(record), record.status, record.created_at,
                ),
            )
        return record

    def get_approval(
        self, approval_id: str, tenant_id: str
    ) -> ToolApprovalRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM agent_tool_approvals WHERE approval_id=%s AND tenant_id=%s",
                (approval_id, tenant_id),
            ).fetchone()
        return ToolApprovalRecord.model_validate(row["payload_json"]) if row else None

    def list_pending_approvals(
        self, tenant_id: str
    ) -> list[ToolApprovalRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT payload_json FROM agent_tool_approvals
                WHERE tenant_id=%s AND status='pending' ORDER BY created_at""",
                (tenant_id,),
            ).fetchall()
        return [ToolApprovalRecord.model_validate(row["payload_json"]) for row in rows]

    def decide_approval(
        self, *, approval_id: str, tenant_id: str, approved: bool,
        decided_by: str, comment: str,
    ) -> ToolApprovalRecord:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT payload_json,status FROM agent_tool_approvals
                WHERE approval_id=%s AND tenant_id=%s FOR UPDATE""",
                (approval_id, tenant_id),
            ).fetchone()
            if not row:
                raise KeyError("approval not found")
            if row["status"] != "pending":
                raise ValueError("approval already decided")
            record = ToolApprovalRecord.model_validate(row["payload_json"])
            record = record.model_copy(
                update={
                    "status": "approved" if approved else "rejected",
                    "decided_by": decided_by, "comment": comment,
                    "decided_at": _now(),
                }
            )
            connection.execute(
                """UPDATE agent_tool_approvals SET payload_json=%s,status=%s
                WHERE approval_id=%s""",
                (self._json(record), record.status, approval_id),
            )
        return record

    def create_task(self, record: SubagentTaskRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_subagent_tasks(
                    task_id,parent_session_id,child_session_id,tenant_id,
                    payload_json,status,created_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    record.task_id, record.parent_session_id,
                    record.child_session_id, record.tenant_id,
                    self._json(record), record.status, record.created_at,
                ),
            )

    def update_task(self, record: SubagentTaskRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE agent_subagent_tasks SET payload_json=%s,status=%s
                WHERE task_id=%s AND tenant_id=%s""",
                (self._json(record), record.status, record.task_id, record.tenant_id),
            )

    def get_task(
        self, task_id: str, tenant_id: str
    ) -> SubagentTaskRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM agent_subagent_tasks WHERE task_id=%s AND tenant_id=%s",
                (task_id, tenant_id),
            ).fetchone()
        return SubagentTaskRecord.model_validate(row["payload_json"]) if row else None

    def list_tasks(
        self, tenant_id: str, parent_session_id: str | None = None
    ) -> list[SubagentTaskRecord]:
        query = "SELECT payload_json FROM agent_subagent_tasks WHERE tenant_id=%s"
        params: list[Any] = [tenant_id]
        if parent_session_id:
            query += " AND parent_session_id=%s"
            params.append(parent_session_id)
        query += " ORDER BY created_at DESC LIMIT 200"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [SubagentTaskRecord.model_validate(row["payload_json"]) for row in rows]

    def claim_next_task(
        self, *, worker_id: str, lease_seconds: float
    ) -> SubagentTaskRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT task_id, tenant_id, payload_json FROM agent_subagent_tasks
                WHERE status='queued'
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            record = SubagentTaskRecord.model_validate(row["payload_json"])
            claimed = record.model_copy(
                update={
                    "status": "running",
                    "worker_id": worker_id,
                    "lease_expires_at": _lease_expiry(lease_seconds),
                    "attempt": record.attempt + 1,
                    "started_at": record.started_at or _now(),
                    "error": None,
                }
            )
            connection.execute(
                """UPDATE agent_subagent_tasks SET payload_json=%s,status=%s
                WHERE task_id=%s AND status='queued'""",
                (self._json(claimed), claimed.status, claimed.task_id),
            )
            return claimed

    def renew_lease(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: float,
    ) -> SubagentTaskRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT payload_json,status FROM agent_subagent_tasks
                WHERE task_id=%s FOR UPDATE""",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            record = SubagentTaskRecord.model_validate(row["payload_json"])
            if record.worker_id != worker_id:
                return None
            if record.status not in {"running", "cancel_requested"}:
                return record
            updated = record.model_copy(
                update={"lease_expires_at": _lease_expiry(lease_seconds)}
            )
            connection.execute(
                """UPDATE agent_subagent_tasks SET payload_json=%s,status=%s
                WHERE task_id=%s""",
                (self._json(updated), updated.status, task_id),
            )
            return updated

    def requeue_expired_leases(self, *, max_attempts: int) -> int:
        now = datetime.now(timezone.utc)
        changed = 0
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT task_id, payload_json FROM agent_subagent_tasks
                WHERE status IN ('running','cancel_requested')
                FOR UPDATE"""
            ).fetchall()
            for row in rows:
                record = SubagentTaskRecord.model_validate(row["payload_json"])
                expires = _parse_iso(record.lease_expires_at)
                if expires is None or expires > now:
                    continue
                if record.status == "cancel_requested":
                    updated = record.model_copy(
                        update={
                            "status": "cancelled",
                            "worker_id": None,
                            "lease_expires_at": None,
                            "completed_at": _now(),
                            "error": record.error or "cancelled after worker lease expiry",
                        }
                    )
                elif record.attempt >= max_attempts:
                    updated = record.model_copy(
                        update={
                            "status": "failed",
                            "worker_id": None,
                            "lease_expires_at": None,
                            "completed_at": _now(),
                            "error": "subagent lease expired too many times",
                        }
                    )
                else:
                    updated = record.model_copy(
                        update={
                            "status": "queued",
                            "worker_id": None,
                            "lease_expires_at": None,
                            "started_at": None,
                            "error": "requeued after worker lease expiry",
                        }
                    )
                connection.execute(
                    """UPDATE agent_subagent_tasks SET payload_json=%s,status=%s
                    WHERE task_id=%s""",
                    (self._json(updated), updated.status, updated.task_id),
                )
                changed += 1
        return changed


def create_runtime_governance_store(
    settings: Settings,
) -> RuntimeGovernanceStore:
    if settings.session_event_backend == "postgres":
        return PostgresRuntimeGovernanceStore(settings.postgres_dsn)
    return SQLiteRuntimeGovernanceStore(settings.runtime_governance_path)
