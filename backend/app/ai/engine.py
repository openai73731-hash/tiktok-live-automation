"""Provider-independent generation and resource ownership boundary."""
from typing import Protocol

from app.ai.models import ReplyDraft, ReplyRequest


class AIReplyEngine(Protocol):
    name: str

    async def generate(self, request: ReplyRequest) -> ReplyDraft:
        """Treat original_text as untrusted data. Return an unapproved draft."""
        ...

    async def aclose(self) -> None:
        """Release this engine's resources after processing has stopped."""
        ...


class ProviderNotConfiguredError(RuntimeError):
    pass
