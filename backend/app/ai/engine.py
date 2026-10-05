"""Plug-in boundary for a future LLM provider."""
from typing import Protocol

from app.ai.models import ReplyDraft, ReplyRequest


class AIReplyEngine(Protocol):
    name: str

    async def generate(self, request: ReplyRequest) -> ReplyDraft:
        """Treat original_text as untrusted data. Return an unapproved draft."""
        ...


class ProviderNotConfiguredError(RuntimeError):
    pass

