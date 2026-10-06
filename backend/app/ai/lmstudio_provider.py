"""Local-only async analyzer and constrained translator. No OpenAI SDK or keys."""
import json
import re

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.ai.analysis import AIAnalysis
from app.ai.lmstudio_prompts import PROMPT_HASH, TRANSLATION_PROMPT_HASH, TRANSLATION_RULES, analysis_prompt
from app.ai.errors import ERROR_CATEGORIES, ERROR_DETAILS, validation_detail
from app.ai.composer import compose
from app.ai.knowledge import AIConfig, local_base_url
from app.ai.models import ReplyDraft, ReplyRequest, TranslationEvidence
from app.ai.safety import claim_violations, style_violations, sentence_count


class LMStudioError(RuntimeError):
    def __init__(self, category: str, detail: str | None = None):
        if category not in ERROR_CATEGORIES or detail is not None and detail not in ERROR_DETAILS:
            raise ValueError("Unrecognized provider failure metadata")
        self.category = category
        self.detail = detail
        super().__init__(category)  # Only predefined categories, never remote details.


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str = Field(min_length=1, max_length=150, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$")


class ModelList(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    data: list[ModelInfo] = Field(max_length=1000)


class ConstrainedTranslation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_language: str = Field(pattern=r"^[a-z]{2,3}$")
    viewer_language_reply: str = Field(min_length=1, max_length=400)
    operator_reply_translation_ru: str = Field(min_length=1, max_length=400)
    back_translation_en: str = Field(min_length=1, max_length=400)


class LMStudioReplyEngine:
    name = "lmstudio"
    analysis_prompt_hash = PROMPT_HASH
    translation_prompt_hash = TRANSLATION_PROMPT_HASH

    def __init__(self, config: AIConfig, *, transport=None):
        self.config = config
        self.model = config.model
        self._base_url = local_base_url(config.lmstudio_base_url)
        self._transport = transport  # Offline HTTP tests, no runtime URL overrides.
        self._client = None
        self._closed = False
        self._model_checked = False
        self.generation_count = 0  # POST attempts only; never request/response bodies.

    def _get_client(self):
        if self._closed:
            raise LMStudioError("client_closed")
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url + "/", timeout=self.config.timeout_seconds,
                follow_redirects=False, trust_env=False, transport=self._transport,
            )
        return self._client

    async def _request(self, method, path, **kwargs):
        try:
            response = await self._get_client().request(method, path, **kwargs)
            if response.is_redirect:
                raise LMStudioError("redirect_rejected")
            if response.status_code in {400, 404}:
                raise LMStudioError("model_or_request_error")
            if response.status_code != 200:
                raise LMStudioError("http_error")
            if len(response.content) > 100_000:
                raise LMStudioError("invalid_output", "response_too_large")
            return response.json()
        except httpx.TimeoutException:
            raise LMStudioError("timeout") from None
        except httpx.TransportError:
            raise LMStudioError("connection_error") from None
        except (ValueError, TypeError):
            raise LMStudioError("invalid_output", "response_json_invalid") from None

    async def list_models(self) -> list[str]:
        try:
            models = ModelList.model_validate(await self._request("GET", "models"))
        except ValueError:
            raise LMStudioError("invalid_models_response") from None
        return [model.id for model in models.data]

    async def check_model(self) -> list[str]:
        models = await self.list_models()
        if self.model not in models:
            raise LMStudioError("model_not_found")
        self._model_checked = True
        return models

    async def _structured(self, schema, system, user):
        json_schema = schema.model_json_schema()
        # Some local runtimes do not expose response_format to the model's prompt.
        # Make the same contract explicit; client validation remains mandatory.
        system += "\nRequired JSON output contract (all required fields, no aliases or extra keys): " + json.dumps(
            json_schema, ensure_ascii=False, separators=(",", ":"),
        )
        if not self.config.thinking_enabled:
            system += "\n/no_think"
        body = {
            "model": self.model, "temperature": self.config.temperature,
            "max_tokens": self.config.max_model_output_tokens, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "strict": True, "schema": json_schema,
            }},
        }
        try:
            self.generation_count += 1
            raw = await self._request("POST", "chat/completions", json=body)
            choices = raw["choices"]
            if not isinstance(choices, list):
                raise LMStudioError("invalid_output", "response_shape_invalid")
            if len(choices) != 1:
                raise LMStudioError("incomplete_output", "choice_count_invalid")
            if choices[0]["finish_reason"] != "stop":
                raise LMStudioError("incomplete_output", "non_stop_finish")
            message = choices[0]["message"]
            if not isinstance(message, dict):
                raise LMStudioError("invalid_output", "response_shape_invalid")
            if message.get("refusal"):
                raise LMStudioError("invalid_output", "refusal")
            if message.get("tool_calls") or message.get("function_call"):
                raise LMStudioError("invalid_output", "tool_call")
            # Local runtimes can expose an empty reasoning channel as whitespace.
            # Accept an empty channel, never actual thinking text or mixed content.
            if any(value.strip() if isinstance(value, str) else value
                   for value in (message.get("reasoning_content"), message.get("reasoning"))):
                raise LMStudioError("invalid_output", "thinking_output")
            content = message["content"]
            if content is None or content == "" or isinstance(content, str) and not content.strip():
                raise LMStudioError("invalid_output", "empty_content")
            if not isinstance(content, str):
                raise LMStudioError("invalid_output", "response_shape_invalid")
            if content.lstrip().startswith(("<think>", "<analysis>")):
                raise LMStudioError("invalid_output", "thinking_output")
            if content.lstrip().startswith(("```", "~~~")):
                raise LMStudioError("invalid_output", "markdown_output")
            # Parse the complete content only. No markdown/JSON extraction fallback.
            return schema.model_validate_json(content)
        except ValidationError as exc:
            raise LMStudioError("invalid_output", validation_detail(exc)) from None
        except (KeyError, TypeError, ValueError):
            raise LMStudioError("invalid_output", "response_shape_invalid") from None

    async def generate(self, request: ReplyRequest) -> ReplyDraft:
        if len(request.original_text) > self.config.max_comment_chars:
            return ReplyDraft(detected_language="und", needs_operator=True)
        if not self._model_checked:
            await self.check_model()
        system = analysis_prompt(request.knowledge)
        result = await self._structured(AIAnalysis, system, request.original_text)
        language, translation, ids = result.detected_language, result.operator_translation, tuple(result.fact_ids)
        if len(set(ids)) != len(ids):
            raise LMStudioError("invalid_output", "duplicate_fact_id")
        if any(request.knowledge.find_fact(id) is None for id in ids):
            raise LMStudioError("invalid_output", "unknown_fact_id")
        base = dict(detected_language=language, operator_translation=translation,
                    should_reply=result.should_reply, response_kind=result.intent, fact_ids=ids)
        if language != "und" and not translation:
            raise LMStudioError("invalid_output", "translation_missing")
        if language != "und" and not re.search(r"[А-Яа-яЁё]", translation or ""):
            raise LMStudioError("invalid_output", "translation_invalid")
        if not translation:
            # Never label an untranslated/English result as a Russian translation.
            return ReplyDraft(**{**base, "operator_translation": None}, needs_operator=True)
        if language == "und" or result.reason == "unsupported":
            return ReplyDraft(**base, needs_operator=True)
        if not result.should_reply:
            return ReplyDraft(**base)
        composition = compose(request, result.intent, ids, language)
        if composition is None:
            return ReplyDraft(**base, needs_operator=True)
        viewer = composition.viewer_text
        evidence = None
        if viewer is None:
            if self.config.operator_language != "ru":
                return ReplyDraft(**base, needs_operator=True)
            system = TRANSLATION_RULES
            data = json.dumps({"source_en": composition.source_en, "target_language": language,
                               "approved_ru": composition.operator_text}, ensure_ascii=False)
            translated = await self._structured(ConstrainedTranslation, system, data)
            failures = (
                (translated.target_language != language, "translation_language_mismatch"),
                (translated.back_translation_en != composition.source_en, "translation_source_mismatch"),
                (translated.operator_reply_translation_ru != composition.operator_text, "translation_operator_mismatch"),
                (translated.viewer_language_reply in {composition.source_en, composition.operator_text}, "translation_untranslated"),
                (claim_violations(translated.viewer_language_reply), "translation_claim"),
                (style_violations(translated.viewer_language_reply), "translation_style"),
                (len(translated.viewer_language_reply) > self.config.max_reply_chars, "translation_length"),
                (not 1 <= sentence_count(translated.viewer_language_reply) <= self.config.max_reply_sentences, "translation_sentence_limit"),
            )
            for failed, detail in failures:
                if failed:
                    raise LMStudioError("translation_rejected", detail)
            viewer = translated.viewer_language_reply
            evidence = TranslationEvidence(source_en=composition.source_en, target_language=language,
                                          viewer_language_reply=viewer,
                                          back_translation_en=translated.back_translation_en,
                                          operator_reply_translation_ru=composition.operator_text)
        return ReplyDraft(**base, suggested_reply=viewer, reply_translation=composition.operator_text,
                          reply_language=language, template_id=composition.template_id,
                          composition_style="constrained_translation" if evidence else "approved_template",
                          translation_evidence=evidence)

    async def aclose(self):
        if not self._closed:
            self._closed = True
            if self._client:
                await self._client.aclose()
