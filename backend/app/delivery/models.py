"""Immutable server bindings and minimal delivery/device contracts."""
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.ai.models import ReplySuggestion

OpaqueId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Account = Annotated[str, Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9._]+$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SuggestionEnvelope(ReplySuggestion):
    suggestion_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    suggestion_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: Literal["tiktok", "fake", "replay", "playground"]
    comment_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    comment_identity_kind: Literal["sdk_message_id", "fingerprint"]
    created_at: AwareDatetime
    expires_at: AwareDatetime
    live_session_id: OpaqueId | None
    target_account: Account | None
    active_runtime_revision: OpaqueId
    knowledge_revision: Revision
    policy_revision: Revision
    suggested_reply_hash: Revision | None


class ApprovalInput(FrozenModel):
    expected_suggestion_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    translation_review_confirmed: bool = Field(default=False, strict=True)


class Approval(FrozenModel):
    approval_id: OpaqueId
    suggestion_id: OpaqueId
    suggestion_revision: Revision
    suggested_reply_hash: Revision
    live_session_id: OpaqueId | None
    active_runtime_revision: OpaqueId
    knowledge_revision: Revision
    policy_revision: Revision
    approved_at: AwareDatetime
    translation_review_confirmed: bool


EligibilityReason = Literal[
    "eligible", "suggestion_not_ready", "source_not_live", "integrity_mismatch",
    "approval_missing", "approval_stale", "translation_review_missing",
    "live_session_mismatch", "target_account_mismatch", "runtime_revision_mismatch",
    "knowledge_revision_mismatch", "policy_revision_mismatch", "expired",
    "duplicate_operation", "live_not_connected", "live_inactive", "operator_paused",
    "delivery_unavailable", "device_unavailable",
]


class Eligibility(FrozenModel):
    eligible: bool
    reason: EligibilityReason


class DeliveryContext(FrozenModel):
    live_session_id: str | None
    target_account: str | None
    active_runtime_revision: str
    knowledge_revision: str
    policy_revision: str
    connected: bool = False
    active: bool = False
    operator_paused: bool = False
    delivery_available: bool = False
    device_available: bool = False


class SuggestionView(FrozenModel):
    envelope: SuggestionEnvelope
    approval: Approval | None = None
    approval_state: Literal["missing", "approved", "stale"]
    eligibility: Eligibility
    delivery: Literal["NOT CONFIGURED"] = "NOT CONFIGURED"


class ApprovalEvent(FrozenModel):
    suggestion_id: str
    approval_state: Literal["approved"] = "approved"


DeliveryStatus = Literal["confirmed", "not_sent", "unknown", "needs_operator"]
DeliveryReason = Literal["mock_confirmed", "not_configured", "rejected", "timeout",
                         "cancelled", "sender_failed", "interrupted", "operator_required"]


class DeliveryResult(FrozenModel):
    status: DeliveryStatus
    reason: DeliveryReason


class DeliveryRequest(FrozenModel):
    operation_id: OpaqueId
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    suggestion_id: OpaqueId
    suggestion_revision: Revision
    comment_identity: Revision
    approval_id: OpaqueId
    suggested_reply_hash: Revision
    text: str = Field(min_length=1, max_length=400)
    live_session_id: OpaqueId
    target_account: Account
    active_runtime_revision: OpaqueId
    expires_at: AwareDatetime


class ReplySenderProtocol(Protocol):
    async def send(self, request: DeliveryRequest) -> DeliveryResult: ...


class AndroidTarget(FrozenModel):
    live_session_id: OpaqueId
    account: Account


class AndroidSubmission(FrozenModel):
    operation_id: OpaqueId
    target: AndroidTarget
    text: str = Field(min_length=1, max_length=400)
    expires_at: AwareDatetime


class AndroidState(FrozenModel):
    state: Literal["not_configured", "ready", "needs_operator"]


class AndroidControllerProtocol(Protocol):
    async def inspect(self, target: AndroidTarget) -> AndroidState: ...
    async def submit(self, request: AndroidSubmission) -> DeliveryResult: ...
