-- Phase 4 schema for PostgreSQL + pgvector.
-- The canonical copy is also available as persistence.SCHEMA_SQL.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    source TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_id),
    UNIQUE (tenant_id, content_sha256)
);

CREATE TABLE IF NOT EXISTS chunks (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    text TEXT NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding vector(384),
    embedding_model TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, document_id, chunk_index),
    FOREIGN KEY (tenant_id, document_id)
        REFERENCES documents (tenant_id, document_id)
        ON DELETE CASCADE
);

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', text)) STORED;

-- Upgrade databases created by earlier phases, where embedding was untyped.
ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(384)
    USING embedding::vector(384);

CREATE INDEX IF NOT EXISTS chunks_tenant_document_index
    ON chunks (tenant_id, document_id, chunk_index);
CREATE INDEX IF NOT EXISTS documents_tenant_index ON documents (tenant_id);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;
CREATE INDEX IF NOT EXISTS chunks_search_vector_gin
    ON chunks USING gin (search_vector);
