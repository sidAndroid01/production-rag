"""Phase 1: deterministic document-ingestion flow for the production RAG project.

The module intentionally stops after creating normalized, overlapping chunks.  It
does not call an embedding model, vector database, LLM, or web service yet.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class Chunk:
    """A chunk ready for a later embedding and persistence phase."""

    text: str
    document_id: str
    index: int
    tenant_id: str
    source: str
    id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IngestResult:
    document_id: str
    content_sha256: str
    chunks: tuple[Chunk, ...]


class RagIngestionPipeline:
    """Validate, normalize, identify, and chunk one UTF-8 document."""

    def __init__(self, chunk_size: int = 900, overlap: int = 120) -> None:
        if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
            raise ValueError("chunk_size must be positive and overlap must be smaller")
        self.chunk_size = chunk_size
        self.overlap = overlap

    @staticmethod
    def _sanitize_document(text: str) -> str:
        # Documents are untrusted data. Keep the words but remove role labels that
        # could be mistaken for instructions by a later generation prompt.
        return re.sub(
            r"(?im)^\s*(system|assistant|developer)\s*:\s*",
            "[untrusted-document-label]: ",
            text,
        )

    @staticmethod
    def _normalize_identifier(value: str, field: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field} is required")
        if any(ord(character) < 32 for character in normalized):
            raise ValueError(f"{field} contains control characters")
        return normalized

    def _split(self, text: str) -> list[str]:
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            return []

        chunks: list[str] = []
        start = 0
        while start < len(normalized):
            end = min(start + self.chunk_size, len(normalized))
            if end < len(normalized):
                boundary = normalized.rfind(" ", start, end)
                end = boundary if boundary > start else end
            chunks.append(normalized[start:end])
            if end == len(normalized):
                break
            # Always make progress when a single token is longer than the target.
            next_start = end - self.overlap
            start = max(next_start, start + 1)
        return chunks

    def ingest(self, data: bytes, filename: str, tenant_id: str) -> IngestResult:
        """Return content-addressed chunks without performing downstream RAG work."""
        filename = self._normalize_identifier(filename, "filename")
        tenant_id = self._normalize_identifier(tenant_id, "tenant_id")

        text = data.decode("utf-8", errors="strict")
        safe_text = self._sanitize_document(text)
        digest = hashlib.sha256(data).hexdigest()
        document_id = digest[:24]
        created_at = datetime.now(UTC)
        chunks = tuple(
            Chunk(
                text=piece,
                document_id=document_id,
                index=index,
                tenant_id=tenant_id,
                source=filename,
                id=str(uuid4()),
                created_at=created_at,
            )
            for index, piece in enumerate(self._split(safe_text))
        )
        return IngestResult(document_id, digest, chunks)


if __name__ == "__main__":
    sample = b"RAG ingestion starts with trusted decoding and deterministic chunking."
    result = RagIngestionPipeline().ingest(sample, "sample.txt", "default")
    print({"document_id": result.document_id, "chunks_created": len(result.chunks)})
