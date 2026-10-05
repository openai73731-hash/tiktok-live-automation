"""Vendor-independent models."""
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Comment(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)
    username: str = Field(min_length=1)
    user_id: str | None = None
    text: str = Field(min_length=1)
    # Local receipt time, in UTC; not an inferred TikTok server timestamp.
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    source: Literal["tiktok"] = "tiktok"


class LiveState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RETRYING = "retrying"
    OFFLINE = "offline"
    ENDED = "ended"
    OPERATOR_REQUIRED = "operator_required"
    STOPPED = "stopped"


class LiveStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    state: LiveState = LiveState.IDLE
    username: str
    mode: Literal["real", "fake"]
    reason: str | None = None
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

