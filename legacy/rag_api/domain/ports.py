from typing import Protocol

from rag_api.domain.models import Chunk, SearchHit


class ChunkRepository(Protocol):
    async def add(self, chunks: list[Chunk]) -> None: ...

    async def search(self, query: str, tenant_id: str, limit: int) -> list[SearchHit]: ...


class Generator(Protocol):
    async def generate(self, question: str, context: list[SearchHit]) -> str: ...
