"""Bounded server-authoritative suggestions/approvals and pure eligibility."""
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
import hashlib
import json
from uuid import uuid4

from app.delivery.models import (Approval, DeliveryContext, Eligibility, SuggestionEnvelope,
                                 SuggestionView)


def digest(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def reply_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def policy_revision(processor):
    from app.ai.social import bank
    return digest({"version": "stage4a4-safety-v1", "project": processor.project.model_dump(mode="json"),
                   "social": bank().model_dump(mode="json")})


def intact(envelope):
    return (envelope.suggested_reply_hash == reply_hash(envelope.suggested_reply) and
            envelope.suggestion_revision == digest(envelope.model_dump(mode="json", exclude={"suggestion_revision"})))


def binding_reason(e, context, now):
    if not intact(e): return "integrity_mismatch"
    if e.status != "ready" or not e.should_reply or not e.suggested_reply: return "suggestion_not_ready"
    if now >= e.expires_at: return "expired"
    if e.active_runtime_revision != context.active_runtime_revision: return "runtime_revision_mismatch"
    if e.knowledge_revision != context.knowledge_revision: return "knowledge_revision_mismatch"
    if e.policy_revision != context.policy_revision: return "policy_revision_mismatch"
    if e.source != "playground":
        if e.live_session_id is None or e.live_session_id != context.live_session_id: return "live_session_mismatch"
        if e.target_account != context.target_account: return "target_account_mismatch"
        if not context.connected: return "live_not_connected"
        if not context.active: return "live_inactive"
    return None


def approval_matches(e, approval):
    names = ("suggestion_id", "suggestion_revision", "suggested_reply_hash", "live_session_id",
             "active_runtime_revision", "knowledge_revision", "policy_revision")
    return approval is not None and all(getattr(e, name) == getattr(approval, name) for name in names)


class DeliveryEligibility:
    """No inference, browser text, network, device calls or journal mutation."""
    @staticmethod
    def evaluate(e, approval, context, *, duplicate=False, now=None):
        reason = binding_reason(e, context, now or datetime.now(UTC))
        if reason is None:
            if e.source != "tiktok": reason = "source_not_live"
            elif approval is None: reason = "approval_missing"
            elif not approval_matches(e, approval): reason = "approval_stale"
            elif e.translation_requires_review and not approval.translation_review_confirmed: reason = "translation_review_missing"
            elif duplicate: reason = "duplicate_operation"
            elif context.operator_paused: reason = "operator_paused"
            elif not context.delivery_available: reason = "delivery_unavailable"
            elif not context.device_available: reason = "device_unavailable"
        return Eligibility(eligible=reason is None, reason=reason or "eligible")


class SuggestionConflict(ValueError):
    """Static conflict only; never echo viewer/provider/exception text."""


class SuggestionStore:
    def __init__(self):
        self.groups = {"playground": OrderedDict(), "live": OrderedDict()}
        self.approvals = {}

    def register(self, record, comment, processor, runtime_revision, *, session_id=None, target_account=None):
        identity_kind = "sdk_message_id" if comment.message_id and record.source == "tiktok" else "fingerprint"
        # Fallback is a conservative 30-second receipt bucket, not a TikTok ID.
        identity = digest({"source": record.source, "session": session_id, "account": target_account,
            "id": comment.message_id if identity_kind == "sdk_message_id" else None,
            "fallback": None if identity_kind == "sdk_message_id" else
            [comment.user_id, comment.username, comment.text, int(comment.timestamp.timestamp()) // 30]})
        now = datetime.now(UTC)
        values = record.suggestion.model_dump(mode="json") | dict(suggestion_id=record.id,
            source=record.source, comment_identity=identity, comment_identity_kind=identity_kind,
            created_at=now.isoformat(), expires_at=(comment.timestamp + timedelta(minutes=5)).isoformat(),
            live_session_id=session_id, target_account=target_account,
            active_runtime_revision=runtime_revision, knowledge_revision=digest(processor.knowledge),
            policy_revision=policy_revision(processor), suggested_reply_hash=reply_hash(record.suggestion.suggested_reply))
        envelope = SuggestionEnvelope.model_validate(values | {"suggestion_revision": "0" * 64})
        envelope = envelope.model_copy(update={"suggestion_revision": digest(envelope.model_dump(mode="json", exclude={"suggestion_revision"}))})
        group = self.groups["playground" if record.source == "playground" else "live"]
        group[record.id] = envelope
        limit = 100 if record.source == "playground" else 50
        while len(group) > limit:
            old_id, _ = group.popitem(last=False)
            self.approvals.pop(old_id, None)
        return envelope

    def get(self, suggestion_id):
        e = self.groups["playground"].get(suggestion_id) or self.groups["live"].get(suggestion_id)
        if e is None:
            raise SuggestionConflict("Предложение недоступно. Обновите историю.")
        return e

    def approve(self, suggestion_id, body, context, *, now=None):
        e = self.get(suggestion_id)
        now = now or datetime.now(UTC)
        if e.suggestion_revision != body.expected_suggestion_revision or binding_reason(e, context, now):
            raise SuggestionConflict("Предложение устарело или недоступно для approval. Обновите карточку.")
        if e.translation_requires_review and not body.translation_review_confirmed:
            raise SuggestionConflict("Подтвердите ручную проверку машинного перевода.")
        old = self.approvals.get(suggestion_id)
        if approval_matches(e, old) and (not e.translation_requires_review or old.translation_review_confirmed):
            return old
        approval = Approval(approval_id=uuid4().hex,
            **{name: getattr(e, name) for name in ("suggestion_id", "suggestion_revision", "suggested_reply_hash",
                "live_session_id", "active_runtime_revision", "knowledge_revision", "policy_revision")},
            approved_at=now, translation_review_confirmed=body.translation_review_confirmed)
        self.approvals[suggestion_id] = approval
        return approval

    def view(self, suggestion_id, context):
        e = self.get(suggestion_id)
        approval = self.approvals.get(suggestion_id)
        valid = approval_matches(e, approval) and binding_reason(e, context, datetime.now(UTC)) is None
        return SuggestionView(envelope=e, approval=approval,
            approval_state="missing" if approval is None else "approved" if valid else "stale",
            eligibility=DeliveryEligibility.evaluate(e, approval, context))
