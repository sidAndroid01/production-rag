"""Phase 4: PostgreSQL persistence for ingestion chunks.

The adapter uses psycopg 3 when the application runs it. Importing this module
does not require a database, which keeps the schema and contract inspectable on
machines that do not yet have Docker or PostgreSQL installed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from embeddings import DIMENSIONS, MODEL_NAME

# Rows are visible when the document has no ACL or shares a group with the
# caller. Applied in SQL before LIMIT so restricted rows never use top-k slots.
ACL_FILTER = "(cardinality(d.allowed_groups) = 0 OR d.allowed_groups && %s::text[])"


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
        """Apply pending migrations; the DSN must belong to the schema owner."""
        from migrate import apply_migrations

        with self._connect() as connection:
            connection.autocommit = True
            apply_migrations(connection)

    @staticmethod
    def _set_tenant(cursor: Any, tenant_id: str) -> None:
        normalized = tenant_id.strip()
        if not normalized:
            raise ValueError("tenant_id is required")
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", (normalized,))

    def check_connection(self) -> None:
        """Raise if PostgreSQL is unavailable; used by the readiness probe."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

    def persist(
        self,
        result: Any,
        embeddings: Sequence[Sequence[float]],
        model_name: str = MODEL_NAME,
        allowed_groups: Sequence[str] = (),
    ) -> PersistResult:
        """Atomically upsert one ingestion result and its chunks.

        The method expects the ``IngestResult`` shape from ``rag.py``. A content
        hash is unique per tenant, making retries and identical reuploads safe.
        """
        chunks = tuple(result.chunks)
        if not chunks:
            raise ValueError("cannot persist an ingestion result with no chunks")
        if model_name != MODEL_NAME:
            raise ValueError(f"database vectors must use the configured model {MODEL_NAME}")
        if len(embeddings) != len(chunks):
            raise ValueError("every chunk must have exactly one embedding")
        normalized_embeddings: list[str] = []
        for embedding in embeddings:
            vector = [float(value) for value in embedding]
            if len(vector) != DIMENSIONS:
                raise ValueError(f"embeddings must have exactly {DIMENSIONS} dimensions")
            if not all(math.isfinite(value) for value in vector):
                raise ValueError("embeddings must contain only finite values")
            normalized_embeddings.append("[" + ",".join(map(str, vector)) + "]")
        first = chunks[0]
        tenant_id = first.tenant_id
        source = first.source
        with self._connect() as connection, connection.cursor() as cursor:
            self._set_tenant(cursor, tenant_id)
            cursor.execute(
                """
                    INSERT INTO documents
                        (document_id, tenant_id, source, content_sha256, chunk_count,
                         allowed_groups)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, content_sha256) DO UPDATE
                        SET updated_at = now()
                    RETURNING document_id, (xmax = 0) AS inserted
                    """,
                (
                    result.document_id,
                    tenant_id,
                    source,
                    result.content_sha256,
                    len(chunks),
                    list(allowed_groups),
                ),
            )
            document_id, inserted = cursor.fetchone()
            if not inserted:
                return PersistResult(document_id, 0, True)

            cursor.executemany(
                """
                    INSERT INTO chunks
                        (id, tenant_id, document_id, chunk_index, text, embedding,
                         embedding_model, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s)
                    ON CONFLICT (tenant_id, document_id, chunk_index) DO NOTHING
                    """,
                (
                    (
                        chunk.id,
                        chunk.tenant_id,
                        document_id,
                        chunk.index,
                        chunk.text,
                        embedding,
                        model_name,
                        chunk.created_at,
                    )
                    for chunk, embedding in zip(chunks, normalized_embeddings, strict=True)
                ),
            )
            cursor.execute(
                "UPDATE documents SET chunk_count = %s, updated_at = now() "
                "WHERE tenant_id = %s AND document_id = %s",
                (len(chunks), tenant_id, document_id),
            )
        return PersistResult(document_id, len(chunks), False)

    def search_similar(
        self,
        tenant_id: str,
        query_embedding: Sequence[float],
        limit: int = 5,
        model_name: str = MODEL_NAME,
        groups: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Find nearest chunks, filtering tenant, ACL and model before ranking."""
        vector = [float(value) for value in query_embedding]
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if limit <= 0:
            raise ValueError("limit must be positive")
        if len(vector) != DIMENSIONS or not all(math.isfinite(value) for value in vector):
            raise ValueError(f"query embedding must be a finite {DIMENSIONS}-dimension vector")
        vector_literal = "[" + ",".join(map(str, vector)) + "]"
        with self._connect() as connection, connection.cursor() as cursor:
            self._set_tenant(cursor, tenant_id)
            cursor.execute(
                """
                    SELECT c.id, c.tenant_id, c.document_id, c.chunk_index, c.text,
                           c.created_at, d.source, d.allowed_groups,
                           1 - (c.embedding <=> %s::vector) AS score
                    FROM chunks AS c
                    JOIN documents AS d USING (tenant_id, document_id)
                    WHERE c.tenant_id = %s
                      AND c.embedding IS NOT NULL
                      AND c.embedding_model = %s
                      AND """
                + ACL_FILTER
                + """
                    ORDER BY c.embedding <=> %s::vector
                    LIMIT %s
                    """,
                (vector_literal, tenant_id, model_name, list(groups), vector_literal, limit),
            )
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def search_hybrid(
        self,
        tenant_id: str,
        query_text: str,
        query_embedding: Sequence[float],
        limit: int = 5,
        candidate_limit: int = 50,
        model_name: str = MODEL_NAME,
        groups: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Retrieve semantic and keyword candidates, then fuse and rerank them.

        PostgreSQL performs the two first-stage searches. Reciprocal-rank fusion
        makes their scores comparable; a small lexical tie-breaker rewards exact
        query terms during the second-stage rerank. This is deterministic and
        local. A learned cross-encoder can replace this method later without
        changing the API contract.
        """
        vector = [float(value) for value in query_embedding]
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not query_text.strip():
            raise ValueError("query_text is required")
        if limit <= 0 or candidate_limit < limit:
            raise ValueError("candidate_limit must be at least limit")
        if len(vector) != DIMENSIONS or not all(math.isfinite(value) for value in vector):
            raise ValueError(f"query embedding must be a finite {DIMENSIONS}-dimension vector")
        vector_literal = "[" + ",".join(map(str, vector)) + "]"
        with self._connect() as connection, connection.cursor() as cursor:
            self._set_tenant(cursor, tenant_id)
            columns = (
                "c.id, c.tenant_id, c.document_id, c.chunk_index, c.text, "
                "c.created_at, d.source, d.allowed_groups"
            )
            cursor.execute(
                f"""
                SELECT {columns}, 1 - (c.embedding <=> %s::vector) AS semantic_score
                FROM chunks AS c
                JOIN documents AS d USING (tenant_id, document_id)
                WHERE c.tenant_id = %s AND c.embedding IS NOT NULL
                  AND c.embedding_model = %s
                  AND {ACL_FILTER}
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s
                """,
                (
                    vector_literal,
                    tenant_id,
                    model_name,
                    list(groups),
                    vector_literal,
                    candidate_limit,
                ),
            )
            names = [column.name for column in cursor.description]
            semantic_rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
            cursor.execute(
                f"""
                SELECT {columns},
                       ts_rank_cd(c.search_vector, plainto_tsquery('english', %s)) AS keyword_score
                FROM chunks AS c
                JOIN documents AS d USING (tenant_id, document_id)
                WHERE c.tenant_id = %s
                  AND c.search_vector @@ plainto_tsquery('english', %s)
                  AND {ACL_FILTER}
                ORDER BY keyword_score DESC, c.chunk_index
                LIMIT %s
                """,
                (query_text, tenant_id, query_text, list(groups), candidate_limit),
            )
            names = [column.name for column in cursor.description]
            keyword_rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

        # Rank fusion avoids pretending that cosine and ts_rank_cd are on the
        # same scale. The second-stage score also rewards exact query terms.
        candidates: dict[Any, dict[str, Any]] = {}
        for rank, row in enumerate(semantic_rows, start=1):
            candidates[row["id"]] = {
                **row,
                "semantic_score": float(row["semantic_score"] or 0.0),
                "keyword_score": 0.0,
                "semantic_rank": rank,
                "keyword_rank": None,
            }
        for rank, row in enumerate(keyword_rows, start=1):
            candidate = candidates.setdefault(
                row["id"],
                {**row, "semantic_score": 0.0, "semantic_rank": None},
            )
            candidate["keyword_score"] = float(row["keyword_score"] or 0.0)
            candidate["keyword_rank"] = rank

        query_terms = set(re.findall(r"[a-zA-Z0-9]+", query_text.lower()))
        max_keyword_score = max(
            (candidate["keyword_score"] for candidate in candidates.values()), default=0.0
        )
        for candidate in candidates.values():
            chunk_terms = set(re.findall(r"[a-zA-Z0-9]+", candidate["text"].lower()))
            overlap = len(query_terms & chunk_terms) / max(len(query_terms), 1)
            semantic_rank = candidate["semantic_rank"] or candidate_limit + 1
            keyword_rank = candidate["keyword_rank"] or candidate_limit + 1
            keyword_score = (
                candidate["keyword_score"] / max_keyword_score if max_keyword_score else 0.0
            )
            # Scale reciprocal rank into approximately [0, 1] so it can be
            # combined with cosine and normalized full-text scores.
            rank_signal = 61.0 * (0.65 / (60 + semantic_rank) + 0.25 / (60 + keyword_rank))
            candidate["score"] = (
                0.60 * candidate["semantic_score"]
                + 0.25 * keyword_score
                + 0.10 * overlap
                + 0.05 * rank_signal
            )
        return sorted(
            candidates.values(),
            key=lambda row: (-row["score"], row["document_id"], row["chunk_index"]),
        )[:limit]

    def list_chunks(
        self, tenant_id: str, document_id: str | None = None, groups: Sequence[str] = ()
    ) -> list[dict[str, Any]]:
        """Return ordered, tenant-scoped chunks for the query adapter."""
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        where = f"tenant_id = %s AND {ACL_FILTER}"
        parameters: list[Any] = [tenant_id, list(groups)]
        if document_id is not None:
            where += " AND document_id = %s"
            parameters.append(document_id)
        with self._connect() as connection, connection.cursor() as cursor:
            self._set_tenant(cursor, tenant_id)
            cursor.execute(
                "SELECT c.id, tenant_id, document_id, c.chunk_index, c.text, c.created_at, "
                "d.source FROM chunks AS c JOIN documents AS d USING (tenant_id, document_id) "
                f"WHERE {where} "
                "ORDER BY document_id, chunk_index",
                parameters,
            )
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    DOCUMENT_COLUMNS = (
        "document_id, source, content_sha256, status, chunk_count, allowed_groups, "
        "created_at, updated_at"
    )

    def _select_documents(
        self, tenant_id: str, groups: Sequence[str], where: str, parameters: Sequence[Any]
    ) -> list[dict[str, Any]]:
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        with self._connect() as connection, connection.cursor() as cursor:
            self._set_tenant(cursor, tenant_id)
            cursor.execute(
                f"SELECT {self.DOCUMENT_COLUMNS} FROM documents AS d "
                f"WHERE tenant_id = %s AND {ACL_FILTER} {where} "
                "ORDER BY created_at DESC, document_id",
                (tenant_id, list(groups), *parameters),
            )
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def list_documents(self, tenant_id: str, groups: Sequence[str] = ()) -> list[dict[str, Any]]:
        """Return the caller-visible document metadata, newest first."""
        return self._select_documents(tenant_id, groups, "", ())

    def get_document(
        self, tenant_id: str, document_id: str, groups: Sequence[str] = ()
    ) -> dict[str, Any] | None:
        """Return one caller-visible document's metadata, or None."""
        rows = self._select_documents(tenant_id, groups, "AND document_id = %s", (document_id,))
        return rows[0] if rows else None

    def delete_document(self, tenant_id: str, document_id: str, groups: Sequence[str] = ()) -> bool:
        """Delete one caller-visible document; its chunks cascade at the database."""
        if not tenant_id.strip() or not document_id.strip():
            raise ValueError("tenant_id and document_id are required")
        with self._connect() as connection, connection.cursor() as cursor:
            self._set_tenant(cursor, tenant_id)
            cursor.execute(
                "DELETE FROM documents AS d WHERE tenant_id = %s AND document_id = %s "
                f"AND {ACL_FILTER}",
                (tenant_id.strip(), document_id.strip(), list(groups)),
            )
            return bool(cursor.rowcount == 1)


def schema_path() -> Path:
    """Return the directory of ordered SQL migrations that define the schema."""
    return Path(__file__).with_name("migrations")
