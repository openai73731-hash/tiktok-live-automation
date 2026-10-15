"""Immutable server bindings and minimal delivery/device contracts."""
from typing import Annotated, Literal, Protocol, Callable
from app.android.models import DeviceInspection, AndroidOutcome

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PrivateAttr

from app.ai.models import ReplySuggestion
from app.live.models import IngestionReceipt

OpaqueId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Account = Annotated[str, Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9._]+$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SuggestionEnvelope(ReplySuggestion):
    # Absent on a deserialized envelope: only the server store grants lifetime.
    _monotonic_deadline: float | None = PrivateAttr(default=None)
    _auto_content_verified: bool = PrivateAttr(default=False)
    _auto_content_binding: str | None = PrivateAttr(default=None)
    _created_monotonic: float | None = PrivateAttr(default=None)
    _created_sequence: int | None = PrivateAttr(default=None)
    _source_message_id: str | None = PrivateAttr(default=None)
    _ingestion_receipt: IngestionReceipt | None = PrivateAttr(default=None)
    _registered_auto_epoch_id: str | None = PrivateAttr(default=None)
    suggestion_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    suggestion_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: Literal["tiktok", "fake", "replay", "playground"]
    comment_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    comment_identity_kind: Literal["sdk_message_id", "fingerprint"]
    durable_delivery_key: Revision | None = None
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
    _approved_monotonic: float | None = PrivateAttr(default=None)
    _approved_sequence: int | None = PrivateAttr(default=None)
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
    "delivery_unavailable", "device_unavailable", "auto_content_unverified",
    "translation_requires_review", "unicode_entry_unavailable", "automatic_mode_required",
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
    physical_send_allowed: bool = False
    text_entry_supported: bool = False
    fill_allowed: bool = False
    auto_eligibility: Eligibility = Eligibility(eligible=False, reason="automatic_mode_required")
    auto_delivery: "AutoDeliveryEligibility | None" = None
    consumed: bool = False


class AutoDeliveryEligibility(FrozenModel):
    """Trusted delivery policy, never parsed from AI output or browser input."""
    eligible: bool = False
    reason: Literal["eligible", "auto_off", "epoch_mismatch", "before_epoch", "approval_required",
        "manual_ineligible", "own_comment", "unused_required", "physical_gate", "not_armed",
        "composer_acceptance", "acceptance_spent", "delivery_paused"] = "auto_off"
    epoch_id: OpaqueId | None = None


class ApprovalEvent(FrozenModel):
    suggestion_id: str
    approval_state: Literal["approved"] = "approved"


DeliveryStatus = Literal["filled", "confirmed", "not_sent", "unknown", "needs_operator"]
DeliveryReason = Literal["mock_confirmed", "not_configured", "rejected", "timeout",
    "cancelled", "sender_failed", "interrupted", "operator_required",
    "physical_delivery_disabled", "device_unavailable", "security_ui", "unknown_screen", "hierarchy_unavailable",
    "account_unverified", "live_unverified", "profile_unverified", "unicode_entry_unavailable",
    "journal_unavailable", "delivery_paused", "cooldown", "dry_run", "shutdown",
    "filled_waiting_user_send", "feed_confirmed", "open_phone_composer", "transient_overlay",
    "confirmation_ambiguous", "automatic_mode_required", "auto_ineligible", "waiting_user_send", "filled_only", "echo_verified"]


class DeliveryResult(FrozenModel):
    status: DeliveryStatus
    reason: DeliveryReason


class DeliveryRequest(FrozenModel):
    operation_id: OpaqueId
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    suggestion_id: OpaqueId
    suggestion_revision: Revision
    comment_identity: Revision
    durable_delivery_key: Revision | None = None
    source_kind: Literal["tiktok", "fake", "replay", "playground"] | None = None
    source_message_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    approval_id: OpaqueId
    suggested_reply_hash: Revision
    text: str = Field(min_length=1, max_length=400)
    live_session_id: OpaqueId
    target_account: Account
    active_runtime_revision: OpaqueId
    expires_at: AwareDatetime
    mode: Literal["MANUAL", "AUTOMATIC", "AUTO"] = "MANUAL"
    device_serial: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class ReplySenderProtocol(Protocol):
    async def send(self, request: DeliveryRequest) -> DeliveryResult: ...


class AndroidTarget(FrozenModel):
    live_session_id: OpaqueId
    account: Account
    device_serial: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class AndroidSubmission(FrozenModel):
    operation_id: OpaqueId
    target: AndroidTarget
    text: str = Field(min_length=1, max_length=400)
    expires_at: AwareDatetime


class ManualDeliveryInput(FrozenModel):
    suggestion_id: OpaqueId
    expected_suggestion_revision: Revision
    approval_id: OpaqueId
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class DryRunView(FrozenModel):
    eligible: bool
    device_ready: bool
    would_send: bool
    reason: EligibilityReason | Literal["not_checked", "adb_unavailable", "device_missing", "multiple_devices",
        "unauthorized", "offline", "adb_failed", "tiktok_not_running", "security_ui", "unknown_screen", "hierarchy_unavailable", "profile_mismatch",
        "profile_unverified", "account_unverified", "live_unverified", "input_unavailable", "send_control_unverified", "transient_overlay", "ready", "rejected"]
    diagnostic_id: OpaqueId
    physical_action: Literal[False] = False


class AndroidState(FrozenModel):
    state: Literal["not_configured", "ready", "needs_operator"]


class AndroidControllerProtocol(Protocol):
    async def inspect(self, target: AndroidTarget | None = None) -> DeviceInspection: ...
    async def submit(self, request: AndroidSubmission, *, guard: Callable[[], bool]) -> AndroidOutcome: ...
    async def aclose(self) -> None: ...
