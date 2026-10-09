"""Provider-independent contracts; no vendor SDK types."""
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.ai.knowledge import KnowledgeBase, ProjectConfig
from app.ai.errors import ProcessingError, ProcessingErrorDetail

ResponseKind = Literal["facts", "no_promises", "unknown", "none", "social"]


class ReplyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    original_text: str
    is_candidate: bool
    project: ProjectConfig
    knowledge: KnowledgeBase


class TranslationEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    source_en: str
    target_language: str
    viewer_language_reply: str
    back_translation_en: str
    operator_reply_translation_ru: str


class ReplyDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")
    detected_language: str = Field(pattern=r"^[a-z]{2,3}$")
    # None preserves the fixture engine's deterministic candidate heuristic.
    should_reply: bool | None = None
    operator_translation: str | None = None
    response_kind: ResponseKind = "none"
    suggested_reply: str | None = None
    reply_translation: str | None = None
    reply_language: str | None = None
    fact_ids: tuple[str, ...] = ()
    # Honest abstention: fake has no universal translation or language detection.
    needs_operator: bool = False
    composition_style: Literal["canonical", "approved_template", "constrained_translation"] = "canonical"
    template_id: str | None = None
    social_id: str | None = None
    translation_evidence: TranslationEvidence | None = None


class ReplySuggestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    username: str
    original_text: str
    detected_language: str = Field(pattern=r"^[a-z]{2,3}$")
    operator_translation: str | None
    should_reply: bool
    suggested_reply: str | None
    reply_translation: str | None
    reason: str
    status: Literal["ready", "skipped", "needs_operator"]
    fact_ids: tuple[str, ...] = ()
    provider: str
    model: str | None = None
    intent: ResponseKind = "none"
    template_id: str | None = None
    social_id: str | None = None
    translation_source: Literal["approved", "constrained"] = "approved"
    translation_requires_review: bool = False
    processing_error: ProcessingError | None = None
    processing_error_detail: ProcessingErrorDetail | None = None
    # Legacy AI advisory: always false, never grants physical delivery authority.
    # Windows AUTO uses a separate server-owned AutoDeliveryEligibility decision
    # after explicit epoch activation, policy and operator approval.
    auto_send_eligible: Literal[False] = False
    # A ready suggestion has NEVER been approved or sent.
    requires_approval: Literal[True] = True
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.processing_error and self.status != "needs_operator":
            raise ValueError("Processing errors require operator intervention")
        if self.processing_error_detail and not self.processing_error:
            raise ValueError("Error detail requires a processing error")
        if self.should_reply:
            if self.status != "ready" or not all((
                self.suggested_reply, self.reply_translation, self.operator_translation,
            )) or self.detected_language == "und":
                raise ValueError("Ready replies need a known language and both translations")
        elif self.status == "ready" or self.suggested_reply or self.reply_translation or self.fact_ids:
            raise ValueError("Non-ready results must not carry a deliverable reply")
        return self
