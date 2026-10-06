"""Explicit operator actions, one owned LIVE session and controlled AI replacement."""
import asyncio
import logging

from app.admin.models import AIStatus, OperatorView
from app.admin.operator_config import LocalConfigError, OperatorOverlay, OperatorStore, TikTokOverride
from app.ai.factory import create_processor
from app.ai.knowledge import ProjectConfig, load_yaml_model
from app.live.models import LiveState, LiveStatus
from app.logging_config import LOGGER_NAME
from app.quality.service import QualityService

logger = logging.getLogger(LOGGER_NAME)


class RuntimeConflict(RuntimeError):
    """Messages are static application text, never provider exceptions."""


class AdminLiveController:
    def __init__(self, runtime, factory):
        self.runtime = runtime
        self.factory = factory
        self.service = self.task = self.processor = None
        self.processor_closed = False

    def _sync(self):
        app = self.runtime.application
        app.state.live_service = self.service
        app.state.live_task = self.task
        app.state.comment_processor = self.processor
        self.runtime.admin.live_service = self.service

    async def connect(self, username, *, startup=False):
        async with self.runtime.lock:
            self.runtime.ensure_idle()
            username = TikTokOverride(username=username).username
            if not username:
                raise ValueError("Own TikTok username required")
            if self.runtime.config_error:
                raise RuntimeConflict("Исправьте или сбросьте локальные настройки перед подключением.")
            if self.runtime.pending:
                raise RuntimeConflict("Сначала примените сохранённые настройки.")
            if self.runtime.project.ai.provider == "openai" and not startup:
                raise RuntimeConflict("Для LIVE выберите и примените Fake или LM Studio; платные запросы не разрешены.")
            config = self.runtime.settings.model_copy(update={"tiktok_username": username,
                **({} if startup else {"live_mode": "real", "diagnostic": False, "replay_scenario": None})})
            processor = create_processor(config, project=self.runtime.project)
            async def handle(comment):
                return await self.runtime.admin.handle_live(comment, processor, mode=config.live_mode)
            try:
                service = self.factory(config, on_comment=handle, on_status=self.runtime.admin.on_live_state)
            except BaseException:
                await processor.aclose()
                raise
            self.processor, self.service = processor, service
            self.processor_closed = False
            self.runtime.admin.live_status = LiveStatus(username=username, mode=config.live_mode, state=LiveState.CONNECTING)
            self.runtime.application.state.quality.set_live_enabled(True)
            self.task = asyncio.create_task(self._run(service, processor), name="admin-live-service")
            self._sync()
            self.runtime.admin.events.publish("live_state", self.runtime.admin.live_view())
        self.runtime.publish()
        return self.runtime.admin.live_view()

    async def _run(self, service, processor):
        try:
            await service.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("admin_live_failed", extra={"fields": {"error_type": type(exc).__name__}})
            status = self.runtime.admin.live_status.model_copy(update={
                "state": LiveState.STOPPED, "terminal_state": LiveState.OPERATOR_REQUIRED, "reason": None})
            self.runtime.admin.on_live_state(status)
        finally:
            await processor.aclose()
            self.processor_closed = True
            self.runtime.publish()

    async def disconnect(self):
        async with self.runtime.lock:
            if self.task is not None:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            # Handles cancellation before _run has entered its try/finally.
            if self.processor is not None and not self.processor_closed:
                await self.processor.aclose()
                self.processor_closed = True
            if self.service is not None:
                previous = self.runtime.admin.live_status
                self.runtime.admin.on_live_state(previous.model_copy(update={"state": LiveState.STOPPED, "reason": None}))
            self.service = self.task = self.processor = None
            self._sync()
            self.runtime.application.state.quality.set_live_enabled(False)
            self.runtime.admin.events.publish("live_state", self.runtime.admin.live_view())
        self.runtime.publish()
        return self.runtime.admin.live_view()


class OperatorRuntime:
    def __init__(self, application, settings, admin, playground_config, factory):
        self.application, self.settings, self.admin = application, settings, admin
        self.playground_config = playground_config
        self.base = load_yaml_model(settings.project_config_path, ProjectConfig)
        # Fixed basename beside the startup project config; no browser path field.
        self.store = OperatorStore(self.base, settings.project_config_path.parent / "operator.local.yaml")
        self.config_error = None
        try:
            self.saved = self.store.load()
        except LocalConfigError:
            self.saved = OperatorOverlay()
            self.config_error = "invalid_local_config"
            logger.error("operator_config_invalid", extra={"fields": {"error_type": "LocalConfigError"}})
        self.project = self.store.validate(self.saved)
        self.username = self.saved.tiktok.username if self.saved.tiktok.username is not None else settings.tiktok_username
        self.lock = asyncio.Lock()
        self.applying = False
        self.closed = False
        self.live = AdminLiveController(self, factory)

    @property
    def pending(self):
        username = self.saved.tiktok.username if self.saved.tiktok.username is not None else self.settings.tiktok_username
        return self.store.validate(self.saved) != self.project or username != self.username

    def lock_reason(self):
        if self.live.service is not None:
            return "Отключите LIVE перед изменением или применением настроек."
        if self.admin.playground.lock.locked() or self.admin.check_task is not None or self.applying:
            return "Дождитесь завершения Playground, Quality Lab или проверки AI."
        return None

    def ensure_idle(self):
        if self.closed:
            raise RuntimeConflict("Панель завершает работу.")
        reason = self.lock_reason()
        if reason:
            raise RuntimeConflict(reason)

    def view(self):
        reason = self.lock_reason()
        return OperatorView(saved=self.saved, active=self.admin.public_settings(),
            desired=self.admin.public_settings(self.store.validate(self.saved)), username=self.username,
            saved_username=self.saved.tiktok.username if self.saved.tiktok.username is not None else self.settings.tiktok_username,
            pending=self.pending, locked=reason is not None, lock_reason=reason, config_error=self.config_error)

    def publish(self):
        if not self.closed:
            self.admin.events.publish("operator_settings", self.view())

    async def save(self, overlay):
        async with self.lock:
            self.ensure_idle()
            self.store.save(overlay)
            self.saved = overlay
            self.config_error = None
        self.publish()
        return self.view()

    async def reset(self):
        async with self.lock:
            self.ensure_idle()
            self.store.reset()
            self.saved = OperatorOverlay()
            self.config_error = None
        self.publish()
        return self.view()

    async def apply(self):
        async with self.lock:
            self.ensure_idle()
            if self.config_error:
                raise RuntimeConflict("Исправьте или сбросьте локальные настройки.")
            playground = self.admin.playground
            await playground.lock.acquire()
            self.applying = True
            processor = None
            try:
                project = self.store.validate(self.saved)
                processor = create_processor(self.playground_config, project=project)
                # Validate replacement Quality Lab before disposing the active client.
                from app.admin.service import PlaygroundService
                candidate = QualityService(PlaygroundService(processor, self.admin.events))
                await self.application.state.quality.aclose()
                await playground.processor.aclose()
                playground.processor = processor
                candidate.playground = playground
                self.application.state.quality = candidate
                self.project = project
                self.username = self.saved.tiktok.username if self.saved.tiktok.username is not None else self.settings.tiktok_username
                self.admin.ai_status = AIStatus(provider=project.ai.provider, model=project.ai.model,
                    state="available" if project.ai.provider == "fake" else "not_checked",
                    available=True if project.ai.provider == "fake" else None)
                self.admin.last_checked = 0
            except BaseException:
                if processor is not None and playground.processor is not processor:
                    await processor.aclose()
                raise
            finally:
                self.applying = False
                playground.lock.release()
        self.admin.events.publish("ai_status", self.admin.ai_status)
        self.application.state.quality.set_live_enabled(False)
        self.publish()
        return self.view()
