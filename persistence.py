"""Phase 4: PostgreSQL persistence for ingestion chunks.

The adapter uses psycopg 3 when the application runs it. Importing this module
does not require a database, which keeps the schema and contract inspectable on
machines that do not yet have Docker or PostgreSQL installed.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from embeddings import DIMENSIONS, MODEL_NAME

# Rows are visible when the document has no ACL or shares a group with the
# caller. Applied in SQL before LIMIT so restricted rows never use top-k slots.
ACL_FILTER = "(cardinality(d.allowed_groups) = 0 OR d.allowed_groups && %s::text[])"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Where a document comes from: the logical document that versions share.

    ``system`` is the origin ("upload", later "gdrive", "s3", ...); ``id`` is
    that system's stable identifier. ``version`` is the origin's own change
    marker (ETag, revision ID) when it has one.
    """

    system: str = "upload"
    id: str = ""
    version: str | None = None
    modified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PersistResult:
    document_id: str
    chunks_written: int
    already_existed: bool
    version: int = 1
    superseded: tuple[str, ...] = ()


class PostgresPersistence:
    """Transactional document/chunk repository backed by PostgreSQL + pgvector."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        self.dsn = dsn
        self._pool_size = (min_size, max_size)
        self._pool: Any | None = None
        self._pool_lock = threading.Lock()

    def _connect(self) -> Any:
        """Borrow a pooled connection; the block commits on success, else rolls back.

        Tenant context uses ``set_config(..., true)``, which is transaction-local,
        so a returned connection never carries one tenant into the next request.
        """
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    try:
                        from psycopg_pool import ConnectionPool
                    except ImportError as exc:
                        raise RuntimeError("install psycopg[binary] and psycopg-pool") from exc
                    min_size, max_size = self._pool_size
                    self._pool = ConnectionPool(
                        self.dsn, min_size=min_size, max_size=max_size, timeout=10, open=True
                    )
        return self._pool.connection()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def initialize(self) -> None:
        """Apply pending migrations; the DSN must belong to the schema owner."""
        import psycopg

        from migrate import apply_migrations

        with psycopg.connect(self.dsn, autocommit=True) as connection:
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
        allowed_groups: Sequence[str] | None = (),
        *,
        groups: Sequence[str] = (),
        content_type: str = "text/plain",
        source: SourceRef | None = None,
    ) -> PersistResult:
        """Atomically store one ingestion result as the current version of its source.

        ``source`` identifies the logical document (defaulting to the filename
        for plain uploads). Content is unique within a logical document, so
        retries and identical uploads are idempotent. A new upload becomes the
        next version and supersedes the older ones the uploader can see
        (``groups``); re-uploading a superseded version's exact bytes reinstates
        it. ``allowed_groups=None`` inherits the current version's ACL.
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
        source = source or SourceRef(id=first.source)
        with self._connect() as connection, connection.cursor() as cursor:
            self._set_tenant(cursor, tenant_id)
            # Serialize uploads of the same logical document so only one becomes current.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{tenant_id}/{source.system}/{source.id}",),
            )
            if allowed_groups is None:
                cursor.execute(
                    f"""
                        SELECT allowed_groups FROM documents AS d
                        WHERE tenant_id = %s AND source_system = %s AND source_id = %s
                          AND status = 'ready' AND {ACL_FILTER}
                        ORDER BY version DESC LIMIT 1
                        """,
                    (tenant_id, source.system, source.id, list(groups)),
                )
                inherited = cursor.fetchone()
                allowed_groups = list(inherited[0]) if inherited else []
            cursor.execute(
                """
                    INSERT INTO documents
                        (document_id, tenant_id, source, content_sha256, chunk_count,
                         allowed_groups, content_type, source_system, source_id,
                         source_version, source_modified_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, source_system, source_id, content_sha256) DO UPDATE
                        SET updated_at = now(), last_checked_at = now(),
                            source_version = coalesce(
                                EXCLUDED.source_version, documents.source_version
                            )
                    RETURNING document_id, (xmax = 0) AS inserted, status, version
                    """,
                (
                    result.document_id,
                    tenant_id,
                    first.source,
                    result.content_sha256,
                    len(chunks),
                    list(allowed_groups),
                    content_type,
                    source.system,
                    source.id,
                    source.version,
                    source.modified_at,
                ),
            )
            document_id, inserted, status, version = cursor.fetchone()
            if not inserted and status == "ready":
                return PersistResult(document_id, 0, True, version)
            if inserted:
                cursor.executemany(
                    """
                        INSERT INTO chunks
                            (id, tenant_id, document_id, chunk_index, text, page, embedding,
                             embedding_model, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s)
                        ON CONFLICT (tenant_id, document_id, chunk_index) DO NOTHING
                        """,
                    (
                        (
                            chunk.id,
                            chunk.tenant_id,
                            document_id,
                            chunk.index,
                            chunk.text,
                            getattr(chunk, "page", None),
                            embedding,
                            model_name,
                            chunk.created_at,
                        )
                        for chunk, embedding in zip(chunks, normalized_embeddings, strict=True)
                    ),
                )
            identity = (tenant_id, source.system, source.id, document_id, list(groups))
            cursor.execute(
                f"""
                    UPDATE documents AS d SET status = 'superseded', updated_at = now()
                    WHERE tenant_id = %s AND source_system = %s AND source_id = %s
                      AND document_id <> %s AND status = 'ready' AND {ACL_FILTER}
                    RETURNING document_id
                    """,
                identity,
            )
            superseded = tuple(row[0] for row in cursor.fetchall())
            cursor.execute(
                f"""
                    SELECT coalesce(max(version), 0) FROM documents AS d
                    WHERE tenant_id = %s AND source_system = %s AND source_id = %s
                      AND document_id <> %s AND {ACL_FILTER}
                    """,
                identity,
            )
            version = int(cursor.fetchone()[0]) + 1
            # The newest upload's filename becomes the display name of the document.
            cursor.execute(
                "UPDATE documents SET status = 'ready', version = %s, source = %s, "
                "updated_at = now() WHERE tenant_id = %s AND document_id = %s",
                (version, first.source, tenant_id, document_id),
            )
        return PersistResult(
            document_id, len(chunks) if inserted else 0, not inserted, version, superseded
        )

    def current_version(
        self, tenant_id: str, source: SourceRef, groups: Sequence[str] = ()
    ) -> dict[str, Any] | None:
        """The caller-visible current version of a logical document, or None."""
        rows = self._select_documents(
            tenant_id,
            groups,
            "AND source_system = %s AND source_id = %s AND status = 'ready'",
            (source.system, source.id),
        )
        return rows[0] if rows else None

    def mark_checked(self, tenant_id: str, document_id: str) -> None:
        """Record that the source was checked and found unchanged."""
        with self._connect() as connection, connection.cursor() as cursor:
            self._set_tenant(cursor, tenant_id)
            cursor.execute(
                "UPDATE documents SET last_checked_at = now() "
                "WHERE tenant_id = %s AND document_id = %s",
                (tenant_id, document_id),
            )

    def update_document(
        self,
        tenant_id: str,
        document_id: str,
        groups: Sequence[str] = (),
        *,
        allowed_groups: Sequence[str] | None = None,
        display_name: str | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Change the ACL and/or display name of a logical document, all versions.

        ``document_id`` may name any version the caller can see. Returns
        ``(found, row)``: row is None when the new ACL excludes the caller.
        """
        with self._connect() as connection, connection.cursor() as cursor:
            self._set_tenant(cursor, tenant_id)
            cursor.execute(
                f"""
                    SELECT source_system, source_id FROM documents AS d
                    WHERE tenant_id = %s AND document_id = %s AND {ACL_FILTER}
                    """,
                (tenant_id, document_id, list(groups)),
            )
            row = cursor.fetchone()
            if row is None:
                return False, None
            system, source_id = row
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{tenant_id}/{system}/{source_id}",),
            )
            cursor.execute(
                f"""
                    UPDATE documents AS d SET
                        allowed_groups = coalesce(%s::text[], allowed_groups),
                        source = coalesce(%s, source),
                        updated_at = now()
                    WHERE tenant_id = %s AND source_system = %s AND source_id = %s
                      AND {ACL_FILTER}
                    """,
                (
                    list(allowed_groups) if allowed_groups is not None else None,
                    display_name,
                    tenant_id,
                    system,
                    source_id,
                    list(groups),
                ),
            )
        # Read back with the *new* ACL: the caller may have removed their own access.
        return True, self.get_document(tenant_id, document_id, groups)

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
                f"""
                    SELECT c.id, c.tenant_id, c.document_id, c.chunk_index, c.text,
                           c.created_at, c.page, d.source, d.allowed_groups,
                           1 - (c.embedding <=> %s::vector) AS score
                    FROM chunks AS c
                    JOIN documents AS d USING (tenant_id, document_id)
                    WHERE c.tenant_id = %s
                      AND c.embedding IS NOT NULL
                      AND c.embedding_model = %s
                      AND d.status = 'ready'
                      AND {ACL_FILTER}
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
                "c.created_at, c.page, d.source, d.allowed_groups"
            )
            cursor.execute(
                f"""
                SELECT {columns}, 1 - (c.embedding <=> %s::vector) AS semantic_score
                FROM chunks AS c
                JOIN documents AS d USING (tenant_id, document_id)
                WHERE c.tenant_id = %s AND c.embedding IS NOT NULL
                  AND c.embedding_model = %s
                  AND d.status = 'ready'
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
                  AND d.status = 'ready'
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
        "document_id, source, source_system, source_id, version, status, content_type, "
        "content_sha256, chunk_count, allowed_groups, source_version, source_modified_at, "
        "last_checked_at, created_at, updated_at"
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

    def list_documents(
        self, tenant_id: str, groups: Sequence[str] = (), *, include_superseded: bool = False
    ) -> list[dict[str, Any]]:
        """Return the caller-visible document metadata, newest first."""
        where = "" if include_superseded else "AND status = 'ready'"
        return self._select_documents(tenant_id, groups, where, ())

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
