from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.config import BackendType, settings
from src.models import ConnectionState, WebhookPayload

# --- Structured logging setup ---

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, settings.log_level.upper(), logging.INFO)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger("main")

# Global mutable state shared with routers via import
app_state: dict[str, Any] = {}


async def _dispatch_send(action: str, params: dict[str, Any]) -> str:
    """Route queued actions to the correct backend method."""
    backend = app_state["backend"]
    if action == "send_text":
        return await backend.send_text(params["to"], params["text"])
    elif action == "send_image":
        return await backend.send_photo(params["to"], Path(params["path"]), params.get("caption", ""))
    elif action == "send_video":
        return await backend.send_video(params["to"], Path(params["path"]), params.get("caption", ""))
    raise ValueError(f"Unknown queue action: {action}")


async def _on_queue_result(msg_id: str, result_id: str, status: str) -> None:
    """Called when the queue finishes processing a message."""
    emitter = app_state["webhook_emitter"]
    await emitter.emit(
        WebhookPayload(
            event="message.sent",
            instance_id=settings.instance_id,
            data={
                "message_id": result_id,
                "queue_id": msg_id,
                "status": status,
            },
        )
    )


async def _on_incoming_message(payload: WebhookPayload) -> None:
    """Called by the poller when a new incoming message is detected."""
    emitter = app_state["webhook_emitter"]
    await emitter.emit(payload)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    start_time = time.time()

    # Ensure data directories exist
    for d in [settings.sessions_dir, settings.media_dir, settings.webhook_failures_dir]:
        Path(d).mkdir(parents=True, exist_ok=True)

    # Initialize backend
    if settings.instagram_backend == BackendType.OFFICIAL:
        from src.backends.official import OfficialBackend
        backend = OfficialBackend(
            access_token=settings.meta_access_token,
            app_secret=settings.meta_app_secret,
            business_account_id=settings.instagram_business_account_id,
        )
    else:
        from src.backends.unofficial import UnofficialBackend
        backend = UnofficialBackend()

    # Initialize services
    from src.services.queue import MessageQueue
    from src.services.webhook_emitter import WebhookEmitter

    queue = MessageQueue()
    await queue.init_db()

    webhook_emitter = WebhookEmitter()

    # Wire up challenge callback for unofficial backend
    if isinstance(backend, UnofficialBackend):  # type: ignore[possibly-undefined]
        def _challenge_hook(payload: WebhookPayload) -> None:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(webhook_emitter.emit(payload))
                else:
                    loop.run_until_complete(webhook_emitter.emit(payload))
            except Exception:
                pass
        backend.on_challenge = _challenge_hook

    app_state.update({
        "backend": backend,
        "backend_type": settings.instagram_backend.value,
        "queue": queue,
        "webhook_emitter": webhook_emitter,
        "settings": settings,
        "start_time": start_time,
    })

    # Set health check start time
    from src.health import set_start_time
    set_start_time(start_time)

    # Login (unofficial backend only)
    poller = None
    if settings.instagram_backend == BackendType.UNOFFICIAL and settings.instagram_username:
        success = await backend.login(settings.instagram_username, settings.instagram_password)
        if success:
            logger.info("backend_connected", backend="unofficial")
        else:
            logger.warning("backend_login_failed", state=backend.state.value)

        # Start incoming message poller
        from src.services.poller import IncomingPoller
        poller = IncomingPoller(backend=backend, on_message=_on_incoming_message)
        if backend.state == ConnectionState.CONNECTED:
            poller.start()
        app_state["poller"] = poller

    # Replay pending messages and start queue consumer
    await queue.replay_pending()
    queue.start_consumer(send_fn=_dispatch_send, on_result=_on_queue_result)

    logger.info(
        "app_started",
        backend=settings.instagram_backend.value,
        instance_id=settings.instance_id,
    )

    yield

    # Shutdown
    await queue.stop()
    if poller:
        await poller.stop()
    logger.info("app_shutdown")


app = FastAPI(
    title="Instagram Messaging API",
    version="1.0.0",
    lifespan=lifespan,
)

# --- Exception handlers ---

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An internal error occurred",
                "details": {},
            },
        },
    )


# --- Include routers ---

from src.health import router as health_router
from src.routers.chat import router as chat_router
from src.routers.instance import router as instance_router
from src.routers.message import router as message_router
from src.routers.post import router as post_router
from src.routers.webhook import router as webhook_router

app.include_router(health_router)
app.include_router(instance_router)
app.include_router(message_router)
app.include_router(chat_router)
app.include_router(post_router)
app.include_router(webhook_router)
