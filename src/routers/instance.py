from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends

from src.auth import verify_api_key
from src.models import (
    AccountInfo,
    ChallengeRequest,
    ConnectionState,
    ErrorDetail,
    ErrorResponse,
    InstanceStatusResponse,
    SuccessResponse,
)

router = APIRouter(prefix="/api/instance", dependencies=[Depends(verify_api_key)])


def _get_app_state() -> dict[str, Any]:
    from src.main import app_state
    return app_state


@router.get("/status", response_model=InstanceStatusResponse)
async def instance_status() -> InstanceStatusResponse:
    state = _get_app_state()
    backend = state["backend"]
    queue = state["queue"]

    account = AccountInfo()
    if backend.state == ConnectionState.CONNECTED:
        try:
            account = await backend.get_account_info()
        except Exception:
            pass

    return InstanceStatusResponse(
        status=backend.state,
        backend=state["backend_type"],
        account=account,
        uptime_seconds=int(time.time() - state["start_time"]),
        messages_sent_today=queue.messages_sent_today,
        rate_limit_remaining=queue.rate_limit_remaining,
    )


@router.post("/logout", response_model=SuccessResponse)
async def instance_logout() -> SuccessResponse:
    state = _get_app_state()
    backend = state["backend"]
    poller = state.get("poller")
    if poller:
        await poller.stop()
    await backend.logout()
    return SuccessResponse(success=True)


@router.post("/restart", response_model=SuccessResponse)
async def instance_restart() -> SuccessResponse | ErrorResponse:
    state = _get_app_state()
    backend = state["backend"]
    poller = state.get("poller")
    settings = state["settings"]

    if poller:
        await poller.stop()

    if backend.state == ConnectionState.CONNECTED:
        try:
            await backend.logout()
        except Exception:
            pass

    success = await backend.login(settings.instagram_username, settings.instagram_password)
    if success and poller:
        poller.start()

    return SuccessResponse(success=success, status=backend.state.value)


@router.post("/challenge", response_model=SuccessResponse | ErrorResponse)
async def submit_challenge(body: ChallengeRequest) -> SuccessResponse | ErrorResponse:
    state = _get_app_state()
    backend = state["backend"]

    if backend.state != ConnectionState.CHALLENGE_REQUIRED:
        return ErrorResponse(
            error=ErrorDetail(
                code="INVALID_REQUEST",
                message="No challenge is pending",
            )
        )

    success = await backend.submit_challenge_code(body.code)
    if success:
        poller = state.get("poller")
        if poller:
            poller.start()
        return SuccessResponse(success=True, status=ConnectionState.CONNECTED.value)

    return ErrorResponse(
        error=ErrorDetail(
            code="CHALLENGE_REQUIRED",
            message="Challenge code was not accepted. Try again.",
        )
    )
