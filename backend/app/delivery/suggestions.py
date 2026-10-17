"""Bounded server-authoritative suggestions/approvals and pure eligibility."""
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
import hashlib
import json
from uuid import uuid4
from time import monotonic

from app.delivery.models import (Approval, DeliveryContext, Eligibility, SuggestionEnvelope,
                                 SuggestionView)


def digest(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def reply_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def comment_identity(comment, source, session_id, target_account):
    if source == 'periodic' and comment._periodic_id:
        return digest({'source':'periodic','account':(target_account or '').casefold(),'slot':comment._periodic_id}), 'periodic_slot'
    kind = "sdk_message_id" if comment.message_id and source == "tiktok" else "fingerprint"
    return digest({"source": source, "session": session_id, "account": target_account,
        "id": comment.message_id if kind == "sdk_message_id" else None,
        "fallback": None if kind == "sdk_message_id" else
        [comment.user_id, comment.username, comment.text, int(comment.timestamp.timestamp()) // 30]}), kind


def durable_delivery_key(source, account, message_id):
    if source != "tiktok" or not message_id: return None
    return digest({"version": "sdk-delivery-v2", "source": source,
        "account": (account or "").casefold(), "id": message_id})


def policy_revision(processor):
    from app.ai.social import bank
    return digest({"version": "stage4a4-safety-v1", "project": processor.project.model_dump(mode="json"),
                   "social": bank().model_dump(mode="json")})


def knowledge_revision(processor):
    kb = getattr(processor, 'kb', None)
    return digest({'legacy':processor.knowledge.model_dump(mode='json'), 'local':kb.revision}) if kb else digest(processor.knowledge)


def intact(envelope):
    return (envelope.suggested_reply_hash == reply_hash(envelope.suggested_reply) and
            envelope.suggestion_revision == digest(envelope.model_dump(mode="json", exclude={"suggestion_revision"})))


def binding_reason(e, context, now=None, *, monotonic_now=None):
    if not intact(e): return "integrity_mismatch"
    if e.status != "ready" or not e.should_reply or not e.suggested_reply: return "suggestion_not_ready"
    # UTC is display/audit only. Copies reconstructed from browser JSON have no lease.
    instant = monotonic() if monotonic_now is None else monotonic_now
    if e._monotonic_deadline is None or instant >= e._monotonic_deadline: return "expired"
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
    def evaluate(e, approval, context, *, duplicate=False, now=None, monotonic_now=None):
        reason = binding_reason(e, context, monotonic_now=monotonic_now)
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
        self.auto_authorizations = {}
        self.sequence = 0
        self.auto_registration_epoch = lambda envelope: None

    def register(self, record, comment, processor, runtime_revision, *, session_id=None, target_account=None):
        # Fallback is a conservative 30-second receipt bucket, not a TikTok ID.
        identity, identity_kind = comment_identity(comment, record.source, session_id, target_account)
        now = datetime.now(UTC)
        values = record.suggestion.model_dump(mode="json") | dict(suggestion_id=record.id,
            source=record.source, comment_identity=identity, comment_identity_kind=identity_kind,
            durable_delivery_key=durable_delivery_key(record.source, target_account, comment.message_id),
            created_at=now.isoformat(), expires_at=(comment.timestamp + timedelta(minutes=5)).isoformat(),
            live_session_id=session_id, target_account=target_account,
            active_runtime_revision=runtime_revision, knowledge_revision=knowledge_revision(processor),
            policy_revision=policy_revision(processor), suggested_reply_hash=reply_hash(record.suggestion.suggested_reply))
        envelope = SuggestionEnvelope.model_validate(values | {"suggestion_revision": "0" * 64})
        envelope = envelope.model_copy(update={"suggestion_revision": digest(envelope.model_dump(mode="json", exclude={"suggestion_revision"}))})
        envelope._monotonic_deadline = comment._received_monotonic + 300
        from app.delivery.automatic import verified_content, content_binding
        envelope._auto_content_verified = verified_content(record.suggestion, processor)
        envelope._auto_content_binding = content_binding(envelope)
        envelope._created_monotonic = monotonic()
        self.sequence += 1
        envelope._created_sequence = self.sequence
        envelope._source_message_id = comment.message_id if identity_kind == "sdk_message_id" else None
        if record.source == 'periodic' and identity_kind == 'periodic_slot':
            envelope._periodic_id, envelope._periodic_proof = comment._periodic_id, comment._periodic_proof
            values = envelope.model_dump(mode='json')
            values['durable_delivery_key'] = digest({'source':'periodic','account':(target_account or '').casefold(),'slot':comment._periodic_id})
            # Rebind immutable public metadata before recording private admission.
            updated = envelope.model_copy(update={'durable_delivery_key':values['durable_delivery_key']})
            updated = updated.model_copy(update={'suggestion_revision':digest(updated.model_dump(mode='json',exclude={'suggestion_revision'}))})
            envelope = updated
        receipt = comment._ingestion_receipt
        if (receipt and record.source in {'tiktok','periodic'} and receipt.account == target_account and
                receipt.session_id == session_id and receipt.runtime_revision == runtime_revision and
                receipt.message_id == envelope._source_message_id):
            envelope._ingestion_receipt = receipt
        # Admission is evaluated at registration too; a later epoch cannot admit
        # an earlier suggestion, regardless of approval or AI completion timing.
        envelope._registered_auto_epoch_id = self.auto_registration_epoch(envelope)
        group = self.groups["playground" if record.source == "playground" else "live"]
        group[record.id] = envelope
        limit = 100 if record.source == "playground" else 50
        while len(group) > limit:
            old_id, _ = group.popitem(last=False)
            self.approvals.pop(old_id, None)
            self.auto_authorizations.pop(old_id, None)
        return envelope

    def get(self, suggestion_id):
        e = self.groups["playground"].get(suggestion_id) or self.groups["live"].get(suggestion_id)
        if e is None:
            raise SuggestionConflict("Предложение недоступно. Обновите историю.")
        return e

    def approve(self, suggestion_id, body, context, *, now=None):
        e = self.get(suggestion_id)
        now = now or datetime.now(UTC)
        if e.suggestion_revision != body.expected_suggestion_revision or binding_reason(e, context):
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
        approval._approved_monotonic = monotonic()
        self.sequence += 1
        approval._approved_sequence = self.sequence
        self.approvals[suggestion_id] = approval
        return approval

    def view(self, suggestion_id, context):
        e = self.get(suggestion_id)
        approval = self.approvals.get(suggestion_id)
        valid = approval_matches(e, approval) and binding_reason(e, context) is None
        return SuggestionView(envelope=e, approval=approval,
            approval_state="missing" if approval is None else "approved" if valid else "stale",
            eligibility=DeliveryEligibility.evaluate(e, approval, context))

    def authoritative(self, suggestion_id, approval_id, expected_revision, context):
        e = self.get(suggestion_id)
        approval = self.approvals.get(suggestion_id)
        if e.suggestion_revision != expected_revision or approval is None or approval.approval_id != approval_id:
            raise SuggestionConflict("Approval binding conflict")
        return e, approval, DeliveryEligibility.evaluate(e, approval, context)


    def authorize_auto(self, suggestion_id, context):
        from app.delivery.automatic import AutomaticEligibility
        e = self.get(suggestion_id)
        verdict = AutomaticEligibility.evaluate(e, context)
        if not verdict.eligible: raise SuggestionConflict("Automatic authorization rejected")
        authorization = Approval(approval_id=uuid4().hex,
            **{name: getattr(e, name) for name in ("suggestion_id", "suggestion_revision", "suggested_reply_hash",
                "live_session_id", "active_runtime_revision", "knowledge_revision", "policy_revision")},
            approved_at=datetime.now(UTC), translation_review_confirmed=False)
        self.auto_authorizations[suggestion_id] = authorization
        return authorization

    def automatic_authoritative(self, suggestion_id, approval_id, expected_revision, context):
        from app.delivery.automatic import AutomaticEligibility
        e = self.get(suggestion_id)
        authorization = self.auto_authorizations.get(suggestion_id)
        if (e.suggestion_revision != expected_revision or authorization is None
                or authorization.approval_id != approval_id or not approval_matches(e, authorization)):
            raise SuggestionConflict("Automatic authorization binding conflict")
        return e, authorization, AutomaticEligibility.evaluate(e, context)
