"""Baseline schema for PostgreSQL control plane, events, governance and metrics."""

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
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
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_runs_tenant_updated
        ON ops_runs(tenant_id, updated_at DESC)
        """
    )
    op.execute(
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
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_action_executions(
            idempotency_key TEXT PRIMARY KEY,
            tool_name TEXT NOT NULL,
            arguments_json JSONB NOT NULL,
            result_json JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
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
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_tool_approvals(
            approval_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            payload_json JSONB NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_subagent_tasks(
            task_id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL,
            child_session_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            payload_json JSONB NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_runtime_metrics(
            metric_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            prompt_tokens BIGINT NOT NULL DEFAULT 0,
            completion_tokens BIGINT NOT NULL DEFAULT 0,
            total_tokens BIGINT NOT NULL DEFAULT 0,
            estimated_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
            latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
            tool_calls INTEGER NOT NULL DEFAULT 0,
            tool_errors INTEGER NOT NULL DEFAULT 0,
            error_code TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_runtime_metrics_tenant
        ON agent_runtime_metrics(tenant_id, created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_runtime_metrics")
