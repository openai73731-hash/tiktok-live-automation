"""Conservative extractive policy: approved wording plus mandatory claim checks.

This is deliberately NOT a semantic verifier for arbitrary LLM paraphrases.
A future provider cannot bypass the policy just by citing an existing fact ID.
"""
import re
import unicodedata

from app.ai.models import ReplyDraft, ReplyRequest

# Negative statements such as "Profit is not guaranteed" are intentionally allowed.
FORBIDDEN_PATTERNS = (
    r"\bguarantee(?:d|s)?\s+(?:\w+\s+){0,2}(?:profits?|income|earnings|returns?|win\s*rate)\b",
    r"\b(?:profits?|income|earnings|returns?)\s+(?:(?:is|are)\s+)?guaranteed\b",
    r"\b(?:risk[- ]free|zero risk|no risk|sure profit)\b",
    r"\b(?:you|everyone)\s+(?:will|always)\s+(?:earn|profit|win|make money)\b",
    r"100\s*%\s*(?:win|success|profit)",
    r"гарант\w*\s+(?:\w+\s+){0,2}(?:прибыл|доход|заработ|выигрыш)",
    r"(?:прибыл\w*|доход\w*|заработ\w*)\s+гарант",
    r"без\s+риска|вы\s+(?:точно\s+)?заработаете",
    r"(?:ganancias?|ingresos?|beneficios?)\s+garantizad|sin\s+riesgo",
)


def claim_violations(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in FORBIDDEN_PATTERNS)


def sentence_count(text: str) -> int:
    # Conservative sentence limit; abbreviations may require operator review.
    return len([s for s in re.split(r"[.!?。！？]+(?:\s*|$)", text) if s.strip()])


def approved_reply(
    request: ReplyRequest, kind: str, fact_ids: tuple[str, ...], language: str,
) -> str | None:
    if kind == "facts":
        if not fact_ids or len(fact_ids) != len(set(fact_ids)):
            return None
        parts = []
        for fact_id in fact_ids:
            fact = request.knowledge.find_fact(fact_id)
            text = fact.localized(language) if fact else None
            if not text:
                return None
            parts.append(text)
        return " ".join(parts)
    if kind in {"no_promises", "unknown"} and not fact_ids:
        return request.knowledge.safe_responses.get(kind, {}).get(language)
    return None


def style_violations(text: str) -> bool:
    return bool(
        any(unicodedata.category(c) in {"So", "Cs", "Cc", "Cf"} for c in text)
        or re.search(r"[#*_`<>\[\]{}]|https?://|www\.|[!?]{2,}|\.{3,}|[:;=8][-^]?[)(DPp]|^[-•]\s", text)
        or re.search(r"\bas an ai\b|thank you for (?:your|the) question|sophisticated.*solution|"
                     r"revolutionary|game[- ]changing|customer support|спасибо за.*вопрос|как ии", text, re.I)
    )


class ReplySafetyPolicy:
    def violations(self, request: ReplyRequest, draft: ReplyDraft) -> tuple[str, ...]:
        from app.ai.composer import expected_text
        issues = []
        if draft.detected_language == "und":
            issues.append("language_unknown")
        if draft.reply_language != draft.detected_language:
            issues.append("reply_language_mismatch")
        if not draft.operator_translation or not draft.operator_translation.strip():
            issues.append("operator_translation_missing")
        if not draft.reply_translation or not draft.reply_translation.strip():
            issues.append("reply_translation_missing")
        for label, language, text in (
            ("reply", draft.detected_language, draft.suggested_reply),
            ("translation", request.project.ai.operator_language, draft.reply_translation),
        ):
            if not text:
                issues.append(f"{label}_empty")
                continue
            if claim_violations(text):
                issues.append(f"{label}_financial_promise")
            expected = (approved_reply(request, draft.response_kind, draft.fact_ids, language)
                        if draft.composition_style == "canonical" else expected_text(request, draft, language))
            if draft.response_kind == "social":
                from app.ai.social import expected_social
                expected = expected_social(request, draft, language)
            constrained = draft.composition_style == "constrained_translation" and label == "reply"
            if constrained:
                evidence = draft.translation_evidence
                source = expected_text(request, draft, "en")
                operator = expected_text(request, draft, request.project.ai.operator_language)
                if (not evidence or not source or not operator
                        or request.project.ai.provider != "lmstudio"
                        or request.project.ai.operator_language != "ru"
                        or evidence.target_language != draft.detected_language
                        or evidence.viewer_language_reply != draft.suggested_reply
                        or evidence.source_en != source or evidence.back_translation_en != source
                        or evidence.operator_reply_translation_ru != operator
                        or draft.reply_translation != operator
                        or expected is not None):
                    issues.append("translation_evidence_invalid")
            elif expected is None or text != expected:
                issues.append(f"{label}_ungrounded")
            if (draft.composition_style != "canonical" or draft.response_kind == "social") and style_violations(text):
                issues.append(f"{label}_style")
            if not 1 <= sentence_count(text) <= request.project.ai.max_reply_sentences:
                issues.append(f"{label}_sentence_limit")
            if len(text) > request.project.ai.max_reply_chars:
                issues.append(f"{label}_length_limit")
        return tuple(issues)
