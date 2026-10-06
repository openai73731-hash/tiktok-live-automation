"""Validated local configuration and approved product knowledge."""
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from urllib.parse import urlsplit


def local_base_url(value: str) -> str:
    """Only the literal loopback hosts and the OpenAI-compatible /v1 root."""
    try:
        url = urlsplit(value)
        valid = (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}
                 and url.port is not None and 1 <= url.port <= 65535
                 and url.username is None and url.password is None
                 and url.path in {"/v1", "/v1/"} and "?" not in value and "#" not in value
                 and not any(c.isspace() or ord(c) < 32 for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("LM Studio requires http://loopback:<port>/v1 without credentials or query")
    # Resolve localhost to a literal loopback so DNS/hosts cannot redirect it.
    host = "[::1]" if url.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{url.port}/v1"

REQUIRED_RULES = frozenset({
    "guaranteed_profit", "guaranteed_income", "guaranteed_win_rate",
    "invented_performance_metrics", "invented_platform_facts", "financial_promises",
})


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class StreamConfig(ConfigModel):
    language: str = Field(default="en", pattern=r"^[a-z]{2,3}$")


class ProductConfig(ConfigModel):
    name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=100)


class AIConfig(ConfigModel):
    provider: Literal["fake", "openai", "lmstudio"] = "fake"
    model: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "none"
    reply_policy: Literal["focused", "friendly"] = "focused"
    max_reply_sentences: int = Field(default=2, ge=1, le=3)
    max_reply_chars: int = Field(default=240, ge=40, le=400)
    max_comment_chars: int = Field(default=1000, ge=50, le=10_000)
    operator_language: str = Field(default="ru", pattern=r"^[a-z]{2,3}$")
    timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    lmstudio_base_url: str = "http://127.0.0.1:1234/v1"
    temperature: float = Field(default=0.2, ge=0, le=2, allow_inf_nan=False)
    max_model_output_tokens: int = Field(default=200, ge=64, le=4096)
    prefer_single_sentence: bool = False
    thinking_enabled: bool = False

    @model_validator(mode="after")
    def policy_limits(self):
        if self.reply_policy == "focused" and self.max_reply_sentences > 2:
            raise ValueError("Focused replies allow at most two sentences")
        return self

    @field_validator("lmstudio_base_url", mode="before")
    @classmethod
    def validate_local_url(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("LM Studio URL must be a string")
        return local_base_url(value)


class ProjectConfig(ConfigModel):
    stream: StreamConfig
    product: ProductConfig
    ai: AIConfig


class Fact(ConfigModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    text: str = Field(min_length=1, max_length=400)
    translations: dict[str, str]

    def localized(self, language: str) -> str | None:
        return self.text if language == "en" else self.translations.get(language)


class KnowledgeBase(ConfigModel):
    facts: tuple[Fact, ...]
    forbidden_claims: frozenset[str]
    # Approved neutral responses, not product or platform claims.
    safe_responses: dict[str, dict[str, str]]
    reply_templates: tuple["ApprovedTemplate", ...] = ()

    @model_validator(mode="after")
    def validate_knowledge(self) -> Self:
        ids = [fact.id for fact in self.facts]
        if len(ids) != len(set(ids)):
            raise ValueError("Fact IDs must be unique")
        if not REQUIRED_RULES <= self.forbidden_claims:
            raise ValueError("Mandatory business rules cannot be disabled")
        if set(self.safe_responses) != {"no_promises", "unknown"}:
            raise ValueError("Expected no_promises and unknown safe responses")
        template_ids = [t.id for t in self.reply_templates]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("Reply template IDs must be unique")
        for template in self.reply_templates:
            if not set(template.fact_ids) <= set(ids):
                raise ValueError("Reply template references unknown facts")
        return self

    def find_fact(self, fact_id: str) -> Fact | None:
        return next((fact for fact in self.facts if fact.id == fact_id), None)


class ApprovedTemplate(ConfigModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    fact_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    texts: dict[str, str]
    # Bind variants to the approved source revision. Editing a fact requires
    # reviewing/updating its templates instead of silently using stale claims.
    source_texts: dict[str, dict[str, str]]

    @model_validator(mode="after")
    def valid_template(self):
        if len(self.fact_ids) != len(set(self.fact_ids)) or "en" not in self.texts:
            raise ValueError("Templates require unique facts and canonical English")
        if set(self.source_texts) != set(self.texts):
            raise ValueError("Each template translation requires its approved source texts")
        if any(not text.strip() or len(text) > 400 for text in self.texts.values()):
            raise ValueError("Empty or oversized template")
        return self


KnowledgeBase.model_rebuild()


def load_yaml_model(path: Path, model: type[ConfigModel]):
    # SafeLoader does not execute Python YAML tags.
    with path.open(encoding="utf-8-sig") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a YAML mapping")
    return model.model_validate(data)
