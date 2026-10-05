"""Compose the AI pipeline without importing a TikTok SDK or LLM SDK."""
from app.ai.fake_engine import FakeAIReplyEngine
from app.ai.engine import AIReplyEngine
from app.ai.knowledge import KnowledgeBase, ProjectConfig, load_yaml_model
from app.ai.openai_provider import OpenAIReplyEngine
from app.ai.processor import CommentProcessor
from app.config import Settings


def create_engine(settings: Settings, project: ProjectConfig) -> AIReplyEngine:
    if project.ai.provider == "fake":
        return FakeAIReplyEngine.from_file(settings.fake_examples_path)
    if project.ai.provider == "openai":
        return OpenAIReplyEngine(project.ai, api_key=settings.openai_api_key)
    raise ValueError("Unsupported AI provider")


def create_processor(settings: Settings, *, ai_smoke: bool = False) -> CommentProcessor:
    project = load_yaml_model(settings.project_config_path, ProjectConfig)
    if ai_smoke or settings.diagnostic or settings.live_mode == "replay":
        # Stage 3A isolates the LIVE integration from provider selection.
        project = project.model_copy(update={
            "ai": project.ai.model_copy(update={"provider": "openai" if ai_smoke else "fake"}),
        })
    knowledge = load_yaml_model(settings.knowledge_path, KnowledgeBase)
    engine = create_engine(settings, project)
    return CommentProcessor(engine, project, knowledge)
