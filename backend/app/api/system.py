"""The only unauthenticated route in the system.

/healthz returns nothing sensitive: no versions, no container names,
no queue state, no error texts (SPEC §10).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas.auth import OkResponse

router = APIRouter()


@router.get("/healthz", response_model=OkResponse)
async def healthz() -> OkResponse:
    return OkResponse()
