"""Environment configuration. Load .env from the selected working directory."""
import re
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ReplayScenarioName = Literal[
    "html_room_success", "html_fail_api_success", "html_challenge", "api_missing_room",
    "live_with_comments", "live_end", "temporary_disconnect", "rate_limit", "security_event",
    "unknown_events_then_comments",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8-sig",
        extra="ignore", validate_default=True,
    )

    live_mode: Literal["real", "fake", "replay"] = "fake"
    replay_scenario: ReplayScenarioName | None = None
    tiktok_username: str = ""
    diagnostic: bool = False
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
