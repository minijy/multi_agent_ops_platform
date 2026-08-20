"""Add governed memory control plane, provenance, retrieval and indexing tables."""

from alembic import op

revision = "0003_mature_memory"
down_revision = "0002_memory_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'default';
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS category TEXT;
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS structured_value JSONB NOT NULL DEFAULT '{}'::jsonb;
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS sensitivity TEXT NOT NULL DEFAULT 'internal';
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS access_count BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS content_hash TEXT;
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS embedding_model TEXT;
        ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS embedding_version TEXT;
        CREATE INDEX IF NOT EXISTS idx_memory_items_subject
          ON memory_items(tenant_id,scope,user_id,agent_id,status,expires_at);
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_user_preferences(
          tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,payload_json JSONB NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,PRIMARY KEY(tenant_id,user_id));
        CREATE TABLE IF NOT EXISTS memory_tenant_policies(
          tenant_id TEXT PRIMARY KEY,payload_json JSONB NOT NULL,updated_at TIMESTAMPTZ NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_events(
          id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,memory_id TEXT,event_type TEXT NOT NULL,
          actor_id TEXT,payload_json JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_memory_events_resource
          ON memory_events(tenant_id,memory_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS memory_sources(
          id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,memory_id TEXT NOT NULL,source_type TEXT NOT NULL,
          source_id TEXT,source_excerpt TEXT,metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_relations(
          id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,source_memory_id TEXT NOT NULL,
          relation_type TEXT NOT NULL,target_type TEXT NOT NULL,target_id TEXT NOT NULL,
          valid_from TIMESTAMPTZ,valid_until TIMESTAMPTZ,confidence DOUBLE PRECISION NOT NULL,
          metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,created_at TIMESTAMPTZ NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_retrieval_logs(
          id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,agent_id TEXT,
          query_hash TEXT NOT NULL,result_ids_json JSONB NOT NULL,score_json JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_memory_retrieval_tenant_time
          ON memory_retrieval_logs(tenant_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS memory_feedback(
          id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,user_id TEXT NOT NULL,memory_id TEXT NOT NULL,
          rating TEXT NOT NULL,comment TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_outbox(
          id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,memory_id TEXT NOT NULL,operation TEXT NOT NULL,
          status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,
          created_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_memory_outbox_claim
          ON memory_outbox(status,updated_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS memory_outbox")
    op.execute("DROP TABLE IF EXISTS memory_feedback")
    op.execute("DROP TABLE IF EXISTS memory_retrieval_logs")
    op.execute("DROP TABLE IF EXISTS memory_relations")
    op.execute("DROP TABLE IF EXISTS memory_sources")
    op.execute("DROP TABLE IF EXISTS memory_events")
    op.execute("DROP TABLE IF EXISTS memory_tenant_policies")
    op.execute("DROP TABLE IF EXISTS memory_user_preferences")
