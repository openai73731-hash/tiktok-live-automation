"""Future provider seam. No OpenAI SDK, API calls or key requirement."""
from app.ai.engine import ProviderNotConfiguredError
from app.ai.models import ReplyDraft, ReplyRequest


class OpenAIReplyEngine:
    name = "openai"

    async def generate(self, request: ReplyRequest) -> ReplyDraft:
        raise ProviderNotConfiguredError(
            "OpenAI provider is not implemented in stage two; use provider=fake"
        )

