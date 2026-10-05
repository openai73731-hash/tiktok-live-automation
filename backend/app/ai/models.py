"""Provider-independent contracts; no vendor SDK types."""
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.ai.knowledge import KnowledgeBase, ProjectConfig

ResponseKind = Literal["facts", "no_promises", "unknown", "none"]


class ReplyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    original_text: str
    is_candidate: bool
    project: ProjectConfig
    knowledge: KnowledgeBase


class ReplyDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")
    detected_language: str = Field(pattern=r"^[a-z]{2,3}$")
    operator_translation: str | None = None
    response_kind: ResponseKind = "none"
    suggested_reply: str | None = None
    reply_translation: str | None = None
    reply_language: str | None = None
    fact_ids: tuple[str, ...] = ()
    # Honest abstention: fake has no universal translation or language detection.
    needs_operator: bool = False


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
    # A ready suggestion has NEVER been approved or sent.
    requires_approval: Literal[True] = True
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.should_reply:
            if self.status != "ready" or not all((
                self.suggested_reply, self.reply_translation, self.operator_translation,
            )) or self.detected_language == "und":
                raise ValueError("Ready replies need a known language and both translations")
        elif self.status == "ready" or self.suggested_reply or self.reply_translation or self.fact_ids:
            raise ValueError("Non-ready results must not carry a deliverable reply")
        return self
