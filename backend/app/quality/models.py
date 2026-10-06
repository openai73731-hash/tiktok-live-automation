"""Bounded, allowlisted evaluation data; no settings or provider payloads."""
from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.admin.models import ReplyRecord, UIModel
from app.ai.models import ResponseKind
from app.ai.errors import ProcessingError

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
    pipeline_error: ProcessingError | Literal["processing_failed"] | None = None
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
    invalid_output_count: int = 0
    translation_rejected_count: int = 0
    average_duration_ms: float | None = None
    p50_duration_ms: int | None = None
    p95_duration_ms: int | None = None
    languages: dict[str, Breakdown] = Field(default_factory=dict)
    categories: dict[str, Breakdown] = Field(default_factory=dict)


class CaseConsistency(UIModel):
    case_id: str
    trials: int
    passes: int
    passing_runs: tuple[int, ...]
    flapping: bool
    selection_consistent: bool | None


class StabilitySummary(UIModel):
    runs_requested: int
    runs_completed: int = 0
    total_generations: int = 0  # Completed processor invocations, including partial trial.
    model_generation_calls: int | None = None  # Actual local POST attempts, including translator.
    pass_min: int | None = None
    pass_max: int | None = None
    mean_pass_rate: float | None = None
    critical_fail_counts: tuple[int, ...] = ()
    should_reply_mean: float | None = None
    should_reply_min: float | None = None
    intent_mean: float | None = None
    intent_min: float | None = None
    fact_ids_mean: float | None = None
    fact_ids_min: float | None = None
    invalid_output_count: int = 0
    translation_rejected_count: int = 0
    pass_consistency: dict[str, int] = Field(default_factory=dict)
    cases: tuple[CaseConsistency, ...] = ()


class TrialSummary(UIModel):
    iteration_id: str
    run_number: int
    started_at: datetime
    finished_at: datetime
    dataset_hash: str
    knowledge_hash: str
    ai_config_hash: str
    provider: str
    model: str
    temperature: float
    analysis_prompt_hash: str | None
    translation_prompt_hash: str | None = None
    model_generation_calls: int | None
    summary: Summary


class QualityRun(TrialSummary):
    results: tuple[CaseResult, ...]


class RunView(UIModel):
    service_epoch: str
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
    temperature: float
    analysis_prompt_hash: str | None = None
    translation_prompt_hash: str | None = None
    runs_requested: int = 1
    run_number: int = 1
    iteration_id: str | None = None
    iteration_started_at: datetime | None = None
    stability: StabilitySummary
    completed_summaries: tuple[TrialSummary, ...] = ()
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
    completed_runs: tuple[QualityRun, ...] = ()


class QualityUpdate(UIModel):
    run: RunView
    result: CaseResult | None = None


class StartInput(UIModel):
    runs: Literal[1, 3] = 1

    @field_validator("runs", mode="before")
    @classmethod
    def integer_runs(cls, value):
        if type(value) is not int:
            raise ValueError("Run count must be an integer")
        return value


class RunInput(UIModel):
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class ReviewInput(RunInput):
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    review: ManualReview
    run_number: int = Field(ge=1, le=3, strict=True)
