"""Bounded, allowlisted evaluation data; no settings or provider payloads."""
from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from app.admin.models import ReplyRecord, UIModel
from app.ai.models import ResponseKind

Category = Literal[
    "automatic_analysis", "automatic_trade_execution", "combined_analysis_trades",
    "how_it_works", "win_rate_profitability", "guarantees", "income_money",
    "greetings", "thanks", "spam_random", "off_topic", "prompt_injection",
    "ambiguous", "informal_english", "multilingual",
]
ReviewGrade = Literal["unreviewed", "good", "bad"]


class Expected(UIModel):
    should_reply: bool = Field(strict=True)
    intent: ResponseKind
    fact_ids: tuple[str, ...] = Field(max_length=2)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if len(set(self.fact_ids)) != len(self.fact_ids):
            raise ValueError("Duplicate expected facts")
        if (self.intent == "facts") != bool(self.fact_ids):
            raise ValueError("Only facts intent selects facts")
        if self.should_reply != (self.intent != "none"):
            raise ValueError("Reply expectation must agree with intent")
        return self


class QualityCase(UIModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    category: Category
    viewer_text: str = Field(min_length=1, max_length=1000)
    language: str = Field(pattern=r"^[a-z]{2,3}$")
    expected: Expected
    expected_status: Literal["ready", "skipped", "needs_operator"] | None = None
    notes: str | None = Field(default=None, max_length=1500)

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        if not self.viewer_text.strip():
            raise ValueError("Empty comment")
        if self.expected_status and (self.expected_status == "ready") != self.expected.should_reply:
            raise ValueError("Status expectation must agree with reply expectation")
        return self


class Dataset(UIModel):
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    cases: tuple[QualityCase, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("Case IDs must be unique")
        return self


class ManualReview(UIModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    comment_translation_quality: ReviewGrade = "unreviewed"
    reply_naturalness: ReviewGrade = "unreviewed"
    reply_translation_quality: ReviewGrade = "unreviewed"
    note: str = Field(default="", max_length=1000)


class CaseResult(UIModel):
    case_id: str
    record: ReplyRecord
    checks: dict[str, bool]
    passed: bool
    critical_failures: tuple[str, ...]
    other_failures: tuple[str, ...]
    pipeline_error: Literal["processing_failed", "provider_error"] | None = None
    review: ManualReview = Field(default_factory=ManualReview)


class Breakdown(UIModel):
    total: int
    passed: int
    failed: int


class Summary(UIModel):
    dataset_total: int
    total: int = 0
    not_run: int
    passed: int = 0
    failed: int = 0
    critical_failed: int = 0
    should_reply_accuracy: float | None = None
    intent_accuracy: float | None = None
    fact_ids_accuracy: float | None = None
    safe_reply_count: int = 0
    ready_count: int = 0
    needs_operator_count: int = 0
    skipped_count: int = 0
    average_duration_ms: float | None = None
    p50_duration_ms: int | None = None
    p95_duration_ms: int | None = None
    languages: dict[str, Breakdown] = Field(default_factory=dict)
    categories: dict[str, Breakdown] = Field(default_factory=dict)


class RunView(UIModel):
    revision: int
    run_id: str | None = None
    state: Literal["idle", "running", "stopping", "completed", "stopped", "failed"] = "idle"
    dataset_name: str
    dataset_version: str
    dataset_hash: str
    knowledge_hash: str
    ai_config_hash: str
    provider: str
    model: str
    enabled: bool
    disabled_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current_case_id: str | None = None
    error_category: Literal["run_failed"] | None = None
    summary: Summary


class QualitySnapshot(UIModel):
    dataset: Dataset
    run: RunView
    results: tuple[CaseResult, ...] = ()


class QualityUpdate(UIModel):
    run: RunView
    result: CaseResult | None = None


class RunInput(UIModel):
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class ReviewInput(RunInput):
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    review: ManualReview
