"""CLI and future FastAPI backend foundation."""
import argparse
import asyncio
import logging
from typing import get_args
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import ValidationError
import yaml

from app.ai.factory import create_processor
from app.config import ReplayScenarioName, Settings
from app.live.models import Comment, LiveState
from app.live.service import LiveService
from app.logging_config import LOGGER_NAME, configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        config = settings or Settings()
        configure_logging(config)
        processor = create_processor(config)
        service = LiveService(config, on_comment=processor.handle)
        application.state.comment_processor = processor
        application.state.live_service = service
        task = asyncio.create_task(service.run(), name="live-service")
        application.state.live_task = task
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await processor.aclose()

    application = FastAPI(title="TikTok LIVE Assistant", lifespan=lifespan)

    @application.get("/health")
    async def health():
        service = application.state.live_service
        task = application.state.live_task
        failed = task.done() and not task.cancelled() and task.exception() is not None
        return {
            "status": "error" if failed else "ok",
            "listener_running": not task.done(),
            "live": service.status.model_dump(mode="json"),
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


async def run_ai_smoke(settings: Settings, text: str) -> int:
    """Explicit operator action: one OpenAI request, no LIVE service constructed."""
    processor = create_processor(settings, ai_smoke=True)
    try:
        suggestion = await processor.process(Comment(username="ai_smoke", text=text))
        logging.getLogger(LOGGER_NAME).info("ai_smoke_result", extra={"fields": {
            "suggestion": suggestion.model_dump(mode="json"),
        }})
        return 2 if suggestion.status == "needs_operator" else 0
    finally:
        await processor.aclose()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Read-only TikTok LIVE listener")
    parser.add_argument("--mode", choices=("real", "fake", "replay"))
    parser.add_argument("--scenario", choices=get_args(ReplayScenarioName), help="Offline replay fixture")
    parser.add_argument("--username", help="Your own TikTok handle")
    parser.add_argument("--diagnostic", action="store_true", default=None,
                        help="Manual real LIVE smoke test with FakeAIReplyEngine")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--ai-smoke", metavar="COMMENT", help="One explicit OpenAI test without TikTok")
    args = parser.parse_args()
    overrides = {}
    if args.ai_smoke is not None:
        if args.mode or args.username or args.scenario or args.diagnostic:
            parser.error("--ai-smoke cannot be combined with LIVE/replay options")
        overrides.update(live_mode="fake", diagnostic=False, replay_scenario=None,
                         tiktok_username="", fake_repeat=False)
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
        code = asyncio.run(run_ai_smoke(settings, args.ai_smoke) if args.ai_smoke is not None
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
