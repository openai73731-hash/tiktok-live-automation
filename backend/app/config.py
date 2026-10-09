"""Environment configuration. Load .env from the selected working directory."""
import re
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ReplayScenarioName = Literal[
    "html_room_success", "html_fail_api_success", "html_challenge", "api_missing_room",
    "live_with_comments", "live_end", "temporary_disconnect", "rate_limit", "security_event",
    "unknown_events_then_comments",
]


class WindowsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    executable_path: Path | None = None
    process_id: int | None = Field(default=None, gt=0, le=2**31 - 1)
    broadcaster_user_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    manual_fill_enabled: bool = True
    acceptance_one_shot: bool = False
    arm_ttl_seconds: float = Field(default=600, ge=1, le=1200, allow_inf_nan=False)
    focus_watch_seconds: float = Field(default=15, ge=1, le=30, allow_inf_nan=False)
    verification_seconds: float = Field(default=10, ge=0.1, le=15, allow_inf_nan=False)
    cooldown_seconds: float = Field(default=3, ge=2, le=60, allow_inf_nan=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8-sig",
        extra="ignore", validate_default=True, env_nested_delimiter="__",
    )

    live_mode: Literal["real", "fake", "replay"] = "fake"
    replay_scenario: ReplayScenarioName | None = None
    tiktok_username: str = ""
    diagnostic: bool = False
    # Web UI is observation/Playground only unless LIVE is explicitly enabled.
    admin_live_enabled: bool = False
    # Trusted startup only. Neither flag/path can be supplied through Admin UI.
    enable_physical_tiktok_send: bool = False
    enable_physical_windows_send: bool = False
    windows: WindowsSettings = Field(default_factory=WindowsSettings)
    # Startup-only platform selection; Android remains the compatibility default.
    tiktok_transport: Literal["android", "windows_live_studio"] = "android"
    instance_id: str = Field(default="default", pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
    account_label: str = Field(default="", max_length=80, pattern=r"^[^\x00-\x1f\x7f]*$")
    state_dir: Path = Path("data")
    adb_executable: Path | None = None
    manual_delivery_min_interval_seconds: float = Field(default=3, ge=2, le=60, allow_inf_nan=False)
    openai_api_key: SecretStr = Field(default=SecretStr(""), repr=False, exclude=True)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: Path = Path("logs")
    log_max_bytes: int = Field(default=5_242_880, ge=1024)
    log_backup_count: int = Field(default=3, ge=1, le=100)
    queue_size: int = Field(default=1000, ge=1, le=100_000)
    reconnect_initial_seconds: float = Field(default=2, gt=0, allow_inf_nan=False)
    reconnect_max_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    reconnect_max_attempts: int = Field(default=5, ge=1, le=100)
    connect_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    shutdown_timeout_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)
    fake_interval_seconds: float = Field(default=1, ge=0, allow_inf_nan=False)
    fake_repeat: bool = False
    project_config_path: Path = Path("config/project.yaml")
    knowledge_path: Path = Path("config/bot_knowledge.yaml")
    fake_examples_path: Path = Path("config/fake_ai_examples.yaml")

    @field_validator("tiktok_username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        value = value.strip().removeprefix("@")
        if value and not re.fullmatch(r"[A-Za-z0-9._]{1,24}", value):
            raise ValueError("Use a TikTok handle, not a URL")
        return value

    @model_validator(mode="after")
    def validate_modes(self) -> Self:
        if self.diagnostic and self.live_mode not in {"real", "replay"}:
            raise ValueError("Diagnostic mode requires LIVE_MODE=real or replay")
        if self.live_mode == "replay":
            if not self.replay_scenario:
                raise ValueError("Replay mode requires --scenario")
            self.tiktok_username = "replay"
        elif self.replay_scenario is not None:
            raise ValueError("Scenario requires LIVE_MODE=replay")
        if self.live_mode == "real" and not self.tiktok_username:
            raise ValueError("TIKTOK_USERNAME is required for LIVE_MODE=real")
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise ValueError("Reconnect maximum must be >= initial delay")
        if self.fake_repeat and self.fake_interval_seconds == 0:
            raise ValueError("Repeating fake mode requires a positive interval")
        return self
