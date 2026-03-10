from __future__ import annotations

import time

from fastapi import APIRouter

from src.models import HealthResponse

router = APIRouter()

_start_time: float = 0.0


def set_start_time(t: float) -> None:
    global _start_time
    _start_time = t


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        uptime=int(time.time() - _start_time),
    )
