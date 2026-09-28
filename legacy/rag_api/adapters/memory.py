import math
import re
from collections import Counter

from rag_api.domain.models import Chunk, SearchHit

TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")


def _terms(text: str) -> Counter[str]:
    return Counter(TOKEN_RE.findall(text.lower()))


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    common = set(left) & set(right)
    numerator = sum(left[word] * right[word] for word in common)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


class InMemoryChunkRepository:
    """Deterministic local adapter. Swap for pgvector/Qdrant in production."""

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []

    async def add(self, chunks: list[Chunk]) -> None:
        existing = {chunk.id for chunk in self._chunks}
        self._chunks.extend(chunk for chunk in chunks if chunk.id not in existing)

    async def search(self, query: str, tenant_id: str, limit: int) -> list[SearchHit]:
        query_terms = _terms(query)
        hits = [
            SearchHit(chunk=chunk, score=_cosine(query_terms, _terms(chunk.text)))
            for chunk in self._chunks
            if chunk.tenant_id == tenant_id
        ]
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:limit]


class ExtractiveGenerator:
    """Safe offline generator used until an LLM provider is configured."""

    async def generate(self, question: str, context: list[SearchHit]) -> str:
        del question
        if not context:
            return "I do not have enough evidence in the indexed documents to answer that."
        evidence = " ".join(hit.chunk.text.strip() for hit in context[:3])
        return f"Based on the indexed sources: {evidence}"
