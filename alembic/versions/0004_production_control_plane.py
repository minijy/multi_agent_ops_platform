"""Complete PostgreSQL schema for accounts, RBAC, agents, skills and results."""

from alembic import op

revision = "0004_production_control_plane"
down_revision = "0003_mature_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_runs_status ON ops_runs(status,updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_ops_audit_tenant_created
          ON ops_audit_events(tenant_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_session_events_tenant
          ON agent_session_events(tenant_id,session_id,sequence);
        CREATE INDEX IF NOT EXISTS idx_agent_session_events_user
          ON agent_session_events(tenant_id,user_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_agent_tool_approvals_pending
          ON agent_tool_approvals(tenant_id,status,created_at);
        CREATE INDEX IF NOT EXISTS idx_agent_subagent_tasks_parent
          ON agent_subagent_tasks(tenant_id,parent_session_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_agent_subagent_tasks_status
          ON agent_subagent_tasks(status,created_at);

        CREATE TABLE IF NOT EXISTS ops_accounts(
          tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,display_name TEXT NOT NULL,
          role TEXT NOT NULL,password_hash TEXT NOT NULL,enabled BOOLEAN NOT NULL DEFAULT TRUE,
          must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
          failed_attempts INTEGER NOT NULL DEFAULT 0,locked_until TIMESTAMPTZ,
          last_login_at TIMESTAMPTZ,password_changed_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(tenant_id,user_id));
        CREATE TABLE IF NOT EXISTS ops_account_sessions(
          session_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,expires_at TIMESTAMPTZ NOT NULL,
          revoked_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL,last_used_at TIMESTAMPTZ NOT NULL,
          FOREIGN KEY(tenant_id,user_id) REFERENCES ops_accounts(tenant_id,user_id) ON DELETE CASCADE);
        CREATE INDEX IF NOT EXISTS ix_ops_account_sessions_owner
          ON ops_account_sessions(tenant_id,user_id);

        CREATE TABLE IF NOT EXISTS ops_access_users(
          tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,name TEXT NOT NULL,
          enabled BOOLEAN NOT NULL DEFAULT TRUE,PRIMARY KEY(tenant_id,user_id));
        CREATE TABLE IF NOT EXISTS ops_permission_groups(
          tenant_id TEXT NOT NULL,group_id TEXT NOT NULL,name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',PRIMARY KEY(tenant_id,group_id));
        CREATE TABLE IF NOT EXISTS ops_permission_rules(
          tenant_id TEXT NOT NULL,rule_id TEXT NOT NULL,group_id TEXT,name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',tool_names_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          PRIMARY KEY(tenant_id,rule_id),
          FOREIGN KEY(tenant_id,group_id) REFERENCES ops_permission_groups(tenant_id,group_id)
            ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS ops_user_permission_groups(
          tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,group_id TEXT NOT NULL,
          PRIMARY KEY(tenant_id,user_id,group_id),
          FOREIGN KEY(tenant_id,user_id) REFERENCES ops_access_users(tenant_id,user_id)
            ON DELETE CASCADE,
          FOREIGN KEY(tenant_id,group_id) REFERENCES ops_permission_groups(tenant_id,group_id)
            ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS ops_permission_rule_tools(
          tenant_id TEXT NOT NULL,tool_name TEXT NOT NULL,rule_id TEXT NOT NULL,
          PRIMARY KEY(tenant_id,tool_name),
          FOREIGN KEY(tenant_id,rule_id) REFERENCES ops_permission_rules(tenant_id,rule_id)
            ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS ops_permission_group_tools(
          tenant_id TEXT NOT NULL,group_id TEXT NOT NULL,tool_name TEXT NOT NULL,rule_id TEXT NOT NULL,
          PRIMARY KEY(tenant_id,group_id,tool_name),
          FOREIGN KEY(tenant_id,group_id) REFERENCES ops_permission_groups(tenant_id,group_id)
            ON DELETE CASCADE,
          FOREIGN KEY(tenant_id,rule_id) REFERENCES ops_permission_rules(tenant_id,rule_id)
            ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS ops_group_tool_permissions(
          tenant_id TEXT NOT NULL,group_id TEXT NOT NULL,tool_name TEXT NOT NULL,
          PRIMARY KEY(tenant_id,group_id,tool_name),
          FOREIGN KEY(tenant_id,group_id) REFERENCES ops_permission_groups(tenant_id,group_id)
            ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS ops_group_permission_rules(
          tenant_id TEXT NOT NULL,group_id TEXT NOT NULL,rule_id TEXT NOT NULL,
          PRIMARY KEY(tenant_id,group_id,rule_id),
          FOREIGN KEY(tenant_id,group_id) REFERENCES ops_permission_groups(tenant_id,group_id)
            ON DELETE CASCADE,
          FOREIGN KEY(tenant_id,rule_id) REFERENCES ops_permission_rules(tenant_id,rule_id)
            ON DELETE CASCADE);

        CREATE TABLE IF NOT EXISTS ops_agent_definitions(
          agent_id TEXT PRIMARY KEY,name TEXT NOT NULL,role TEXT NOT NULL,kind TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',enabled BOOLEAN NOT NULL DEFAULT TRUE,
          system_prompt TEXT NOT NULL DEFAULT '',allowed_tools_json TEXT NOT NULL DEFAULT '[]',
          strict_tool_allowlist BOOLEAN NOT NULL DEFAULT FALSE,workflow_id TEXT NOT NULL DEFAULT '',
          builtin BOOLEAN NOT NULL DEFAULT TRUE,integration_json TEXT NOT NULL DEFAULT '{}',
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
        CREATE TABLE IF NOT EXISTS ops_agent_skills(
          name TEXT PRIMARY KEY,description TEXT NOT NULL,content TEXT NOT NULL,
          model_invocable BOOLEAN NOT NULL DEFAULT TRUE,
          user_invocable BOOLEAN NOT NULL DEFAULT TRUE,
          builtin BOOLEAN NOT NULL DEFAULT FALSE,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());

        CREATE TABLE IF NOT EXISTS agent_tool_results(
          result_ref TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,
          session_id TEXT NOT NULL,tool_name TEXT NOT NULL,payload_json JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
        CREATE INDEX IF NOT EXISTS idx_agent_tool_results_session
          ON agent_tool_results(tenant_id,session_id,created_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_tool_results")
    op.execute("DROP TABLE IF EXISTS ops_agent_skills")
    op.execute("DROP TABLE IF EXISTS ops_agent_definitions")
    op.execute("DROP TABLE IF EXISTS ops_group_permission_rules")
    op.execute("DROP TABLE IF EXISTS ops_group_tool_permissions")
    op.execute("DROP TABLE IF EXISTS ops_permission_group_tools")
    op.execute("DROP TABLE IF EXISTS ops_permission_rule_tools")
    op.execute("DROP TABLE IF EXISTS ops_user_permission_groups")
    op.execute("DROP TABLE IF EXISTS ops_permission_rules")
    op.execute("DROP TABLE IF EXISTS ops_permission_groups")
    op.execute("DROP TABLE IF EXISTS ops_access_users")
    op.execute("DROP TABLE IF EXISTS ops_account_sessions")
    op.execute("DROP TABLE IF EXISTS ops_accounts")
