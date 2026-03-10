"""Webhook delivery with HMAC-SHA256 signing and retry with exponential backoff.

Failed deliveries are stored to disk for manual replay.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx
import structlog

from src.config import settings
from src.models import WebhookConfigRequest, WebhookPayload

logger = structlog.get_logger("services.webhook")

_RETRY_DELAYS = [2, 8, 32]


class WebhookEmitter:
    def __init__(self) -> None:
        self._config: WebhookConfigRequest | None = None
        self._failures_dir = Path(settings.webhook_failures_dir)
        self._failures_dir.mkdir(parents=True, exist_ok=True)

        # Load default config from env
        if settings.webhook_url:
            self._config = WebhookConfigRequest(
                url=settings.webhook_url,
                secret=settings.webhook_secret,
            )

    @property
    def config(self) -> WebhookConfigRequest | None:
        return self._config

    def set_config(self, config: WebhookConfigRequest) -> None:
        self._config = config
        logger.info("webhook_configured", url=config.url, events=config.events)

    def clear_config(self) -> None:
        self._config = None
        logger.info("webhook_cleared")

    def _sign_payload(self, body: bytes, secret: str) -> str:
        return "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()

    async def emit(self, payload: WebhookPayload) -> bool:
        if not self._config or not self._config.url:
            return False

        # Check if event type is subscribed
        if payload.event not in self._config.events:
            return False

        body = payload.model_dump_json().encode()
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Event-Type": payload.event,
            "X-Instance-Id": payload.instance_id,
        }

        if self._config.secret:
            headers["X-Webhook-Signature"] = self._sign_payload(body, self._config.secret)

        for attempt, delay in enumerate(_RETRY_DELAYS):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        self._config.url,
                        content=body,
                        headers=headers,
                    )
                if resp.status_code < 400:
                    logger.info(
                        "webhook_delivered",
                        event=payload.event,
                        status=resp.status_code,
                        attempt=attempt + 1,
                    )
                    return True
                logger.warning(
                    "webhook_delivery_failed",
                    event=payload.event,
                    status=resp.status_code,
                    attempt=attempt + 1,
                )
            except Exception:
                logger.warning(
                    "webhook_delivery_error",
                    event=payload.event,
                    attempt=attempt + 1,
                )

            if attempt < len(_RETRY_DELAYS) - 1:
                import asyncio
                await asyncio.sleep(delay)

        # All retries exhausted -- store for manual replay
        self._store_failure(payload)
        return False

    def _store_failure(self, payload: WebhookPayload) -> None:
        filename = f"{int(time.time())}_{payload.event}.json"
        path = self._failures_dir / filename
        try:
            path.write_text(payload.model_dump_json(indent=2))
            logger.error("webhook_delivery_exhausted", event=payload.event, stored=str(path))
        except Exception:
            logger.exception("webhook_failure_store_error")
