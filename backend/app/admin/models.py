"""Allowlisted browser data. Never serialize Settings, SDK or provider responses."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ai.models import ReplySuggestion
from app.admin.operator_config import OperatorOverlay
from app.delivery.models import SuggestionView
from app.windows.preparation import PreparationStatus


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
    live_session_id: str | None = None
    comment_identity: str | None = None


class ReplyRecord(UIModel):
    id: str
    source: Literal["tiktok", "fake", "replay", "playground"]
    received_at: datetime
    duration_ms: int
    original_truncated: bool = False
    suggestion: ReplySuggestion
    delivery_review: SuggestionView | None = None
    live_session_id: str | None = None
    comment_identity: str | None = None


class LiveView(UIModel):
    enabled: bool
    mode: Literal["real", "fake", "replay"]
    state: str
    terminal_state: str | None
    activity: Literal["active", "inactive", "unknown"]
    queue_size: int
    updated_at: datetime
    username: str = ""
    connection_owned: bool = False
    live_session_id: str | None = None
    last_comment_at: datetime | None = None
    current_comment_count: int = 0
    delivery_ready: Literal[False] = False
    cleanup_state: Literal["not_started", "running", "complete", "failed"] = "not_started"


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
    active_transport: Literal["android", "windows_live_studio"] = "android"
    backend: Literal["running"] = "running"
    started_at: datetime
    uptime_seconds: int
    live: LiveView
    ai: AIStatus
    metrics: Metrics
    playground_processed: int
    reply_mode: Literal["MANUAL", "AUTOMATIC"] = "MANUAL"
    automatic_enabled: bool = False
    delivery: Literal["NOT CONFIGURED"] = "NOT CONFIGURED"
    reply_policy: Literal["focused", "friendly"] = "focused"
    temperature: float = 0
    preparation: PreparationStatus = Field(default_factory=PreparationStatus)


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
    reply_policy: Literal["focused", "friendly"] = "focused"


class OperatorView(UIModel):
    saved_transport: Literal["android", "windows_live_studio"] = "android"
    active_transport: Literal["android", "windows_live_studio"] = "android"
    saved: OperatorOverlay
    active: PublicSettings
    desired: PublicSettings
    username: str
    saved_username: str
    pending: bool
    locked: bool
    lock_reason: str | None = None
    config_error: Literal["invalid_local_config"] | None = None
    saved_config_revision: str
    active_runtime_revision: str
    active_device_serial: str | None = None
    connect_allowed: bool = False
    connect_block_reason: str | None = None


class SaveSettingsInput(UIModel):
    expected_saved_revision: str = Field(pattern=r"^[a-f0-9]{32}$")
    settings: OperatorOverlay


class ApplySettingsInput(UIModel):
    expected_saved_revision: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_active_runtime_revision: str = Field(pattern=r"^[a-f0-9]{32}$")


class ResetSettingsInput(UIModel):
    expected_saved_revision: str = Field(pattern=r"^[a-f0-9]{32}$")


class DisconnectInput(UIModel):
    expected_live_session_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class ConnectInput(UIModel):
    username: str = Field(min_length=1, max_length=25)
    expected_active_runtime_revision: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
