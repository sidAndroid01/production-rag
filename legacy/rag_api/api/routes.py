from typing import Annotated

from fastapi import APIRouter, File, Header, HTTPException, UploadFile, status

from rag_api.api.dependencies import ServiceDep, TenantDep
from rag_api.core.config import get_settings
from rag_api.domain.models import IngestResponse, QueryRequest, QueryResponse
from rag_api.services.guardrails import UnsafeInputError

router = APIRouter()


@router.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", tags=["health"])
async def readiness() -> dict[str, str]:
    return {"status": "ready"}


@router.post("/v1/documents", response_model=IngestResponse, status_code=201, tags=["rag"])
async def ingest_document(
    service: ServiceDep,
    tenant_id: TenantDep,
    file: Annotated[UploadFile, File(description="UTF-8 plain text")],
) -> IngestResponse:
    settings = get_settings()
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File is too large")
    if file.content_type not in {"text/plain", "text/markdown", "application/octet-stream"}:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only text files are supported")
    try:
        return await service.ingest(data, file.filename or "document.txt", tenant_id)
    except UnicodeDecodeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "File must be UTF-8") from exc


@router.post("/v1/query", response_model=QueryResponse, tags=["rag"])
async def query(
    payload: QueryRequest,
    service: ServiceDep,
    tenant_id: TenantDep,
    x_request_id: Annotated[str | None, Header()] = None,
) -> QueryResponse:
    try:
        return await service.query(payload.question, tenant_id, x_request_id)
    except UnsafeInputError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
