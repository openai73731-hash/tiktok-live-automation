"""Finite reviewed small-talk bank; no model output or unknown-question fallback."""
from functools import lru_cache
from pathlib import Path
import re
import unicodedata

from pydantic import Field, model_validator

from app.ai.knowledge import ConfigModel, load_yaml_model
from app.ai.models import ReplyDraft

# Like project/knowledge config, this reviewed file belongs to the workspace,
# not to a parent of site-packages in an installed wheel.
BANK_PATH = Path("config/social_replies.yaml")


def normalize(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\s.!?¿¡]+", " ", text).strip()


class SocialEntry(ConfigModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    language: str = Field(pattern=r"^(en|ru|es)$")
    comments: dict[str, str]
    reply: str = Field(min_length=1, max_length=260)
    reply_ru: str = Field(min_length=1, max_length=260)


class SocialBank(ConfigModel):
    entries: tuple[SocialEntry, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unambiguous(self):
        aliases = [normalize(text) for entry in self.entries for text in entry.comments]
        ids = [entry.id for entry in self.entries]
        if len(set(aliases)) != len(aliases) or len(set(ids)) != len(ids) or "" in aliases:
            raise ValueError("Social bank must be unambiguous")
        return self


@lru_cache(maxsize=1)
def bank():
    return load_yaml_model(BANK_PATH, SocialBank)


def match(text):
    key = normalize(text)
    for entry in bank().entries:
        for comment, translation in entry.comments.items():
            if normalize(comment) == key:
                return entry, translation
    return None


def draft_for(request):
    if request.project.ai.reply_policy != "friendly" or request.project.ai.operator_language != "ru":
        return None
    found = match(request.original_text)
    if found is None:
        return None
    entry, translation = found
    return ReplyDraft(detected_language=entry.language, should_reply=True, response_kind="social",
        operator_translation=request.original_text if entry.language == "ru" else translation,
        suggested_reply=entry.reply, reply_translation=entry.reply_ru, reply_language=entry.language,
        social_id=entry.id)


def expected_social(request, draft, language):
    found = match(request.original_text)
    if (request.project.ai.reply_policy != "friendly" or request.project.ai.operator_language != "ru"
            or not found or draft.fact_ids or draft.template_id or draft.translation_evidence
            or draft.composition_style != "canonical"):
        return None
    entry, _ = found
    if draft.social_id != entry.id or draft.detected_language != entry.language:
        return None
    return entry.reply if language == entry.language else entry.reply_ru if language == "ru" else None
