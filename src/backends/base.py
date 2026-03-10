from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from src.models import AccountInfo, ConnectionState, MessageItem, ThreadSummary


@runtime_checkable
class InstagramBackend(Protocol):
    state: ConnectionState

    async def login(self, username: str, password: str) -> bool: ...

    async def logout(self) -> bool: ...

    async def send_text(self, user_id: str, text: str) -> str:
        """Returns message_id."""
        ...

    async def send_photo(self, user_id: str, photo_path: Path, caption: str = "") -> str: ...

    async def send_video(self, user_id: str, video_path: Path, caption: str = "") -> str: ...

    async def get_threads(self, limit: int = 20) -> list[ThreadSummary]: ...

    async def get_messages(self, thread_id: str, limit: int = 20) -> list[MessageItem]: ...

    async def get_account_info(self) -> AccountInfo: ...

    async def submit_challenge_code(self, code: str) -> bool: ...

    async def post_photo(self, photo_path: Path, caption: str = "") -> dict[str, str]:
        """Returns dict with media_id and media_url."""
        ...
