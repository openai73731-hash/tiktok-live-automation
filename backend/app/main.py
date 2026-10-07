"""CLI and future FastAPI backend foundation."""
import argparse
import asyncio
import logging
from typing import get_args
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from pydantic import ValidationError
import yaml

from app.ai.factory import create_processor
from app.ai.knowledge import ProjectConfig, load_yaml_model
from app.config import ReplayScenarioName, Settings
from app.live.models import Comment, LiveState
from app.live.service import LiveService
from app.logging_config import LOGGER_NAME, configure_logging
from app.admin.events import AdminEventBus
from app.admin.router import ASSETS, router as admin_router
from app.admin.security import LocalAdminMiddleware, SECURITY_HEADERS
from app.admin.service import AdminService, PlaygroundService
from app.admin.runtime import OperatorRuntime
from app.quality.router import router as quality_router
from app.quality.service import QualityService
from app.delivery.service import ManualDeliveryService
from app.lifecycle import finish_owned


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        config = settings or Settings()
        configure_logging(config)
        playground_config = config.model_copy(update={
            "live_mode": "fake", "diagnostic": False, "replay_scenario": None,
            "tiktok_username": "",
        })
        events = AdminEventBus()
        playground = PlaygroundService(create_processor(playground_config), events)
        admin = AdminService(config, playground, events)
        application.state.admin = admin
        application.state.playground_service = playground
        runtime = OperatorRuntime(application, config, admin, playground_config, LiveService)
        admin.runtime = runtime
        playground.on_busy_change = runtime.publish
        application.state.operator_runtime = runtime
        if runtime.project != playground.processor.project:
            await playground.processor.aclose()
            playground.processor = create_processor(playground_config, project=runtime.project)
            from app.admin.models import AIStatus
            ai = runtime.project.ai
            admin.ai_status = AIStatus(provider=ai.provider, model=ai.model,
                state="available" if ai.provider == "fake" else "not_checked",
                available=True if ai.provider == "fake" else None)
        quality = QualityService(playground, live_enabled=config.admin_live_enabled)
        application.state.quality = quality
        delivery = ManualDeliveryService(config, admin.suggestions, admin.delivery_context, serial=runtime.android_serial)
        application.state.delivery = admin.delivery = delivery
        delivery.on_idle = runtime.publish
        runtime.live._sync()
        try:
            if config.admin_live_enabled and not runtime.config_error:
                await runtime.live.connect(runtime.username or ("demo" if config.live_mode == "fake" else ""), startup=True)
            yield
        finally:
            runtime.closed = True
            async def shutdown():
                try:
                    await delivery.aclose()
                finally:
                    try:
                        await runtime.live.stop_current()
                    finally:
                        try: await application.state.quality.aclose()
                        finally: await admin.aclose()
            await finish_owned(asyncio.create_task(shutdown(), name="owned-admin-shutdown"))

    application = FastAPI(title="TikTok LIVE Assistant", lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)
    application.add_middleware(LocalAdminMiddleware)
    application.include_router(admin_router)
    application.include_router(quality_router)
    application.mount("/ui/assets", StaticFiles(directory=ASSETS), name="admin-assets")

    @application.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        # Pydantic's default errors echo input; browser responses must not.
        return JSONResponse({"error": "invalid_input", "detail": "Проверьте текст и параметры запроса."}, status_code=422)

    @application.exception_handler(Exception)
    async def safe_error(request: Request, exc: Exception):
        logging.getLogger(LOGGER_NAME).error("admin_request_failed", extra={
            "fields": {"error_type": type(exc).__name__},
        })
        return JSONResponse({"error": "operation_failed", "detail": "Операция не выполнена. Требуется оператор."},
                            status_code=500, headers=SECURITY_HEADERS)

    @application.get("/health")
    async def health():
        service = application.state.live_service
        task = application.state.live_task
        failed = task is not None and task.done() and not task.cancelled() and task.exception() is not None
        return {
            "status": "error" if failed else "ok",
            "listener_running": task is not None and not task.done(),
            "live": (service.status if service is not None else application.state.admin.live_status).model_dump(mode="json"),
        }

    return application


app = create_app()


async def run_listener(settings: Settings) -> int:
    if settings.live_mode == "replay":
        from app.live.replay import run_replay
        result = await run_replay(settings)
        return 0 if result["passed"] else 1
    processor = create_processor(settings)
    service = LiveService(settings, on_comment=processor.handle)
    try:
        await service.run()
    finally:
        await processor.aclose()
    return 2 if service.status.terminal_state == LiveState.OPERATOR_REQUIRED else 0


async def run_ai_smoke(settings: Settings, text: str, provider: str | None = None) -> int:
    """Explicit operator action: one comment, no LIVE service constructed."""
    processor = create_processor(settings, ai_smoke=provider is None, smoke_provider=provider)
    try:
        suggestion = await processor.process(Comment(username="ai_smoke", text=text))
        logging.getLogger(LOGGER_NAME).info("ai_smoke_result", extra={"fields": {
            "suggestion": suggestion.model_dump(mode="json"),
        }})
        return 2 if suggestion.status == "needs_operator" else 0
    finally:
        await processor.aclose()


async def list_ai_models(settings: Settings) -> int:
    from app.ai.lmstudio_provider import LMStudioReplyEngine, LMStudioError
    project = load_yaml_model(settings.project_config_path, ProjectConfig)
    engine = LMStudioReplyEngine(project.ai)
    try:
        models = await engine.check_model()
        logging.getLogger(LOGGER_NAME).info("ai_models", extra={"fields": {
            "provider": engine.name, "model": engine.model, "models": models, "status": "ready",
        }})
        return 0
    except LMStudioError as exc:
        logging.getLogger(LOGGER_NAME).error("ai_models_failed", extra={"fields": {
            "provider": engine.name, "model": engine.model, "error_category": exc.category,
            "status": "needs_operator", "hint": "Start LM Studio and load the configured model",
        }})
        return 2
    finally:
        await engine.aclose()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Read-only TikTok LIVE listener")
    parser.add_argument("--mode", choices=("real", "fake", "replay"))
    parser.add_argument("--scenario", choices=get_args(ReplayScenarioName), help="Offline replay fixture")
    parser.add_argument("--username", help="Your own TikTok handle")
    parser.add_argument("--diagnostic", action="store_true", default=None,
                        help="Manual real LIVE smoke test with FakeAIReplyEngine")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--ai-smoke", metavar="COMMENT", help="One explicit OpenAI test without TikTok")
    parser.add_argument("--ai-smoke-provider", choices=("fake", "openai", "lmstudio"))
    parser.add_argument("--list-ai-models", choices=("lmstudio",))
    parser.add_argument("--project-config", help="YAML configuration path")
    parser.add_argument("comment", nargs="?", help="Comment for --ai-smoke-provider")
    args = parser.parse_args()
    overrides = {}
    smoke_text = args.ai_smoke if args.ai_smoke is not None else args.comment
    if args.ai_smoke_provider and (args.ai_smoke is not None or args.comment is None):
        parser.error("--ai-smoke-provider requires a positional COMMENT, without --ai-smoke")
    if args.comment is not None and not args.ai_smoke_provider:
        parser.error("A positional COMMENT requires --ai-smoke-provider")
    if args.list_ai_models and (args.ai_smoke is not None or args.ai_smoke_provider):
        parser.error("Model discovery and AI smoke are separate commands")
    if smoke_text is not None or args.list_ai_models:
        if args.mode or args.username or args.scenario or args.diagnostic:
            parser.error("AI diagnostics cannot be combined with LIVE/replay options")
        overrides.update(live_mode="fake", diagnostic=False, replay_scenario=None,
                         tiktok_username="", fake_repeat=False)
    if args.project_config:
        overrides["project_config_path"] = args.project_config
    if args.scenario:
        overrides["replay_scenario"] = args.scenario
    if args.diagnostic is not None:
        overrides["diagnostic"] = args.diagnostic
    if args.mode:
        overrides["live_mode"] = args.mode
    if args.username:
        overrides["tiktok_username"] = args.username
    try:
        settings = Settings(_env_file=args.env_file, **overrides)
    except ValidationError as exc:
        # Do not dump environment inputs or potential secrets on errors.
        details = "; ".join(
            f"{'.'.join(map(str, item['loc'])) or 'settings'}: {item['msg']}"
            for item in exc.errors(include_input=False, include_url=False)
        )
        parser.error(details)
    configure_logging(settings)
    try:
        if args.list_ai_models:
            code = asyncio.run(list_ai_models(settings))
        elif args.ai_smoke_provider:
            code = asyncio.run(run_ai_smoke(settings, smoke_text, args.ai_smoke_provider))
        else:
            code = asyncio.run(run_ai_smoke(settings, smoke_text) if smoke_text is not None
                               else run_listener(settings))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logging.getLogger(LOGGER_NAME).error("configuration_failed", extra={
            "fields": {"error_type": type(exc).__name__,
                       "hint": "Check config paths and YAML schemas"},
        })
        code = 2
    except KeyboardInterrupt:
        logging.getLogger(LOGGER_NAME).info("application_stopped")
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    cli()
