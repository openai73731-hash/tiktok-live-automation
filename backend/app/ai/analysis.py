"""Strict analyzer contract shared by local and cloud providers."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AIAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")
    detected_language: str = Field(pattern=r"^[a-z]{2,3}$")
    operator_translation: str | None = Field(max_length=4000)
    should_reply: bool
    # Social responses are selected separately from a finite approved bank.
    intent: Literal["facts", "no_promises", "unknown", "none"]
    fact_ids: list[str] = Field(max_length=2)
    reason: Literal["approved_facts", "financial_claim", "knowledge_missing", "no_reply", "unsupported"]

    @model_validator(mode="after")
    def consistent_selection(self):
        if (not self.should_reply and (self.intent != "none" or self.fact_ids)
                or self.should_reply and self.intent == "none"
                or self.intent != "facts" and self.fact_ids):
            raise ValueError("Inconsistent selection")
        return self
