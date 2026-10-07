"""Deterministic AUTO authorization; never trust an LLM eligibility flag."""
from app.android.adb import ascii_supported
from app.ai.models import ReplyDraft, ReplyRequest
from app.ai.safety import ReplySafetyPolicy
from app.delivery.models import Eligibility
from app.delivery.suggestions import binding_reason, digest


def content_binding(e):
    return digest({k: getattr(e,k) for k in ("original_text", "detected_language", "operator_translation",
        "suggested_reply", "reply_translation", "intent", "fact_ids", "template_id", "translation_source",
        "translation_requires_review", "provider", "model", "processing_error", "knowledge_revision", "policy_revision")})


def verified_content(suggestion, processor):
    if (suggestion.status != "ready" or suggestion.intent not in {"facts", "no_promises"}
            or suggestion.translation_requires_review or suggestion.translation_source != "approved"
            or suggestion.processing_error or suggestion.detected_language == "und"):
        return False
    request = ReplyRequest(original_text=suggestion.original_text, is_candidate=True,
        project=processor.project, knowledge=processor.knowledge)
    draft = ReplyDraft(detected_language=suggestion.detected_language,
        operator_translation=suggestion.operator_translation, should_reply=True,
        response_kind=suggestion.intent, suggested_reply=suggestion.suggested_reply,
        reply_translation=suggestion.reply_translation, reply_language=suggestion.detected_language,
        fact_ids=suggestion.fact_ids, template_id=suggestion.template_id,
        composition_style="approved_template" if suggestion.template_id else "canonical")
    return not ReplySafetyPolicy().violations(request, draft)


class AutomaticEligibility:
    @staticmethod
    def evaluate(e, context, *, duplicate=False):
        reason = binding_reason(e, context)
        if reason is None:
            if e.source != "tiktok": reason = "source_not_live"
            elif e.translation_requires_review: reason = "translation_requires_review"
            elif (not e._auto_content_verified or e._auto_content_binding != content_binding(e) or e.comment_identity_kind != "sdk_message_id"
                    or e.intent not in {"facts", "no_promises"} or e.processing_error):
                reason = "auto_content_unverified"
            elif not ascii_supported(e.suggested_reply): reason = "unicode_entry_unavailable"
            elif duplicate: reason = "duplicate_operation"
            elif context.operator_paused: reason = "operator_paused"
            elif not context.delivery_available: reason = "delivery_unavailable"
            elif not context.device_available: reason = "device_unavailable"
        return Eligibility(eligible=reason is None, reason=reason or "eligible")
