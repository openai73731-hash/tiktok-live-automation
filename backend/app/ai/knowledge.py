"""Validated local configuration and approved product knowledge."""
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    provider: Literal["fake", "openai"] = "fake"
    max_reply_sentences: int = Field(default=2, ge=1, le=2)
    max_reply_chars: int = Field(default=240, ge=40, le=400)
    max_comment_chars: int = Field(default=1000, ge=50, le=10_000)
    operator_language: str = Field(default="ru", pattern=r"^[a-z]{2,3}$")
    timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)


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

    @model_validator(mode="after")
    def validate_knowledge(self) -> Self:
        ids = [fact.id for fact in self.facts]
        if len(ids) != len(set(ids)):
            raise ValueError("Fact IDs must be unique")
        if not REQUIRED_RULES <= self.forbidden_claims:
            raise ValueError("Mandatory business rules cannot be disabled")
        if set(self.safe_responses) != {"no_promises", "unknown"}:
            raise ValueError("Expected no_promises and unknown safe responses")
        return self

    def find_fact(self, fact_id: str) -> Fact | None:
        return next((fact for fact in self.facts if fact.id == fact_id), None)


def load_yaml_model(path: Path, model: type[ConfigModel]):
    # SafeLoader does not execute Python YAML tags.
    with path.open(encoding="utf-8-sig") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a YAML mapping")
    return model.model_validate(data)
