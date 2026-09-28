from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, Request, status

from rag_api.core.config import Settings, get_settings
from rag_api.services.rag import RagService


def get_rag_service(request: Request) -> RagService:
    return cast(RagService, request.app.state.rag_service)


async def authenticate(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return "default"


ServiceDep = Annotated[RagService, Depends(get_rag_service)]
TenantDep = Annotated[str, Depends(authenticate)]
