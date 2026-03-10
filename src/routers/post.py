from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile

from src.auth import verify_api_key
from src.models import (
    ConnectionState,
    ErrorDetail,
    ErrorResponse,
    PostResponse,
)

router = APIRouter(prefix="/api/post", dependencies=[Depends(verify_api_key)])


def _get_app_state() -> dict[str, Any]:
    from src.main import app_state
    return app_state


@router.post("/photo", response_model=PostResponse | ErrorResponse)
async def post_photo(
    file: UploadFile = File(...),
    caption: str = Form(""),
) -> PostResponse | ErrorResponse:
    state = _get_app_state()
    backend = state["backend"]

    if backend.state != ConnectionState.CONNECTED:
        return ErrorResponse(
            error=ErrorDetail(
                code="SESSION_EXPIRED",
                message=f"Instance is {backend.state.value}",
            )
        )

    import uuid
    suffix = Path(file.filename).suffix if file.filename else ".jpg"
    media_dir = Path(state["settings"].media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    file_path = media_dir / f"{uuid.uuid4()}{suffix}"
    content = await file.read()
    file_path.write_bytes(content)

    try:
        result = await backend.post_photo(file_path, caption)
        return PostResponse(
            success=True,
            media_id=result["media_id"],
            media_url=result["media_url"],
        )
    except Exception as exc:
        return ErrorResponse(
            error=ErrorDetail(code="INTERNAL_ERROR", message=str(exc))
        )
    finally:
        if file_path.exists():
            file_path.unlink()
