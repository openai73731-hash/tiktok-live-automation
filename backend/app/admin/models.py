"""Allowlisted browser data. Never serialize Settings, SDK or provider responses."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ai.models import ReplySuggestion


class UIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnalyzeInput(UIModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    text: str = Field(min_length=1, max_length=10_000)
    allow_paid: bool = False


class CommentView(UIModel):
    id: str
    source: Literal["tiktok", "fake", "replay", "playground"]
    username: str = Field(max_length=200)
    text: str = Field(max_length=10_000)
    timestamp: datetime
    truncated: bool = False


class ReplyRecord(UIModel):
    id: str
    source: Literal["tiktok", "fake", "replay", "playground"]
    received_at: datetime
    duration_ms: int
    original_truncated: bool = False
    suggestion: ReplySuggestion


class LiveView(UIModel):
    enabled: bool
    mode: Literal["real", "fake", "replay"]
    state: str
    terminal_state: str | None
    activity: Literal["active", "inactive", "unknown"]
    queue_size: int
    updated_at: datetime


class AIStatus(UIModel):
    provider: str
    model: str
    state: Literal["available", "unavailable", "not_checked", "checking", "manual_check_required"]
    available: bool | None = None
    model_found: bool | None = None
    last_check: datetime | None = None
    error_category: str | None = None


class Metrics(UIModel):
    processed: int = 0
    ready: int = 0
    skipped: int = 0
    needs_operator: int = 0


class LiveReplyEvent(UIModel):
    record: ReplyRecord
    metrics: Metrics


class PlaygroundResultEvent(UIModel):
    record: ReplyRecord
    processed: int


class AdminStatus(UIModel):
    backend: Literal["running"] = "running"
    started_at: datetime
    uptime_seconds: int
    live: LiveView
    ai: AIStatus
    metrics: Metrics
    playground_processed: int
    reply_mode: Literal["MANUAL"] = "MANUAL"
    automatic_enabled: Literal[False] = False
    delivery: Literal["NOT CONFIGURED"] = "NOT CONFIGURED"


class PublicSettings(UIModel):
    product_name: str
    platform: str
    stream_language: str
    provider: str
    model: str
    operator_language: str
    temperature: float
    max_reply_chars: int
    max_reply_sentences: int
    max_comment_chars: int
    max_model_output_tokens: int
    timeout_seconds: float
    thinking_enabled: bool
    lmstudio_base_url: str | None
    live_enabled: bool
    live_mode: str
