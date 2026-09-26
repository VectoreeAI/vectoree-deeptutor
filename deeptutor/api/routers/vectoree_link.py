"""Vectoree project link. Responses never include the API key or console JWT."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from deeptutor.services.vectoree_link import VectoreeLinkError, get_linker

router = APIRouter()


class VectoreeLinkStart(BaseModel):
    apiUrl: str | None = None
    projectId: str = Field(min_length=1, max_length=2000)


@router.get("/link-status")
async def vectoree_link_status() -> dict[str, object]:
    return get_linker().status()


@router.get("/link")
async def vectoree_link_poll() -> dict[str, object]:
    return get_linker().snapshot()


@router.post("/link/start")
async def vectoree_link_start(body: VectoreeLinkStart) -> dict[str, object]:
    try:
        return get_linker().start(api_url=body.apiUrl, project_id=body.projectId)
    except VectoreeLinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
