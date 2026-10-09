"""Explicit operator actions, one owned LIVE session and controlled AI replacement."""
import asyncio
import logging
from uuid import uuid4

from app.admin.models import AIStatus, OperatorView
from app.admin.operator_config import LocalConfigError, OperatorOverlay, OperatorStore, TikTokOverride
from app.ai.factory import create_processor
from app.ai.knowledge import ProjectConfig, load_yaml_model
from app.live.models import LiveState, LiveStatus
from app.logging_config import LOGGER_NAME
from app.lifecycle import finish_owned
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
        self.session_id = None
        self.last_session_id = None
        self.session_invalidated = False

    def _sync(self):
        app = self.runtime.application
        app.state.live_service = self.service
        app.state.live_task = self.task
        app.state.comment_processor = self.processor
        self.runtime.admin.live_service = self.service

    async def connect(self, username, *, startup=False):
        async with self.runtime.lock:
            username = TikTokOverride(username=username).username
            if not username:
                raise ValueError("Own TikTok username required")
            reason = self.runtime.connect_block_reason(username, startup=startup)
            if reason: raise RuntimeConflict(reason)
            config = self.runtime.settings.model_copy(update={"tiktok_username": username,
                **({} if startup else {"live_mode": "real", "diagnostic": False, "replay_scenario": None})})
            processor = create_processor(config, project=self.runtime.project)
            session_id = uuid4().hex
            async def handle(comment):
                if self.session_id != session_id:
                    return None
                return await self.runtime.admin.handle_live(comment, processor, mode=config.live_mode,
                    session_id=session_id, target_account=username)
            def on_status(status):
                if self.session_id == session_id:
                    self.runtime.admin.on_live_state(status)
            try:
                service = self.factory(config, on_comment=handle, on_status=on_status)
                def receipt(comment):
                    delivery = self.runtime.admin.delivery
                    if self.session_id == session_id and hasattr(delivery, "observe_comment"):
                        delivery.observe_comment(comment, session_id, username)
                service.on_receipt = receipt
            except BaseException:
                await processor.aclose()
                raise
            self.processor, self.service = processor, service
            self.session_id = self.last_session_id = session_id
            self.runtime.admin.begin_live_session(session_id)
            self.session_invalidated = False
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
                "state": LiveState.STOPPED, "terminal_state": LiveState.OPERATOR_REQUIRED, "reason": None,
                "cleanup_state": service.status.cleanup_state})
            self.runtime.admin.on_live_state(status)
        finally:
            async def close():
                await processor.aclose()
                self.processor_closed = True
                self.runtime.publish()
            await finish_owned(asyncio.create_task(close(), name="live-ai-cleanup"))

    async def disconnect(self, expected_live_session_id):
        async with self.runtime.lock:
            if expected_live_session_id != (self.session_id or self.last_session_id):
                raise RuntimeConflict("LIVE-сессия изменилась. Обновите состояние панели.")
            await finish_owned(asyncio.create_task(self._stop(), name="admin-live-stop"))
        self.runtime.publish()
        return self.runtime.admin.live_view()

    async def stop_current(self):
        """Lifespan-only stop, never exposed as an unversioned browser action."""
        async with self.runtime.lock:
            await finish_owned(asyncio.create_task(self._stop(), name="admin-live-shutdown"))

    async def _stop(self):
        self.session_invalidated = True
        # Revoke activity before the first cancellation/cleanup await.
        previous = self.runtime.admin.live_status
        if self.service is not None:
            self.runtime.admin.on_live_state(previous.model_copy(update={"state": LiveState.DISCONNECTED, "reason": None}))
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
        self.session_id = None
        self._sync()
        self.runtime.application.state.quality.set_live_enabled(False)
        self.runtime.admin.events.publish("live_state", self.runtime.admin.live_view())


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
        self.android_serial = self.saved.android.device_serial
        self.lock = asyncio.Lock()
        self.applying = False
        self.closed = False
        self.saved_config_revision = uuid4().hex
        self.active_runtime_revision = uuid4().hex
        self.live = AdminLiveController(self, factory)

    @property
    def pending(self):
        username = self.saved.tiktok.username if self.saved.tiktok.username is not None else self.settings.tiktok_username
        return (self.store.validate(self.saved) != self.project or username != self.username or
                self.saved.android.device_serial != self.android_serial)

    def lock_reason(self):
        if getattr(self.admin, "delivery", None) and self.admin.delivery.busy:
            return "Дождитесь завершения проверки устройства или delivery operation."
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

    def connect_block_reason(self, username=None, *, startup=False):
        # Listener input is independent of Android readiness/client/profile.
        if self.closed: return "Панель завершает работу."
        reason = self.lock_reason()
        if reason: return reason
        if self.config_error: return "Исправьте или сбросьте локальные настройки перед подключением."
        if self.admin.live_status.cleanup_state == "failed":
            return "Очистка LIVE не завершена. Требуется оператор и перезапуск backend."
        if self.pending: return "Сначала примените сохранённые настройки."
        if self.project.ai.provider == "openai" and not startup:
            return "Для LIVE выберите и примените Fake или LM Studio; платные запросы не разрешены."
        if not (self.username if username is None else username): return "Укажите и примените username собственного LIVE."
        return None

    def view(self):
        reason = self.lock_reason()
        connect_reason = self.connect_block_reason()
        return OperatorView(saved=self.saved, active=self.admin.public_settings(),
            desired=self.admin.public_settings(self.store.validate(self.saved)), username=self.username,
            saved_username=self.saved.tiktok.username if self.saved.tiktok.username is not None else self.settings.tiktok_username,
            pending=self.pending, locked=reason is not None, lock_reason=reason, config_error=self.config_error,
            saved_config_revision=self.saved_config_revision, active_runtime_revision=self.active_runtime_revision,
            active_device_serial=self.android_serial, connect_allowed=connect_reason is None,
            connect_block_reason=connect_reason)

    def publish(self):
        if not self.closed:
            self.admin.events.publish("operator_settings", self.view())

    def require_saved(self, revision):
        if revision != self.saved_config_revision:
            raise RuntimeConflict("Настройки изменены в другой вкладке. Обновите их перед сохранением.")

    async def save(self, overlay, expected_saved_revision):
        async with self.lock:
            self.require_saved(expected_saved_revision)
            self.ensure_idle()
            if getattr(self.admin, "delivery", None) and overlay.android.device_serial != self.saved.android.device_serial:
                self.admin.delivery.require_discovered(overlay.android.device_serial)
            self.store.save(overlay)
            self.saved = overlay
            self.config_error = None
            self.saved_config_revision = uuid4().hex
        self.publish()
        return self.view()

    async def reset(self, expected_saved_revision):
        async with self.lock:
            self.require_saved(expected_saved_revision)
            self.ensure_idle()
            self.store.reset()
            self.saved = OperatorOverlay()
            self.config_error = None
            self.saved_config_revision = uuid4().hex
        self.publish()
        return self.view()

    async def apply(self, expected_saved_revision, expected_active_runtime_revision):
        try:
            return await self._apply(expected_saved_revision, expected_active_runtime_revision)
        finally:
            # _apply has released both locks before observers see this snapshot.
            if not self.lock.locked() and not self.admin.playground.lock.locked():
                self.publish()

    async def _apply(self, expected_saved_revision, expected_active_runtime_revision):
        async with self.lock:
            self.require_saved(expected_saved_revision)
            if expected_active_runtime_revision != self.active_runtime_revision:
                raise RuntimeConflict("Активные настройки изменились. Обновите панель.")
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
                old_quality, old_processor = self.application.state.quality, playground.processor
                # No await between publication of all new active references.
                playground.processor = processor
                candidate.playground = playground
                self.application.state.quality = candidate
                self.project = project
                self.active_runtime_revision = uuid4().hex
                self.username = self.saved.tiktok.username if self.saved.tiktok.username is not None else self.settings.tiktok_username
                self.android_serial = self.saved.android.device_serial
                if getattr(self.admin, "delivery", None): self.admin.delivery.set_serial(self.android_serial)
                self.admin.ai_status = AIStatus(provider=project.ai.provider, model=project.ai.model,
                    state="available" if project.ai.provider == "fake" else "not_checked",
                    available=True if project.ai.provider == "fake" else None)
                self.admin.last_checked = 0
                candidate.set_live_enabled(False)
                self.admin.events.publish("ai_status", self.admin.ai_status)
                self.publish()
                async def retire():
                    try:
                        await old_quality.aclose()
                    finally:
                        await old_processor.aclose()
                await finish_owned(asyncio.create_task(retire(), name="retire-playground-runtime"))
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
