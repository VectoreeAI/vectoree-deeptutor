"""Vectoree project link. Responses never include the API key or console JWT."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from deeptutor.multi_user.context import get_current_user
from deeptutor.services.vectoree_link import VectoreeLinkError, app_auth, get_linker

router = APIRouter()
public_router = APIRouter()

_HTML_HEADERS = {"Cache-Control": "no-store"}
_CALLBACK_OK = (
    "<!doctype html><title>Vectoree</title>"
    '<body style="font-family:sans-serif"><h1>Vectoree connected</h1>'
    "<p>You can close this window and return to DeepTutor.</p></body>"
)
_CALLBACK_BAD = (
    "<!doctype html><title>Vectoree</title>"
    "<p>Vectoree login could not be completed. Return to DeepTutor and try again.</p>"
)


class VectoreeLinkStart(BaseModel):
    apiUrl: str | None = None
    projectId: str = Field(min_length=1, max_length=2000)
    publicOrigin: str | None = None


# Status and poll stay public: once a link completes, auth turns on, and the
# unauthenticated /link page must still see the result to move on to /login.
@public_router.get("/link-status")
async def vectoree_link_status() -> dict[str, object]:
    return get_linker().status()


@public_router.get("/link")
async def vectoree_link_poll() -> dict[str, object]:
    return get_linker().snapshot()


@router.post("/link/start")
async def vectoree_link_start(body: VectoreeLinkStart) -> dict[str, object]:
    # Relinking swaps the sign-in authority for every account.
    if app_auth.resolve_vectoree_app_link() is not None and not get_current_user().is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only an administrator can relink Vectoree.",
        )
    try:
        return get_linker().start(
            api_url=body.apiUrl,
            project_id=body.projectId,
            public_origin=body.publicOrigin,
        )
    except VectoreeLinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@public_router.get("/link/callback")
def vectoree_link_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Host-browser return path. State is checked server-side; no secrets in HTML."""

    callback_state = state if len(request.query_params.getlist("state")) == 1 else None
    callback_code = code if len(request.query_params.getlist("code")) <= 1 else None
    callback_error = (
        error if len(request.query_params.getlist("error")) <= 1 else "Invalid OAuth callback"
    )
    try:
        get_linker().accept_callback(
            code=callback_code,
            state=callback_state,
            error=callback_error,
        )
    except VectoreeLinkError:
        return HTMLResponse(_CALLBACK_BAD, status_code=400, headers=_HTML_HEADERS)
    return HTMLResponse(_CALLBACK_OK, headers=_HTML_HEADERS)
