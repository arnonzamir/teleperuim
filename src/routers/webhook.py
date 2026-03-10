from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from src.auth import verify_api_key
from src.models import SuccessResponse, WebhookConfigRequest, WebhookConfigResponse

router = APIRouter(prefix="/api/webhook", dependencies=[Depends(verify_api_key)])


def _get_app_state() -> dict[str, Any]:
    from src.main import app_state
    return app_state


@router.put("", response_model=SuccessResponse)
async def set_webhook(body: WebhookConfigRequest) -> SuccessResponse:
    state = _get_app_state()
    emitter = state["webhook_emitter"]
    emitter.set_config(body)
    return SuccessResponse(success=True)


@router.get("", response_model=WebhookConfigResponse)
async def get_webhook() -> WebhookConfigResponse:
    state = _get_app_state()
    emitter = state["webhook_emitter"]
    config = emitter.config
    if not config:
        return WebhookConfigResponse(url="", events=[], active=False)
    return WebhookConfigResponse(
        url=config.url,
        events=config.events,
        active=bool(config.url),
    )


@router.delete("", response_model=SuccessResponse)
async def delete_webhook() -> SuccessResponse:
    state = _get_app_state()
    emitter = state["webhook_emitter"]
    emitter.clear_config()
    return SuccessResponse(success=True)
