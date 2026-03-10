from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# --- Connection State ---

class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"
    BLOCKED = "BLOCKED"


# --- Account Info ---

class AccountInfo(BaseModel):
    username: str = ""
    user_id: str = ""
    full_name: str = ""


# --- Instance Status ---

class InstanceStatusResponse(BaseModel):
    status: ConnectionState
    backend: str
    account: AccountInfo
    uptime_seconds: int = 0
    messages_sent_today: int = 0
    rate_limit_remaining: int = 0


# --- Messages ---

class SendTextRequest(BaseModel):
    to: str
    text: str


class SendMediaUrlRequest(BaseModel):
    to: str
    type: str = Field(pattern=r"^(image|video)$")
    url: str
    caption: str = ""


class MessageResponse(BaseModel):
    success: bool = True
    message_id: str = ""
    queued: bool = True
    queue_position: int = 0


# --- Threads / Chat ---

class UserInfo(BaseModel):
    user_id: str
    username: str = ""
    full_name: str = ""
    profile_pic_url: str = ""


class LastMessage(BaseModel):
    text: str = ""
    timestamp: str = ""
    from_me: bool = False


class ThreadSummary(BaseModel):
    thread_id: str
    participants: list[UserInfo] = []
    last_message: LastMessage | None = None
    unread_count: int = 0


class MessageItem(BaseModel):
    message_id: str
    thread_id: str
    from_user: UserInfo | None = Field(None, alias="from")
    timestamp: str = ""
    type: str = "text"
    text: str = ""
    media_url: str | None = None

    model_config = {"populate_by_name": True}


# --- Webhook Config ---

class WebhookConfigRequest(BaseModel):
    url: str
    secret: str = ""
    events: list[str] = Field(
        default_factory=lambda: [
            "message.received",
            "message.sent",
            "status.changed",
            "challenge.required",
        ]
    )


class WebhookConfigResponse(BaseModel):
    url: str = ""
    events: list[str] = []
    active: bool = False


# --- Webhook Payloads ---

class WebhookPayload(BaseModel):
    event: str
    instance_id: str
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    data: dict[str, Any] = {}


# --- Challenge ---

class ChallengeRequest(BaseModel):
    code: str


# --- Errors ---

class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorDetail


# --- Generic ---

class SuccessResponse(BaseModel):
    success: bool = True
    status: str | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
    uptime: int = 0


# --- Posts ---

class PostPhotoRequest(BaseModel):
    caption: str = ""

class PostResponse(BaseModel):
    success: bool = True
    media_id: str = ""
    media_url: str = ""
