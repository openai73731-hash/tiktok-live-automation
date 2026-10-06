"""Select comments, isolate provider failures and return operator suggestions."""
import asyncio
import logging
import re
from time import perf_counter

from app.ai.engine import AIReplyEngine, ProviderNotConfiguredError
from app.ai.errors import safe_error
from app.ai.knowledge import KnowledgeBase, ProjectConfig
from app.ai.models import ReplyDraft, ReplyRequest, ReplySuggestion
from app.ai.safety import ReplySafetyPolicy
from app.live.models import Comment
from app.logging_config import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)


def is_reply_candidate(text: str) -> bool:
    text = text.casefold()
    return bool(re.search(
        r"[?？]|^\s*(is|are|does|do|can|will)\b|\b(how|what|why|where|when|help|explain|"
        r"please|tell me)\b|\b(как|что|почему|где|когда|можно|"
        r"помоги\w*|объясни\w*)\b|\b(cómo|qué|por qué|ayuda)\b|"
        r"not working|doesn't work|не работает|ошибка|no funciona",
        text,
    ))


class CommentProcessor:
    def __init__(
        self, engine: AIReplyEngine, project: ProjectConfig,
        knowledge: KnowledgeBase, *, safety: ReplySafetyPolicy | None = None,
    ) -> None:
        self.engine = engine
        self.project = project
        self.knowledge = knowledge
        self.safety = safety or ReplySafetyPolicy()

    async def process(self, comment: Comment) -> ReplySuggestion:
        started = perf_counter()
        candidate = is_reply_candidate(comment.text)
        base = {
            "username": comment.username, "original_text": comment.text,
            "provider": self.engine.name, "detected_language": "und",
            "model": getattr(self.engine, "model", None),
            "operator_translation": None,
        }

        def abstain(reason: str, status: str = "needs_operator", *, processing_error=None, processing_error_detail=None) -> ReplySuggestion:
            return ReplySuggestion(
                **base, should_reply=False, suggested_reply=None,
                reply_translation=None, reason=reason, status=status, processing_error=processing_error,
                processing_error_detail=processing_error_detail,
            )

        if len(comment.text) > self.project.ai.max_comment_chars:
            return abstain("Сообщение слишком длинное для автоматической подготовки ответа.")
        try:
            request = ReplyRequest(
                original_text=comment.text, is_candidate=candidate,
                project=self.project, knowledge=self.knowledge,
            )
            async with asyncio.timeout(self.project.ai.timeout_seconds):
                from app.ai.social import draft_for
                raw = draft_for(request)
                if raw is None:
                    raw = await self.engine.generate(request)
            # Validate the boundary even if a future provider returns a dict.
            draft = ReplyDraft.model_validate(raw)
        except Exception as exc:
            category, detail = safe_error(exc)
            logger.error("ai_provider_failed", extra={"fields": {
                "provider": self.engine.name, "model": base["model"],
                "error_type": type(exc).__name__,
                "error_category": category, "error_detail": detail,
                "duration_ms": round((perf_counter() - started) * 1000), "status": "needs_operator",
            }})
            if isinstance(exc, ProviderNotConfiguredError):
                return abstain("Provider не настроен: задайте его API key в окружении.", processing_error="provider_error")
            return abstain("Не удалось подготовить ответ; требуется оператор.",
                           processing_error=category, processing_error_detail=detail)
        base.update(
            detected_language=draft.detected_language,
            operator_translation=draft.operator_translation,
            intent=draft.response_kind,
        )
        if draft.needs_operator or draft.detected_language == "und" or not draft.operator_translation:
            return abstain("Нет надёжного перевода или знания для этого сообщения; требуется оператор.")
        if not (candidate if draft.should_reply is None else draft.should_reply):
            return abstain("Приветствие или сообщение без запроса ответа.", status="skipped")
        if draft.response_kind == "none":
            return abstain("Сообщение похоже на вопрос, но движок не подготовил ответ.")
        issues = self.safety.violations(request, draft)
        if issues:
            logger.warning("reply_blocked", extra={"fields": {
                "provider": self.engine.name, "rules": list(issues),
            }})
            return abstain("Ответ не прошёл проверку фактов, языка или ограничений; требуется оператор.")
        reason = (
            "Короткий социальный ответ из утверждённого банка; требуется подтверждение."
            if draft.response_kind == "social" else
            "Вопрос о результате торговли; ответ без финансовых обещаний."
            if draft.response_kind == "no_promises" else
            "Нет подтверждённых сведений; предложен честный ответ об ограничении знаний."
            if draft.response_kind == "unknown" else
            "Вопрос о программе; ответ основан на утверждённых фактах."
        )
        if draft.translation_evidence is not None:
            reason += " Машинный перевод ответа требует проверки оператором."
        return ReplySuggestion(
            **base, should_reply=True, suggested_reply=draft.suggested_reply,
            reply_translation=draft.reply_translation, fact_ids=draft.fact_ids,
            reason=reason, status="ready",
            template_id=draft.template_id,
            social_id=draft.social_id,
            translation_source="constrained" if draft.translation_evidence else "approved",
            translation_requires_review=draft.translation_evidence is not None,
        )

    async def handle(self, comment: Comment) -> ReplySuggestion:
        suggestion = await self.process(comment)
        logger.info("reply_suggestion", extra={"fields": {
            "suggestion": suggestion.model_dump(mode="json"),
        }})
        return suggestion

    async def aclose(self) -> None:
        try:
            async with asyncio.timeout(5):
                await self.engine.aclose()
        except Exception as exc:
            logger.error("ai_provider_cleanup_failed", extra={"fields": {
                "provider": self.engine.name, "model": getattr(self.engine, "model", None),
                "error_type": type(exc).__name__,
            }})
