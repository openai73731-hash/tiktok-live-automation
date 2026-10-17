"""One explicitly applied generic profile; legacy projects remain valid."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProfileValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ProfileModel(ProfileValue):
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = "http://127.0.0.1:1234/v1"
    model: str = Field(default="qwen/qwen3-8b", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    temperature: float = Field(default=.2, ge=0, le=1, allow_inf_nan=False)
    max_tokens: int = Field(default=512, ge=128, le=2048)

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        from app.ai.knowledge import local_base_url
        return local_base_url(value)


class KnowledgeSettings(ProfileValue):
    enabled: bool = Field(default=True, strict=True)
    top_k: int = Field(default=3, ge=1, le=8, strict=True)


class PeriodicSettings(ProfileValue):
    enabled: bool = Field(default=False, strict=True)
    interval_seconds: int = Field(default=300, ge=60, le=86400, strict=True)
    jitter_seconds: int = Field(default=30, ge=0, le=300, strict=True)
    mode: Literal["templates", "ai", "mixed"] = "templates"
    instruction: str = Field(default="Write one short, neutral comment inviting conversation about the topic.", max_length=800)
    templates: tuple[str, ...] = Field(default=("If you have a question about the topic, feel free to ask.",
        "What would you like to discuss next?", "Welcome everyone, feel free to join the conversation."), max_length=12)
    max_per_hour: int = Field(default=12, ge=1, le=60, strict=True)

    @field_validator("templates")
    @classmethod
    def texts(cls, values):
        if any(not s.strip() or len(s) > 240 or any(ord(c) < 32 for c in s) for s in values):
            raise ValueError("Templates must contain 1..240 printable characters")
        return tuple(dict.fromkeys(s.strip() for s in values))

    @model_validator(mode="after")
    def limits(self):
        if self.jitter_seconds > self.interval_seconds // 2:
            raise ValueError("Jitter must not exceed half the interval")
        if self.enabled and self.mode in {"templates", "mixed"} and not self.templates:
            raise ValueError("Template mode requires at least one template")
        return self


class AIProfile(ProfileValue):
    name: str = Field(default="LIVE Assistant", min_length=1, max_length=80)
    topic: str = Field(default="General conversation about this LIVE", min_length=1, max_length=300)
    language: str = Field(default="en", pattern=r"^[a-z]{2,3}$")
    system_prompt: str = Field(default="You are the host's helpful TikTok LIVE assistant. Be honest and concise.", min_length=1, max_length=3000)
    reply_instruction: str = Field(default="Answer the viewer's question briefly. Use relevant supplied knowledge for business facts; say when a fact is unavailable.", min_length=1, max_length=1000)
    tone: str = Field(default="Friendly, calm and natural", min_length=1, max_length=160)
    model: ProfileModel = Field(default_factory=ProfileModel)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    periodic_comments: PeriodicSettings = Field(default_factory=PeriodicSettings)
