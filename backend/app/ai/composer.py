"""Deterministic selection of reviewed wording; never free factual generation."""
from dataclasses import dataclass
from app.ai.models import ReplyRequest
from app.ai.safety import approved_reply, claim_violations


@dataclass(frozen=True)
class Composition:
    source_en: str
    operator_text: str
    viewer_text: str | None
    template_id: str | None


def template_text(request: ReplyRequest, template_id: str, fact_ids: tuple[str, ...], language: str) -> str | None:
    template = next((t for t in request.knowledge.reply_templates if t.id == template_id), None)
    if (template is None or not fact_ids or len(fact_ids) != len(set(fact_ids))
            or set(template.fact_ids) != set(fact_ids)):
        return None
    # Editing a source requires reviewing templates, including translations.
    for lang, sources in template.source_texts.items():
        actual = {id: request.knowledge.find_fact(id).localized(lang)
                  for id in template.fact_ids if request.knowledge.find_fact(id)}
        if sources != actual or any(not v or claim_violations(v) for v in actual.values()):
            return None
    return template.texts.get(language)


def compose(request: ReplyRequest, kind: str, fact_ids: tuple[str, ...], language: str) -> Composition | None:
    if kind == "facts" and (not fact_ids or len(fact_ids) != len(set(fact_ids))
                            or not all(request.knowledge.find_fact(id) for id in fact_ids)):
        return None
    operator = request.project.ai.operator_language
    template_id = None
    if kind == "facts" and request.project.ai.prefer_single_sentence:
        matches = sorted((t for t in request.knowledge.reply_templates
                          if set(t.fact_ids) == set(fact_ids)), key=lambda t: t.id)
        if matches:
            template_id = matches[0].id
    def text(lang):
        if template_id:
            return template_text(request, template_id, fact_ids, lang)
        return approved_reply(request, kind, fact_ids, lang)
    source, translated, viewer = text("en"), text(operator), text(language)
    if not source or not translated:
        return None
    return Composition(source, translated, viewer, template_id)


def expected_text(request, draft, language):
    if draft.template_id:
        if draft.response_kind != "facts":
            return None
        return template_text(request, draft.template_id, draft.fact_ids, language)
    return approved_reply(request, draft.response_kind, draft.fact_ids, language)
