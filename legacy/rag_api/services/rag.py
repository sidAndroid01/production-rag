import hashlib
from uuid import uuid4

from rag_api.domain.models import Chunk, Citation, IngestResponse, QueryResponse
from rag_api.domain.ports import ChunkRepository, Generator
from rag_api.services.chunking import TextChunker
from rag_api.services.guardrails import InputGuardrail


class RagService:
    def __init__(
        self,
        repository: ChunkRepository,
        generator: Generator,
        chunker: TextChunker,
        guardrail: InputGuardrail,
        min_score: float,
        top_k: int,
        max_query_length: int,
    ) -> None:
        self.repository = repository
        self.generator = generator
        self.chunker = chunker
        self.guardrail = guardrail
        self.min_score = min_score
        self.top_k = top_k
        self.max_query_length = max_query_length

    async def ingest(self, data: bytes, filename: str, tenant_id: str) -> IngestResponse:
        text = data.decode("utf-8", errors="strict")
        text = self.guardrail.sanitize_document(text)
        digest = hashlib.sha256(data).hexdigest()
        document_id = digest[:24]
        pieces = self.chunker.split(text)
        chunks = [
            Chunk(
                text=piece,
                document_id=document_id,
                index=index,
                tenant_id=tenant_id,
                source=filename,
            )
            for index, piece in enumerate(pieces)
        ]
        # newChunks = []
        # for idx, text in enumerate(pieces):
        #     newChunks.append(Chunk(text=text, document_id=document_id, index=idx, tenant_id=tenant_id, source=filename))
        # chunks = [for text in pieces Chunk()]
        await self.repository.add(chunks)
        return IngestResponse(
            document_id=document_id, chunks_created=len(chunks), content_sha256=digest
        )

    async def query(self, question: str, tenant_id: str, request_id: str | None) -> QueryResponse:
        safe_question = self.guardrail.validate_query(question, self.max_query_length)
        hits = await self.repository.search(safe_question, tenant_id, self.top_k)
        grounded_hits = [hit for hit in hits if hit.score >= self.min_score]
        answer = await self.generator.generate(safe_question, grounded_hits)
        citations = [
            Citation(
                document_id=hit.chunk.document_id,
                source=hit.chunk.source,
                chunk_index=hit.chunk.index,
                score=round(hit.score, 4),
                excerpt=hit.chunk.text[:240],
            )
            for hit in grounded_hits
        ]
        return QueryResponse(
            answer=answer,
            citations=citations,
            request_id=request_id or str(uuid4()),
            grounded=bool(grounded_hits),
        )
