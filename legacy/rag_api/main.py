from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from rag_api.adapters.memory import ExtractiveGenerator, InMemoryChunkRepository
from rag_api.api.routes import router
from rag_api.core.config import get_settings
from rag_api.core.logging import configure_logging
from rag_api.services.chunking import TextChunker
from rag_api.services.guardrails import InputGuardrail
from rag_api.services.rag import RagService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.rag_service = RagService(
        repository=InMemoryChunkRepository(),
        generator=ExtractiveGenerator(),
        chunker=TextChunker(),
        guardrail=InputGuardrail(),
        min_score=settings.min_relevance_score,
        top_k=settings.top_k,
        max_query_length=settings.max_query_length,
    )
    yield


app = FastAPI(title="Production RAG API", version="0.1.0", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    structlog.get_logger().exception("unhandled_exception", path=request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def run() -> None:
    uvicorn.run("rag_api.main:app", host="0.0.0.0", port=8000)
