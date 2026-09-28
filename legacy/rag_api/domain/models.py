from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field


@dataclass(frozen=True, slots=True)
class Chunk:
    text: str
    document_id: str
    index: int
    tenant_id: str
    source: str
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk: Chunk
    score: float


class QueryRequest(BaseModel):
    question: str = Field(min_length=1)


class Citation(BaseModel):
    document_id: str
    source: str
    chunk_index: int
    score: float
    excerpt: str


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    request_id: str
    grounded: bool


class IngestResponse(BaseModel):
    document_id: str
    chunks_created: int
    content_sha256: str
