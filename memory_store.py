"""In-memory document store with the same contract as PostgresPersistence.

Used when DATABASE_URL is not set: documents are identified by their source
(system + id), identical uploads are idempotent, new uploads of a source become
new versions, ACLs can be changed later, and group ACLs apply to every read.
State is lost when the process exits.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from permissions import Principal, can_read_groups
from persistence import PersistResult, SourceRef


class InMemoryStore:
    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], dict[str, Any]] = {}
        self._chunks: dict[tuple[str, str], tuple[Any, ...]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _visible(meta: dict[str, Any], groups: Sequence[str]) -> bool:
        allowed = set(meta["allowed_groups"])
        return not allowed or bool(allowed & set(groups))

    def _versions(
        self, tenant_id: str, system: str, source_id: str, groups: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Caller-visible versions of one logical document (caller holds the lock)."""
        return [
            meta
            for (owner, _), meta in self._documents.items()
            if owner == tenant_id
            and meta["source_system"] == system
            and meta["source_id"] == source_id
            and self._visible(meta, groups)
        ]

    def persist(
        self,
        result: Any,
        allowed_groups: Sequence[str] | None = (),
        *,
        groups: Sequence[str] = (),
        content_type: str = "text/plain",
        source: SourceRef | None = None,
    ) -> PersistResult:
        chunks = tuple(result.chunks)
        if not chunks:
            raise ValueError("cannot persist an ingestion result with no chunks")
        tenant_id, filename = chunks[0].tenant_id, chunks[0].source
        source = source or SourceRef(id=filename)
        key = (tenant_id, result.document_id)
        now = datetime.now(UTC)
        with self._lock:
            versions = self._versions(tenant_id, source.system, source.id, groups)
            current = [meta for meta in versions if meta["status"] == "ready"]
            if allowed_groups is None:
                allowed_groups = current[0]["allowed_groups"] if current else []
            existing = self._documents.get(key)
            if existing is not None and existing["status"] == "ready":
                existing.update(last_checked_at=now)
                return PersistResult(result.document_id, 0, True, existing["version"])
            siblings = [meta for meta in versions if meta["document_id"] != result.document_id]
            superseded = []
            for meta in siblings:
                if meta["status"] == "ready":
                    meta.update(status="superseded", updated_at=now)
                    superseded.append(meta["document_id"])
            version = max((meta["version"] for meta in siblings), default=0) + 1
            if existing is None:
                self._documents[key] = {
                    "document_id": result.document_id,
                    "source": filename,
                    "source_system": source.system,
                    "source_id": source.id,
                    "version": version,
                    "status": "ready",
                    "content_type": content_type,
                    "content_sha256": result.content_sha256,
                    "chunk_count": len(chunks),
                    "allowed_groups": list(allowed_groups),
                    "source_version": source.version,
                    "source_modified_at": source.modified_at,
                    "last_checked_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
                self._chunks[key] = chunks
            else:
                existing.update(
                    status="ready",
                    version=version,
                    source=filename,
                    updated_at=now,
                    last_checked_at=now,
                )
        return PersistResult(
            result.document_id,
            len(chunks) if existing is None else 0,
            existing is not None,
            version,
            tuple(superseded),
        )

    def current_version(
        self, tenant_id: str, source: SourceRef, groups: Sequence[str] = ()
    ) -> dict[str, Any] | None:
        with self._lock:
            for meta in self._versions(tenant_id, source.system, source.id, groups):
                if meta["status"] == "ready":
                    return dict(meta)
        return None

    def mark_checked(self, tenant_id: str, document_id: str) -> None:
        with self._lock:
            meta = self._documents.get((tenant_id, document_id))
            if meta is not None:
                meta["last_checked_at"] = datetime.now(UTC)

    def update_document(
        self,
        tenant_id: str,
        document_id: str,
        groups: Sequence[str] = (),
        *,
        allowed_groups: Sequence[str] | None = None,
        display_name: str | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        now = datetime.now(UTC)
        with self._lock:
            meta = self._documents.get((tenant_id, document_id))
            if meta is None or not self._visible(meta, groups):
                return False, None
            for version in self._versions(
                tenant_id, meta["source_system"], meta["source_id"], groups
            ):
                if allowed_groups is not None:
                    version["allowed_groups"] = list(allowed_groups)
                if display_name is not None:
                    version["source"] = display_name
                version["updated_at"] = now
            # The caller may have removed their own access with the new ACL.
            return True, dict(meta) if self._visible(meta, groups) else None

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
        """Chunks of current document versions the principal may read.

        Chunk ``source`` is replaced by the document's current display name, so
        a rename is reflected in citations immediately.
        """
        with self._lock:
            return tuple(
                _renamed(chunk, meta["source"])
                for key, meta in self._documents.items()
                if key[0] == principal.tenant_id
                and meta["status"] == "ready"
                and can_read_groups(meta["allowed_groups"], principal)
                for chunk in self._chunks[key]
            )


def _renamed(chunk: Any, source: str) -> Any:
    return chunk if chunk.source == source else replace(chunk, source=source)
