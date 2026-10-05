"""Offline demo engine with explicit, finite language/translation fixtures."""
import re
import unicodedata
from pathlib import Path

from pydantic import Field

from app.ai.knowledge import ConfigModel, load_yaml_model
from app.ai.models import ReplyDraft, ReplyRequest, ResponseKind
from app.ai.safety import approved_reply


def normalize(text: str) -> str:
    # Case/space/punctuation variants only; never guess the meaning of new text.
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\s.!?¿¡]+", " ", text).strip()


class FakeScenario(ConfigModel):
    language: str = Field(pattern=r"^[a-z]{2,3}$")
    comments: tuple[str, ...]
    translations: dict[str, str]
    response_kind: ResponseKind
    fact_ids: tuple[str, ...] = ()


class FakeExamples(ConfigModel):
    scenarios: tuple[FakeScenario, ...]


class FakeAIReplyEngine:
    name = "fake"

    def __init__(self, examples: FakeExamples) -> None:
        self.scenarios: dict[str, FakeScenario] = {}
        for scenario in examples.scenarios:
            for text in scenario.comments:
                key = normalize(text)
                if not key or key in self.scenarios:
                    raise ValueError("Empty or ambiguous fake scenario")
                self.scenarios[key] = scenario

    @classmethod
    def from_file(cls, path: Path) -> "FakeAIReplyEngine":
        return cls(load_yaml_model(path, FakeExamples))

    async def generate(self, request: ReplyRequest) -> ReplyDraft:
        scenario = self.scenarios.get(normalize(request.original_text))
        if scenario is None:
            return ReplyDraft(detected_language="und", needs_operator=True)
        operator_language = request.project.ai.operator_language
        translation = (
            request.original_text if scenario.language == operator_language
            else scenario.translations.get(operator_language)
        )
        if not request.is_candidate or scenario.response_kind == "none":
            return ReplyDraft(
                detected_language=scenario.language, operator_translation=translation,
                needs_operator=not bool(translation),
            )
        # Each configured fact is a short sentence. Validation is still applied
        # after generation and catches longer/unsafe knowledge or translations.
        ids = scenario.fact_ids[:request.project.ai.max_reply_sentences]
        reply = approved_reply(request, scenario.response_kind, ids, scenario.language)
        reply_ru = approved_reply(request, scenario.response_kind, ids, operator_language)
        return ReplyDraft(
            detected_language=scenario.language, operator_translation=translation,
            response_kind=scenario.response_kind, suggested_reply=reply,
            reply_translation=reply_ru, reply_language=scenario.language,
            fact_ids=ids, needs_operator=not all((translation, reply, reply_ru)),
        )

    async def aclose(self) -> None:
        pass
