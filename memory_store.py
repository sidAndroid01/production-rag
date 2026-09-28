"""In-memory document store with the same contract as PostgresPersistence.

Used when DATABASE_URL is not set: identical uploads are idempotent, re-uploads
of a source become new versions, and group ACLs apply to every read. State is
lost when the process exits.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from permissions import Principal, can_read_groups
from persistence import PersistResult


class InMemoryStore:
    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], dict[str, Any]] = {}
        self._chunks: dict[tuple[str, str], tuple[Any, ...]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _visible(meta: dict[str, Any], groups: Sequence[str]) -> bool:
        allowed = set(meta["allowed_groups"])
        return not allowed or bool(allowed & set(groups))

    def persist(
        self,
        result: Any,
        allowed_groups: Sequence[str] = (),
        *,
        groups: Sequence[str] = (),
        content_type: str = "text/plain",
    ) -> PersistResult:
        chunks = tuple(result.chunks)
        if not chunks:
            raise ValueError("cannot persist an ingestion result with no chunks")
        tenant_id, source = chunks[0].tenant_id, chunks[0].source
        key = (tenant_id, result.document_id)
        now = datetime.now(UTC)
        with self._lock:
            existing = self._documents.get(key)
            if existing is not None and existing["status"] == "ready":
                return PersistResult(result.document_id, 0, True, existing["version"])
            siblings = [
                meta
                for (owner, document_id), meta in self._documents.items()
                if owner == tenant_id
                and meta["source"] == (existing or {"source": source})["source"]
                and document_id != result.document_id
                and self._visible(meta, groups)
            ]
            superseded = []
            for meta in siblings:
                if meta["status"] == "ready":
                    meta.update(status="superseded", updated_at=now)
                    superseded.append(meta["document_id"])
            version = max((meta["version"] for meta in siblings), default=0) + 1
            if existing is None:
                self._documents[key] = {
                    "document_id": result.document_id,
                    "source": source,
                    "version": version,
                    "status": "ready",
                    "content_type": content_type,
                    "content_sha256": result.content_sha256,
                    "chunk_count": len(chunks),
                    "allowed_groups": list(allowed_groups),
                    "created_at": now,
                    "updated_at": now,
                }
                self._chunks[key] = chunks
            else:
                existing.update(status="ready", version=version, updated_at=now)
        return PersistResult(
            result.document_id,
            len(chunks) if existing is None else 0,
            existing is not None,
            version,
            tuple(superseded),
        )

    def list_documents(
        self, tenant_id: str, groups: Sequence[str] = (), *, include_superseded: bool = False
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                dict(meta)
                for (owner, _), meta in self._documents.items()
                if owner == tenant_id
                and self._visible(meta, groups)
                and (include_superseded or meta["status"] == "ready")
            ]
        return sorted(rows, key=lambda row: (row["created_at"], row["document_id"]), reverse=True)

    def get_document(
        self, tenant_id: str, document_id: str, groups: Sequence[str] = ()
    ) -> dict[str, Any] | None:
        with self._lock:
            meta = self._documents.get((tenant_id, document_id))
            return dict(meta) if meta is not None and self._visible(meta, groups) else None

    def delete_document(self, tenant_id: str, document_id: str, groups: Sequence[str] = ()) -> bool:
        key = (tenant_id, document_id)
        with self._lock:
            meta = self._documents.get(key)
            if meta is None or not self._visible(meta, groups):
                return False
            del self._documents[key]
            self._chunks.pop(key, None)
            return True

    def readable_chunks(self, principal: Principal) -> tuple[Any, ...]:
        """Chunks of current document versions the principal may read."""
        with self._lock:
            return tuple(
                chunk
                for key, meta in self._documents.items()
                if key[0] == principal.tenant_id
                and meta["status"] == "ready"
                and can_read_groups(meta["allowed_groups"], principal)
                for chunk in self._chunks[key]
            )
