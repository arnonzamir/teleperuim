"""Meta Graph API backend for Instagram Messaging.

TODO: Implement when Meta app review is complete. This backend will use the
Instagram Messaging API (Graph API) for Business/Creator accounts with proper
Meta app approval. Key differences from unofficial:
  - Webhook-based incoming messages (no polling needed)
  - 200 msgs/hour rate limit
  - 24-hour messaging window constraint
  - Text, images, and structured message templates only
"""
from __future__ import annotations

from pathlib import Path

from src.models import AccountInfo, ConnectionState, MessageItem, ThreadSummary


class OfficialBackend:
    def __init__(self, access_token: str, app_secret: str, business_account_id: str) -> None:
        self.state = ConnectionState.DISCONNECTED
        self._access_token = access_token
        self._app_secret = app_secret
        self._business_account_id = business_account_id

    async def login(self, username: str, password: str) -> bool:
        # TODO: Validate the long-lived access token against Graph API
        raise NotImplementedError(
            "Official backend not yet implemented. "
            "Requires Meta app review and Graph API integration. "
            "Use instagram_backend=unofficial for now."
        )

    async def logout(self) -> bool:
        # TODO: Invalidate token if needed
        raise NotImplementedError("Official backend: logout not implemented")

    async def send_text(self, user_id: str, text: str) -> str:
        # TODO: POST to /{ig-user-id}/messages with the Messaging API
        # Must respect 24-hour messaging window
        raise NotImplementedError("Official backend: send_text not implemented")

    async def send_photo(self, user_id: str, photo_path: Path, caption: str = "") -> str:
        # TODO: Upload media via Graph API, then send as message attachment
        raise NotImplementedError("Official backend: send_photo not implemented")

    async def send_video(self, user_id: str, video_path: Path, caption: str = "") -> str:
        # TODO: Upload video via Graph API, then send as message attachment
        raise NotImplementedError("Official backend: send_video not implemented")

    async def get_threads(self, limit: int = 20) -> list[ThreadSummary]:
        # TODO: GET /{ig-user-id}/conversations with Messaging API
        raise NotImplementedError("Official backend: get_threads not implemented")

    async def get_messages(self, thread_id: str, limit: int = 20) -> list[MessageItem]:
        # TODO: GET /{conversation-id}/messages
        raise NotImplementedError("Official backend: get_messages not implemented")

    async def get_account_info(self) -> AccountInfo:
        # TODO: GET /me?fields=id,username,name with Graph API
        raise NotImplementedError("Official backend: get_account_info not implemented")

    async def submit_challenge_code(self, code: str) -> bool:
        # No challenge concept in official mode -- auth is via tokens
        raise NotImplementedError(
            "Official backend does not use challenge codes. "
            "Authentication is token-based via Meta app review."
        )
