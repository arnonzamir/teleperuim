from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from src.auth import verify_api_key
from src.models import (
    ConnectionState,
    ErrorDetail,
    ErrorResponse,
    MessageResponse,
    SendMediaUrlRequest,
    SendTextRequest,
)

router = APIRouter(prefix="/api/message", dependencies=[Depends(verify_api_key)])


def _get_app_state() -> dict[str, Any]:
    from src.main import app_state
    return app_state


def _check_connected() -> ErrorResponse | None:
    state = _get_app_state()
    backend = state["backend"]
    if backend.state != ConnectionState.CONNECTED:
        return ErrorResponse(
            error=ErrorDetail(
                code="SESSION_EXPIRED",
                message=f"Instance is {backend.state.value}. Cannot send messages.",
            )
        )
    return None


@router.post("/send-text", response_model=MessageResponse | ErrorResponse)
async def send_text(
    body: SendTextRequest,
    sync: bool = Query(False),
) -> MessageResponse | ErrorResponse:
    err = _check_connected()
    if err:
        return err

    state = _get_app_state()
    queue = state["queue"]

    if queue.rate_limit_remaining <= 0:
        return ErrorResponse(
            error=ErrorDetail(
                code="RATE_LIMITED",
                message="Hourly message limit reached.",
                details={"limit": state["settings"].rate_limit_per_hour},
            )
        )

    if sync:
        backend = state["backend"]
        try:
            msg_id = await backend.send_text(body.to, body.text)
            return MessageResponse(success=True, message_id=msg_id, queued=False)
        except Exception as exc:
            return ErrorResponse(
                error=ErrorDetail(code="INTERNAL_ERROR", message=str(exc))
            )

    msg_id, position = await queue.enqueue(
        action="send_text",
        params={"to": body.to, "text": body.text},
    )
    return MessageResponse(
        success=True, message_id=msg_id, queued=True, queue_position=position
    )


@router.post("/send-media", response_model=MessageResponse | ErrorResponse)
async def send_media(
    to: str = Form(...),
    type: str = Form(...),
    file: UploadFile = File(...),
    caption: str = Form(""),
    sync: bool = Query(False),
) -> MessageResponse | ErrorResponse:
    err = _check_connected()
    if err:
        return err

    state = _get_app_state()
    settings = state["settings"]

    # Save uploaded file to media dir
    suffix = Path(file.filename).suffix if file.filename else ".bin"
    media_dir = Path(settings.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)

    import uuid
    file_path = media_dir / f"{uuid.uuid4()}{suffix}"
    content = await file.read()
    file_path.write_bytes(content)

    if sync:
        backend = state["backend"]
        try:
            if type == "image":
                msg_id = await backend.send_photo(to, file_path, caption)
            else:
                msg_id = await backend.send_video(to, file_path, caption)
            return MessageResponse(success=True, message_id=msg_id, queued=False)
        except Exception as exc:
            return ErrorResponse(
                error=ErrorDetail(code="INTERNAL_ERROR", message=str(exc))
            )

    queue = state["queue"]
    msg_id, position = await queue.enqueue(
        action=f"send_{type}",
        params={"to": to, "path": str(file_path), "caption": caption},
    )
    return MessageResponse(
        success=True, message_id=msg_id, queued=True, queue_position=position
    )


@router.post("/send-media-url", response_model=MessageResponse | ErrorResponse)
async def send_media_url(
    body: SendMediaUrlRequest,
    sync: bool = Query(False),
) -> MessageResponse | ErrorResponse:
    err = _check_connected()
    if err:
        return err

    state = _get_app_state()
    settings = state["settings"]

    # Download the URL to a temp file
    media_dir = Path(settings.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(body.url)
            resp.raise_for_status()
    except Exception as exc:
        return ErrorResponse(
            error=ErrorDetail(
                code="INVALID_REQUEST",
                message=f"Failed to download media from URL: {exc}",
            )
        )

    import uuid
    ext = ".jpg" if body.type == "image" else ".mp4"
    file_path = media_dir / f"{uuid.uuid4()}{ext}"
    file_path.write_bytes(resp.content)

    if sync:
        backend = state["backend"]
        try:
            if body.type == "image":
                msg_id = await backend.send_photo(body.to, file_path, body.caption)
            else:
                msg_id = await backend.send_video(body.to, file_path, body.caption)
            return MessageResponse(success=True, message_id=msg_id, queued=False)
        except Exception as exc:
            return ErrorResponse(
                error=ErrorDetail(code="INTERNAL_ERROR", message=str(exc))
            )

    queue = state["queue"]
    msg_id, position = await queue.enqueue(
        action=f"send_{body.type}",
        params={"to": body.to, "path": str(file_path), "caption": body.caption},
    )
    return MessageResponse(
        success=True, message_id=msg_id, queued=True, queue_position=position
    )
