"""Compose the AI pipeline without importing a TikTok SDK or LLM SDK."""
from app.ai.fake_engine import FakeAIReplyEngine
from app.ai.knowledge import KnowledgeBase, ProjectConfig, load_yaml_model
from app.ai.openai_provider import OpenAIReplyEngine
from app.ai.processor import CommentProcessor
from app.config import Settings


def create_processor(settings: Settings) -> CommentProcessor:
    project = load_yaml_model(settings.project_config_path, ProjectConfig)
    if settings.diagnostic:
        # Stage 3A isolates the LIVE integration from provider selection.
        project = project.model_copy(update={
            "ai": project.ai.model_copy(update={"provider": "fake"}),
        })
    knowledge = load_yaml_model(settings.knowledge_path, KnowledgeBase)
    engine = (
        FakeAIReplyEngine.from_file(settings.fake_examples_path)
        if project.ai.provider == "fake" else OpenAIReplyEngine()
    )
    return CommentProcessor(engine, project, knowledge)
