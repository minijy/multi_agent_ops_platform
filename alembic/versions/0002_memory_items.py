"""Add tenant-aware long-term memory storage and optional pgvector index."""

from alembic import op

revision = "0002_memory_items"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_items(
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT,
            agent_id TEXT,
            scope TEXT NOT NULL CHECK(scope IN ('user','tenant','agent','profile')),
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('candidate','active','conflicted','superseded','deleted')),
            importance DOUBLE PRECISION NOT NULL DEFAULT 0.5,
            confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            quality_score DOUBLE PRECISION NOT NULL DEFAULT 0.5,
            source TEXT NOT NULL,
            source_session_id TEXT,
            conflict_group_id TEXT,
            supersedes_id TEXT,
            correction_of TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            expires_at TIMESTAMPTZ,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            embedding_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """CREATE INDEX IF NOT EXISTS idx_memory_items_access
        ON memory_items(tenant_id,status,scope,user_id,agent_id,updated_at DESC)"""
    )
    op.execute(
        """CREATE INDEX IF NOT EXISTS idx_memory_items_key
        ON memory_items(tenant_id,key,status)"""
    )
    op.execute(
        """
        DO $$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS vector;
            ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS embedding vector(384);
            CREATE INDEX IF NOT EXISTS idx_memory_items_embedding
                ON memory_items USING hnsw (embedding vector_cosine_ops);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'pgvector unavailable; embedding_json fallback remains enabled';
        END $$
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS memory_items")
