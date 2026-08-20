"""Persist connectors, tools and tool bindings as control-plane tables."""

from alembic import op

revision = "0005_connector_tool_entities"
down_revision = "0004_production_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_connections(
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          connector_type TEXT NOT NULL,
          name TEXT NOT NULL,
          enabled BOOLEAN NOT NULL DEFAULT TRUE,
          config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          secret_ref TEXT NOT NULL,
          resource_scopes_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_ops_connections_tenant_type
          ON ops_connections(tenant_id, connector_type, enabled);

        CREATE TABLE IF NOT EXISTS ops_connection_secrets(
          secret_ref TEXT PRIMARY KEY,
          payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS ops_tools(
          tool_name TEXT PRIMARY KEY,
          display_name TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '',
          connector_type TEXT,
          operation TEXT NOT NULL DEFAULT '',
          resource_scope TEXT,
          enabled BOOLEAN NOT NULL DEFAULT TRUE,
          system_prompt TEXT NOT NULL DEFAULT '',
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS ops_tool_bindings(
          tenant_id TEXT NOT NULL,
          tool_name TEXT NOT NULL,
          connection_id TEXT NOT NULL,
          resource_scopes_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY(tenant_id, tool_name)
        );
        CREATE INDEX IF NOT EXISTS idx_ops_tool_bindings_connection
          ON ops_tool_bindings(connection_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ops_tool_bindings")
    op.execute("DROP TABLE IF EXISTS ops_tools")
    op.execute("DROP TABLE IF EXISTS ops_connection_secrets")
    op.execute("DROP TABLE IF EXISTS ops_connections")
