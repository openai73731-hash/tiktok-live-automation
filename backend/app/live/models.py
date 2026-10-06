"""Vendor-independent models."""
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Comment(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)
    username: str = Field(min_length=1)
    user_id: str | None = None
    message_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    text: str = Field(min_length=1)
    # Local receipt time, in UTC; not an inferred TikTok server timestamp.
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    source: Literal["tiktok", "playground"] = "tiktok"


class LiveState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    RETRYING = "reconnecting"  # Compatibility for existing consumers.
    OFFLINE = "offline"
    ENDED = "ended"
    OPERATOR_REQUIRED = "operator_required"
    NEED_OPERATOR = "operator_required"
    STOPPED = "stopped"


class LiveStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    state: LiveState = LiveState.IDLE
    username: str
    mode: Literal["real", "fake", "replay"]
    reason: str | None = None
    terminal_state: LiveState | None = None
    cleanup_state: Literal["not_started", "running", "complete", "failed"] = "not_started"
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
