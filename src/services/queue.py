"""Outgoing message queue with rate limiting and SQLite persistence.

Messages are enqueued via the API and consumed by a background worker that
enforces per-hour rate limits with jitter to avoid pattern detection.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Coroutine

import aiosqlite
import structlog

from src.config import settings

logger = structlog.get_logger("services.queue")

# Minimum gap between sends (seconds)
_BASE_GAP = 3.0
_JITTER_RANGE = 1.0


class MessageQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._db_path = settings.queue_db_path
        self._consumer_task: asyncio.Task[None] | None = None
        self._send_fn: Callable[..., Coroutine[Any, Any, str]] | None = None
        self._on_result: Callable[..., Coroutine[Any, Any, None]] | None = None
        self._messages_sent_hour: list[float] = []
        self._messages_sent_today: int = 0
        self._day_start: float = 0.0
        self._paused_until: float = 0.0

    async def init_db(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pending_messages (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT DEFAULT 'pending'
                )
            """)
            await db.commit()

    async def replay_pending(self) -> int:
        """Re-enqueue messages that were pending before shutdown."""
        count = 0
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT id, payload FROM pending_messages WHERE status = 'pending' ORDER BY created_at"
            ) as cursor:
                async for row in cursor:
                    msg = json.loads(row[1])
                    msg["_db_id"] = row[0]
                    await self._queue.put(msg)
                    count += 1
        if count:
            logger.info("replayed_pending_messages", count=count)
        return count

    async def enqueue(self, action: str, params: dict[str, Any]) -> tuple[str, int]:
        """Add a message to the queue. Returns (message_id, queue_position)."""
        msg_id = str(uuid.uuid4())
        msg = {
            "id": msg_id,
            "action": action,
            "params": params,
            "created_at": time.time(),
        }

        # Persist to SQLite
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO pending_messages (id, payload, created_at) VALUES (?, ?, ?)",
                (msg_id, json.dumps(msg), msg["created_at"]),
            )
            await db.commit()

        msg["_db_id"] = msg_id
        await self._queue.put(msg)
        position = self._queue.qsize()
        logger.info("message_enqueued", message_id=msg_id, position=position, action=action)
        return msg_id, position

    def start_consumer(
        self,
        send_fn: Callable[..., Coroutine[Any, Any, str]],
        on_result: Callable[..., Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self._send_fn = send_fn
        self._on_result = on_result
        self._consumer_task = asyncio.create_task(self._consume_loop())

    async def stop(self) -> None:
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

    def _reset_day_if_needed(self) -> None:
        now = time.time()
        if now - self._day_start > 86400:
            self._messages_sent_today = 0
            self._day_start = now

    def _prune_hourly_window(self) -> None:
        cutoff = time.time() - 3600
        self._messages_sent_hour = [t for t in self._messages_sent_hour if t > cutoff]

    @property
    def messages_sent_today(self) -> int:
        self._reset_day_if_needed()
        return self._messages_sent_today

    @property
    def rate_limit_remaining(self) -> int:
        self._prune_hourly_window()
        return max(0, settings.rate_limit_per_hour - len(self._messages_sent_hour))

    async def _consume_loop(self) -> None:
        logger.info("queue_consumer_started")
        while True:
            msg = await self._queue.get()
            try:
                await self._wait_for_rate_limit()
                await self._process_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("queue_consumer_error", message_id=msg.get("id"))
            finally:
                self._queue.task_done()

    async def _wait_for_rate_limit(self) -> None:
        # Check pause (cooldown from throttle response)
        now = time.time()
        if now < self._paused_until:
            wait = self._paused_until - now
            logger.info("queue_paused_cooldown", wait_seconds=round(wait, 1))
            await asyncio.sleep(wait)

        self._reset_day_if_needed()
        self._prune_hourly_window()

        # Hard hourly ceiling
        while len(self._messages_sent_hour) >= settings.rate_limit_per_hour:
            logger.warning("rate_limit_hourly_reached")
            await asyncio.sleep(10)
            self._prune_hourly_window()

        # Base gap with jitter
        gap = _BASE_GAP + random.uniform(-_JITTER_RANGE, _JITTER_RANGE)
        if self._messages_sent_hour:
            elapsed = time.time() - self._messages_sent_hour[-1]
            if elapsed < gap:
                await asyncio.sleep(gap - elapsed)

    async def _process_message(self, msg: dict[str, Any]) -> None:
        if not self._send_fn:
            logger.error("no_send_fn_configured")
            return

        action = msg.get("action", "")
        params = msg.get("params", {})
        msg_id = msg["id"]

        try:
            result_id = await self._send_fn(action, params)

            # Mark as done in DB
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    "UPDATE pending_messages SET status = 'sent' WHERE id = ?",
                    (msg.get("_db_id", msg_id),),
                )
                await db.commit()

            self._messages_sent_hour.append(time.time())
            self._messages_sent_today += 1

            if self._on_result:
                await self._on_result(msg_id, result_id, "sent")

            logger.info("message_processed", message_id=msg_id, result_id=result_id)

        except Exception as exc:
            error_msg = str(exc).lower()
            if "429" in error_msg or "please wait" in error_msg:
                self._paused_until = time.time() + 300
                logger.warning("rate_limited_by_instagram", pause_seconds=300)
                # Re-enqueue
                await self._queue.put(msg)
            else:
                # Mark as failed
                async with aiosqlite.connect(self._db_path) as db:
                    await db.execute(
                        "UPDATE pending_messages SET status = 'failed' WHERE id = ?",
                        (msg.get("_db_id", msg_id),),
                    )
                    await db.commit()
                if self._on_result:
                    await self._on_result(msg_id, "", "failed")
                logger.error("message_send_failed", message_id=msg_id)

    def pause_sends(self, seconds: int = 300) -> None:
        self._paused_until = time.time() + seconds
