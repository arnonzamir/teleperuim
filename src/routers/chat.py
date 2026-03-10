from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from src.auth import verify_api_key
from src.models import ConnectionState, ErrorDetail, ErrorResponse

router = APIRouter(prefix="/api/chat", dependencies=[Depends(verify_api_key)])


def _get_app_state() -> dict[str, Any]:
    from src.main import app_state
    return app_state


@router.get("/threads")
async def list_threads(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    state = _get_app_state()
    backend = state["backend"]

    if backend.state != ConnectionState.CONNECTED:
        return ErrorResponse(
            error=ErrorDetail(
                code="SESSION_EXPIRED",
                message=f"Instance is {backend.state.value}",
            )
        ).model_dump()

    threads = await backend.get_threads(limit=limit)
    return {"threads": [t.model_dump() for t in threads]}


@router.get("/messages/{thread_id}")
async def get_messages(
    thread_id: str,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    state = _get_app_state()
    backend = state["backend"]

    if backend.state != ConnectionState.CONNECTED:
        return ErrorResponse(
            error=ErrorDetail(
                code="SESSION_EXPIRED",
                message=f"Instance is {backend.state.value}",
            )
        ).model_dump()

    messages = await backend.get_messages(thread_id, limit=limit)
    return {
        "thread_id": thread_id,
        "messages": [m.model_dump(by_alias=True) for m in messages],
    }
