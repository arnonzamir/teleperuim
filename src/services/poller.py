"""Incoming message poller with adaptive intervals.

Polls direct_threads() for new messages and emits them via webhook.
Interval adapts: 10s (active) -> 30s (quiet 5min) -> 60s (quiet 30min).
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Callable, Coroutine

import structlog

from src.config import settings
from src.models import ConnectionState, WebhookPayload

if TYPE_CHECKING:
    from src.backends.base import InstagramBackend

logger = structlog.get_logger("services.poller")

_ACTIVE_INTERVAL = None  # set from config
_QUIET_INTERVAL = 30
_VERY_QUIET_INTERVAL = 60
_QUIET_THRESHOLD = 300       # 5 minutes
_VERY_QUIET_THRESHOLD = 1800  # 30 minutes
_ERROR_BACKOFFS = [30, 60, 120, 300]


class IncomingPoller:
    def __init__(
        self,
        backend: InstagramBackend,
        on_message: Callable[[WebhookPayload], Coroutine[Any, Any, None]],
    ) -> None:
        self._backend = backend
        self._on_message = on_message
        self._task: asyncio.Task[None] | None = None
        self._high_water_marks: dict[str, str] = {}
        self._last_message_time: float = time.time()
        self._consecutive_errors: int = 0

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("poller_started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("poller_stopped")

    def _current_interval(self) -> float:
        base = settings.poll_interval_seconds
        quiet_for = time.time() - self._last_message_time

        if quiet_for > _VERY_QUIET_THRESHOLD:
            return _VERY_QUIET_INTERVAL
        if quiet_for > _QUIET_THRESHOLD:
            return _QUIET_INTERVAL
        return base

    async def _poll_loop(self) -> None:
        while True:
            if self._backend.state != ConnectionState.CONNECTED:
                await asyncio.sleep(5)
                continue

            try:
                await self._poll_once()
                self._consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._consecutive_errors += 1
                backoff_idx = min(self._consecutive_errors - 1, len(_ERROR_BACKOFFS) - 1)
                backoff = _ERROR_BACKOFFS[backoff_idx]
                logger.exception("poll_error", backoff_seconds=backoff)
                await asyncio.sleep(backoff)
                continue

            interval = self._current_interval()
            await asyncio.sleep(interval)

    async def _poll_once(self) -> None:
        threads = await self._backend.get_threads(limit=20)

        for thread in threads:
            if not thread.last_message:
                continue

            thread_id = thread.thread_id
            hwm = self._high_water_marks.get(thread_id, "")

            # Use timestamp string as high water mark
            msg_ts = thread.last_message.timestamp
            if not msg_ts or msg_ts <= hwm:
                continue

            # Skip messages from ourselves
            if thread.last_message.from_me:
                self._high_water_marks[thread_id] = msg_ts
                continue

            self._high_water_marks[thread_id] = msg_ts
            self._last_message_time = time.time()

            # Build webhook payload
            from_user = thread.participants[0] if thread.participants else None
            payload = WebhookPayload(
                event="message.received",
                instance_id=settings.instance_id,
                data={
                    "message_id": "",
                    "thread_id": thread_id,
                    "from": {
                        "user_id": from_user.user_id if from_user else "",
                        "username": from_user.username if from_user else "",
                        "full_name": from_user.full_name if from_user else "",
                    },
                    "type": "text",
                    "text": thread.last_message.text,
                    "media": None,
                    "reply_to_message_id": None,
                },
            )

            try:
                await self._on_message(payload)
            except Exception:
                logger.exception("poll_webhook_emit_failed", thread_id=thread_id)
