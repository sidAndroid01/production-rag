"""Phase 2: deterministic query flow over chunks produced by ``rag.py``.

This module deliberately uses an in-memory lexical index.  Embeddings, a
vector database, and a hosted LLM belong to later phases behind replaceable
adapters.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Protocol
from uuid import uuid4


class ChunkLike(Protocol):
    text: str
    document_id: str
    index: int
    tenant_id: str
    source: str


@dataclass(frozen=True, slots=True)
class Citation:
    document_id: str
    source: str
    chunk_index: int
    score: float
    excerpt: str


@dataclass(frozen=True, slots=True)
class QueryResult:
    answer: str
    citations: tuple[Citation, ...]
    request_id: str
    grounded: bool


class UnsafeQueryError(ValueError):
    """Raised when a query violates the input contract."""


TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")
INJECTION_PATTERNS = (
    re.compile(r"ignore (all|any|the) previous instructions", re.I),
    re.compile(r"reveal (the )?(system|developer) prompt", re.I),
    re.compile(r"print.*(api[_ -]?key|secret|password)", re.I),
)


def _terms(text: str) -> Counter[str]:
    return Counter(TOKEN_RE.findall(text.lower()))


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    common = set(left) & set(right)
    numerator = sum(left[word] * right[word] for word in common)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


class RagQueryPipeline:
    """Validate, retrieve, gate, answer, and cite a query."""

    def __init__(
        self,
        chunks: Iterable[ChunkLike],
        *,
        top_k: int = 5,
        min_score: float = 0.10,
        max_query_length: int = 2_000,
    ) -> None:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if not 0.0 <= min_score <= 1.0:
            raise ValueError("min_score must be between 0 and 1")
        if max_query_length <= 0:
            raise ValueError("max_query_length must be positive")
        self.chunks = tuple(chunks)
        self.top_k = top_k
        self.min_score = min_score
        self.max_query_length = max_query_length

    def _validate(self, question: str) -> str:
        safe_question = question.strip()
        if not safe_question or len(safe_question) > self.max_query_length:
            raise UnsafeQueryError("query is empty or exceeds the configured length limit")
        if any(pattern.search(safe_question) for pattern in INJECTION_PATTERNS):
            raise UnsafeQueryError("query matched a prompt-injection policy")
        return safe_question

    def _retrieve(self, question: str, tenant_id: str) -> list[tuple[ChunkLike, float]]:
        query_terms = _terms(question)
        candidates = (
            (chunk, _cosine(query_terms, _terms(chunk.text)))
            for chunk in self.chunks
            if chunk.tenant_id == tenant_id
        )
        return sorted(candidates, key=lambda item: (-item[1], item[0].index))[: self.top_k]

    def query(self, question: str, tenant_id: str, request_id: str | None = None) -> QueryResult:
        """Return a grounded response, or an explicit abstention when evidence is weak."""
        if not tenant_id.strip():
            raise UnsafeQueryError("tenant_id is required")
        safe_question = self._validate(question)
        grounded = [
            (chunk, score)
            for chunk, score in self._retrieve(safe_question, tenant_id)
            if score >= self.min_score
        ]
        if not grounded:
            return QueryResult(
                answer="I do not have enough evidence in the indexed documents to answer that.",
                citations=(),
                request_id=request_id or str(uuid4()),
                grounded=False,
            )

        evidence = " ".join(chunk.text.strip() for chunk, _ in grounded[:3])
        citations = tuple(
            Citation(
                document_id=chunk.document_id,
                source=chunk.source,
                chunk_index=chunk.index,
                score=round(score, 4),
                excerpt=chunk.text[:240],
            )
            for chunk, score in grounded
        )
        return QueryResult(
            answer=f"Based on the indexed sources: {evidence}",
            citations=citations,
            request_id=request_id or str(uuid4()),
            grounded=True,
        )


if __name__ == "__main__":
    from rag import RagIngestionPipeline

    ingested = RagIngestionPipeline().ingest(
        b"The refund window is thirty days. Contact support for an exception.",
        "policy.txt",
        "default",
    )
    response = RagQueryPipeline(ingested.chunks).query("What is the refund window?", "default")
    print({"answer": response.answer, "citations": len(response.citations)})
