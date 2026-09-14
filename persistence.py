"""Phase 4: PostgreSQL persistence for ingestion chunks.

The adapter uses psycopg 3 when the application runs it. Importing this module
does not require a database, which keeps the schema and contract inspectable on
machines that do not yet have Docker or PostgreSQL installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_SQL = """
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
    embedding vector,
    embedding_model TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, document_id, chunk_index),
    FOREIGN KEY (tenant_id, document_id)
        REFERENCES documents (tenant_id, document_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS chunks_tenant_document_index
    ON chunks (tenant_id, document_id, chunk_index);
CREATE INDEX IF NOT EXISTS documents_tenant_index ON documents (tenant_id);
"""


@dataclass(frozen=True, slots=True)
class PersistResult:
    document_id: str
    chunks_written: int
    already_existed: bool


class PostgresPersistence:
    """Transactional document/chunk repository backed by PostgreSQL + pgvector."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        self.dsn = dsn

    def _connect(self) -> Any:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("install psycopg[binary] to use PostgreSQL persistence") from exc
        return psycopg.connect(self.dsn)

    def initialize(self) -> None:
        """Create the extension, tables, and indexes in one transaction."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(SCHEMA_SQL)

    def persist(self, result: Any) -> PersistResult:
        """Atomically upsert one ingestion result and its chunks.

        The method expects the ``IngestResult`` shape from ``rag.py``. A content
        hash is unique per tenant, making retries and identical reuploads safe.
        """
        chunks = tuple(result.chunks)
        if not chunks:
            raise ValueError("cannot persist an ingestion result with no chunks")
        first = chunks[0]
        tenant_id = first.tenant_id
        source = first.source
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO documents
                        (document_id, tenant_id, source, content_sha256, chunk_count)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, content_sha256) DO UPDATE
                        SET updated_at = now()
                    RETURNING document_id, (xmax = 0) AS inserted
                    """,
                    (result.document_id, tenant_id, source, result.content_sha256, len(chunks)),
                )
                document_id, inserted = cursor.fetchone()
                if not inserted:
                    return PersistResult(document_id, 0, True)

                cursor.executemany(
                    """
                    INSERT INTO chunks
                        (id, tenant_id, document_id, chunk_index, text, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, document_id, chunk_index) DO NOTHING
                    """,
                    (
                        (
                            chunk.id,
                            chunk.tenant_id,
                            document_id,
                            chunk.index,
                            chunk.text,
                            chunk.created_at,
                        )
                        for chunk in chunks
                    ),
                )
                cursor.execute(
                    "UPDATE documents SET chunk_count = %s, updated_at = now() "
                    "WHERE tenant_id = %s AND document_id = %s",
                    (len(chunks), tenant_id, document_id),
                )
        return PersistResult(document_id, len(chunks), False)

    def list_chunks(self, tenant_id: str, document_id: str | None = None) -> list[dict[str, Any]]:
        """Return ordered, tenant-scoped chunks for the query adapter."""
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        where = "tenant_id = %s"
        parameters: list[Any] = [tenant_id]
        if document_id is not None:
            where += " AND document_id = %s"
            parameters.append(document_id)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, tenant_id, document_id, chunk_index, text, created_at, "
                    f"source FROM chunks JOIN documents USING (tenant_id, document_id) WHERE {where} "
                    "ORDER BY document_id, chunk_index",
                    parameters,
                )
                columns = [column.name for column in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]


def schema_path() -> Path:
    """Return the path used by tooling that wants to inspect the schema."""
    return Path(__file__).with_name("schema.sql")
