from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import structlog
from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
)

from src.config import settings
from src.models import (
    AccountInfo,
    ConnectionState,
    LastMessage,
    MessageItem,
    ThreadSummary,
    UserInfo,
    WebhookPayload,
)

logger = structlog.get_logger("backend.unofficial")


class UnofficialBackend:
    def __init__(self) -> None:
        self.state = ConnectionState.DISCONNECTED
        self._cl: Client | None = None
        self._account_info = AccountInfo()
        self._session_path = Path(settings.sessions_dir) / f"{settings.instance_id}.json"
        self._session_dump_task: asyncio.Task[None] | None = None
        # Callback set by the app to emit webhook events for challenges
        self.on_challenge: Any = None

    def _create_client(self) -> Client:
        cl = Client()

        if settings.proxy_url:
            cl.set_proxy(settings.proxy_url)

        # Security: force SSL verification on the underlying requests session.
        # instagrapi disables this by default; we override it here per audit.
        if hasattr(cl, "private") and hasattr(cl.private, "verify"):
            cl.private.verify = True
        if hasattr(cl, "public") and hasattr(cl.public, "verify"):
            cl.public.verify = True

        cl.challenge_code_handler = self._challenge_handler
        return cl

    def _challenge_handler(self, username: str, choice: Any) -> str:
        """Custom challenge handler that emits a webhook instead of printing to stdout."""
        self.state = ConnectionState.CHALLENGE_REQUIRED
        logger.warning("challenge_required", username=username, choice_type=str(choice))

        if self.on_challenge:
            try:
                self.on_challenge(
                    WebhookPayload(
                        event="challenge.required",
                        instance_id=settings.instance_id,
                        data={
                            "type": str(choice),
                            "contact_point": username,
                            "instructions": "Submit the verification code via POST /api/instance/challenge",
                        },
                    )
                )
            except Exception:
                logger.exception("challenge_webhook_failed")

        # Return empty -- caller must use submit_challenge_code endpoint
        return ""

    def _force_ssl_verify(self) -> None:
        """Ensure SSL verification stays enabled after login (sessions can reset it)."""
        if self._cl is None:
            return
        for session_attr in ("private", "public"):
            session = getattr(self._cl, session_attr, None)
            if session and hasattr(session, "verify"):
                session.verify = True

    async def login(self, username: str, password: str) -> bool:
        self.state = ConnectionState.CONNECTING
        logger.info("login_started", username=username)

        loop = asyncio.get_event_loop()
        try:
            cl = self._create_client()

            # Try to load existing session
            if self._session_path.exists():
                logger.info("loading_session", path=str(self._session_path))
                await loop.run_in_executor(
                    None, cl.load_settings, self._session_path
                )
                self._force_ssl_on(cl)
                try:
                    await loop.run_in_executor(None, cl.login, username, password)
                    self._force_ssl_on(cl)
                except LoginRequired:
                    logger.warning("session_invalid_relogin")
                    cl = self._create_client()
                    await loop.run_in_executor(None, cl.login, username, password)
                    self._force_ssl_on(cl)
            else:
                await loop.run_in_executor(None, cl.login, username, password)
                self._force_ssl_on(cl)

            self._cl = cl
            self._force_ssl_verify()

            # Dump session immediately on success
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            await loop.run_in_executor(None, cl.dump_settings, self._session_path)

            # Fetch account info
            try:
                user = await loop.run_in_executor(None, cl.account_info)
                self._account_info = AccountInfo(
                    username=user.username or "",
                    user_id=str(user.pk),
                    full_name=user.full_name or "",
                )
            except Exception:
                logger.warning("account_info_fetch_failed")

            self.state = ConnectionState.CONNECTED
            logger.info("login_success", username=username)

            self._start_session_dump_loop()
            return True

        except ChallengeRequired:
            self.state = ConnectionState.CHALLENGE_REQUIRED
            logger.warning("challenge_required_on_login", username=username)
            return False
        except PleaseWaitFewMinutes:
            self.state = ConnectionState.BLOCKED
            logger.error("login_rate_limited", username=username)
            return False
        except Exception:
            self.state = ConnectionState.DISCONNECTED
            logger.exception("login_failed")
            return False

    def _force_ssl_on(self, cl: Client) -> None:
        for attr in ("private", "public"):
            session = getattr(cl, attr, None)
            if session and hasattr(session, "verify"):
                session.verify = True

    def _start_session_dump_loop(self) -> None:
        if self._session_dump_task and not self._session_dump_task.done():
            return
        self._session_dump_task = asyncio.create_task(self._session_dump_loop())

    async def _session_dump_loop(self) -> None:
        """Dump session to disk every 30 minutes to capture cookie refreshes."""
        while self.state == ConnectionState.CONNECTED and self._cl:
            await asyncio.sleep(1800)
            if self._cl and self.state == ConnectionState.CONNECTED:
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None, self._cl.dump_settings, self._session_path
                    )
                    logger.debug("session_dumped")
                except Exception:
                    logger.exception("session_dump_failed")

    async def logout(self) -> bool:
        if self._session_dump_task:
            self._session_dump_task.cancel()
        if self._cl:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._cl.logout)
            except Exception:
                logger.warning("logout_api_call_failed")
            self._cl = None
        if self._session_path.exists():
            self._session_path.unlink()
        self.state = ConnectionState.DISCONNECTED
        self._account_info = AccountInfo()
        logger.info("logged_out")
        return True

    def _require_client(self) -> Client:
        if self._cl is None or self.state != ConnectionState.CONNECTED:
            raise RuntimeError("Not connected")
        return self._cl

    async def send_text(self, user_id: str, text: str) -> str:
        cl = self._require_client()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, cl.direct_send, text, [int(user_id)]
        )
        msg_id = str(getattr(result, "id", "")) if result else ""
        logger.info(
            "message_sent",
            to=user_id,
            message_id=msg_id,
            content_hint=f"[REDACTED len={len(text)}]",
        )
        return msg_id

    async def send_photo(self, user_id: str, photo_path: Path, caption: str = "") -> str:
        cl = self._require_client()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, cl.direct_send_photo, str(photo_path), [int(user_id)]
        )
        msg_id = str(getattr(result, "id", "")) if result else ""
        logger.info("photo_sent", to=user_id, message_id=msg_id)
        return msg_id

    async def send_video(self, user_id: str, video_path: Path, caption: str = "") -> str:
        cl = self._require_client()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, cl.direct_send_video, str(video_path), [int(user_id)]
        )
        msg_id = str(getattr(result, "id", "")) if result else ""
        logger.info("video_sent", to=user_id, message_id=msg_id)
        return msg_id

    async def get_threads(self, limit: int = 20) -> list[ThreadSummary]:
        cl = self._require_client()
        loop = asyncio.get_event_loop()
        raw_threads = await loop.run_in_executor(
            None, cl.direct_threads, limit
        )
        threads: list[ThreadSummary] = []
        for t in raw_threads:
            participants = [
                UserInfo(
                    user_id=str(u.pk),
                    username=u.username or "",
                    full_name=u.full_name or "",
                )
                for u in (t.users or [])
            ]
            last_msg = None
            if t.messages:
                m = t.messages[0]
                last_msg = LastMessage(
                    text=m.text or "",
                    timestamp=m.timestamp.isoformat() if m.timestamp else "",
                    from_me=str(m.user_id) == str(cl.user_id),
                )
            threads.append(
                ThreadSummary(
                    thread_id=str(t.id),
                    participants=participants,
                    last_message=last_msg,
                    unread_count=0,
                )
            )
        return threads

    async def get_messages(self, thread_id: str, limit: int = 20) -> list[MessageItem]:
        cl = self._require_client()
        loop = asyncio.get_event_loop()
        raw_messages = await loop.run_in_executor(
            None, cl.direct_messages, int(thread_id), limit
        )
        items: list[MessageItem] = []
        for m in raw_messages:
            msg_type = "text"
            media_url = None
            if hasattr(m, "media") and m.media:
                msg_type = "media"
                media_url = getattr(m.media, "thumbnail_url", None) or ""
            items.append(
                MessageItem(
                    message_id=str(m.id),
                    thread_id=thread_id,
                    **{
                        "from": UserInfo(
                            user_id=str(m.user_id),
                        )
                    },
                    timestamp=m.timestamp.isoformat() if m.timestamp else "",
                    type=msg_type,
                    text=m.text or "",
                    media_url=media_url,
                )
            )
        return items

    async def get_account_info(self) -> AccountInfo:
        return self._account_info

    async def post_photo(self, photo_path: Path, caption: str = "") -> dict[str, str]:
        cl = self._require_client()
        loop = asyncio.get_event_loop()
        media = await loop.run_in_executor(
            None, cl.photo_upload, str(photo_path), caption
        )
        media_id = str(getattr(media, "pk", ""))
        media_url = ""
        if hasattr(media, "thumbnail_url") and media.thumbnail_url:
            media_url = str(media.thumbnail_url)
        elif hasattr(media, "code") and media.code:
            media_url = f"https://www.instagram.com/p/{media.code}/"
        logger.info("photo_posted", media_id=media_id)
        return {"media_id": media_id, "media_url": media_url}

    async def submit_challenge_code(self, code: str) -> bool:
        if self._cl is None:
            raise RuntimeError("No client available for challenge submission")
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, self._cl.challenge_resolve, code
            )
            if result:
                self._force_ssl_verify()
                self.state = ConnectionState.CONNECTED
                logger.info("challenge_resolved")
                return True
        except Exception:
            logger.exception("challenge_resolve_failed")
        return False
