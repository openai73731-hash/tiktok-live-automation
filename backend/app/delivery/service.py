"""Local explicit manual operations, lazy journal, inspection-only diagnostics."""
import asyncio
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4
from typing import Literal

from app.android.adb import AdbTransport
from app.android.controller import ADBAndroidController
from app.android.models import DeviceInspection
from app.delivery.models import (FrozenModel, AndroidTarget, DeliveryResult, DryRunView, DeliveryReason)
from app.delivery.coordinator import DeliveryCoordinator
from app.delivery.journal import SQLiteDeliveryOperationStore, JournalConflict, JOURNAL_PATH
from app.delivery.sender import AndroidTikTokReplySender
from app.delivery.suggestions import SuggestionConflict
from app.lifecycle import finish_owned
from app.windows.sender import WindowsSenderStatus


class DeliveryView(FrozenModel):
    transport: Literal["android", "windows_live_studio"] = "android"
    instance_id: str = "default"
    account_label: str = ""
    windows_status: Literal["probe_required", "operator_arm_required"] | None = None
    windows_sender: WindowsSenderStatus | None = None
    delivery_uncertain: bool = False
    inspection: DeviceInspection | None = None
    physical_delivery_enabled: bool
    physical_ready: bool
    delivery: Literal["NOT READY", "DRY RUN READY", "FILL READY"]
    paused: bool
    busy: bool
    reason: Literal["physical_delivery_disabled", "device_unavailable", "ready", "delivery_paused", "cooldown", "not_configured"]


class DeliveryHistoryEntry(FrozenModel):
    operation_id: str
    suggestion_id: str
    time: str
    username: str
    device: str | None
    mode: Literal["MANUAL", "AUTO"] = "MANUAL"
    result: Literal["confirmed", "not_sent", "unknown", "needs_operator"]
    reason: DeliveryReason | None


class ManualDeliveryService:
    def __init__(self, settings, suggestions, context, *, serial=None, controller=None, journal_factory=None):
        self.settings, self.suggestions, self.context = settings, suggestions, context
        self.controller = controller or ADBAndroidController(AdbTransport(settings.adb_executable),
            serial=serial, physical_enabled=settings.enable_physical_tiktok_send)
        self.journal_factory = journal_factory or (lambda: SQLiteDeliveryOperationStore((settings.state_dir / JOURNAL_PATH.name).resolve()))
        self.journal = self.coordinator = None
        self.tasks = set()
        self.lock = asyncio.Lock()
        self.closed = self.paused = False
        self.last_attempt = float("-inf")
        self.history = deque(maxlen=50)
        self.on_idle = lambda: None

    @property
    def busy(self): return self.lock.locked() or self.controller.lock.locked()

    @property
    def delivery_available(self): return self.settings.enable_physical_tiktok_send

    def device_ready(self, context):
        controller = self.controller
        return (controller.ready_for(context.live_session_id, context.target_account)
                if hasattr(controller, "ready_for") else controller.last.ready)

    def text_supported(self, text):
        actions = getattr(self.controller, "actions", None)
        return bool(text and actions and actions.supports(text))

    @property
    def cooldown(self):
        return monotonic() - self.last_attempt < self.settings.manual_delivery_min_interval_seconds

    def view(self):
        inspected = self.controller.last
        ready = (not self.closed and not self.paused and not self.cooldown and getattr(self.controller, "current_ready", inspected.ready) and
                 self.context().device_available and self.settings.enable_physical_tiktok_send)
        reason = "delivery_paused" if self.closed or self.paused else "cooldown" if self.cooldown else (
            "physical_delivery_disabled" if not self.settings.enable_physical_tiktok_send else "ready" if ready else "device_unavailable")
        return DeliveryView(instance_id=self.settings.instance_id, account_label=self.settings.account_label,
            inspection=inspected, physical_delivery_enabled=self.settings.enable_physical_tiktok_send,
            physical_ready=ready, delivery="DRY RUN READY" if inspected.ready else "NOT READY",
            paused=self.paused, busy=self.busy, reason=reason)

    def require_discovered(self, serial):
        if serial is not None and serial not in {d.serial for d in self.controller.last.devices}:
            raise SuggestionConflict("Выберите устройство из последнего списка backend.")

    def set_serial(self, serial):
        self.controller.serial = serial
        if hasattr(self.controller, "invalidate_trust"): self.controller.invalidate_trust()
        self.controller.last = DeviceInspection(reason="not_checked")

    def can_send(self, review):
        if not review.eligibility.eligible or not self.view().physical_ready or self.busy: return False
        actions = getattr(self.controller, "actions", None)
        if actions is None or not actions.supports(review.envelope.suggested_reply): return False
        try:
            return not self.journal or not self.journal.has_envelope_operation(review.envelope)
        except Exception:
            return False

    async def trust_action(self, *, token=None, verify=False):
        async def run():
            async with self.lock:
                ctx = self.context()
                account = ctx.target_account
                if not account: raise SuggestionConflict("Сначала укажите аккаунт собственного LIVE.")
                target = AndroidTarget(live_session_id=ctx.live_session_id or "0" * 32,
                    account=account, device_serial=self.controller.serial)
                valid = await self.controller.verify_account(target) if verify else await self.controller.confirm_send(target, token)
                if not valid: raise SuggestionConflict("Проверка не выполнена. Проверьте Profile, LIVE и reviewed composer.")
                return self.view()
        return await self._owned(run)

    async def _owned(self, action):
        if self.closed: raise SuggestionConflict("Delivery завершает работу.")
        if len(self.tasks) >= 8: raise SuggestionConflict("Delivery занята.")
        task = asyncio.create_task(action(), name="admin-manual-operation")
        self.tasks.add(task)
        try:
            return await task
        finally:
            async def settle():
                try:
                    if not task.done(): task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                finally: self.tasks.discard(task)
            try:
                await finish_owned(asyncio.create_task(settle(), name="admin-delivery-cleanup"))
            finally:
                # A browser may have observed locked=True during inspection/capture.
                # Publish again after owned work releases its locks, even on failure/cancel.
                self.on_idle()

    async def inspect(self):
        async def run():
            async with self.lock:
                ctx = self.context()
                target = AndroidTarget(live_session_id=ctx.live_session_id or "0" * 32, account=ctx.target_account,
                    device_serial=self.controller.serial) if ctx.target_account else None
                await self.controller.inspect(target)
                return self.view()
        return await self._owned(run)

    async def capture(self):
        async def run():
            async with self.lock:
                return await self.controller.capture()
        return await self._owned(run)

    def prepare(self):
        if self.closed or not self.settings.enable_physical_tiktok_send: return self.view()
        if self.journal is None:
            try:
                self.journal = self.journal_factory()
                if self.journal.unresolved(): self.paused = True
            except Exception: self.paused = True
        return self.view()

    async def dry_run(self, body):
        async def run():
            async with self.lock:
                diagnostic_id = uuid4().hex
                def diagnostic_context():
                    return self.context().model_copy(update={"delivery_available": True, "device_available": True})
                try:
                    _, _, verdict = self.suggestions.authoritative(body.suggestion_id, body.approval_id,
                        body.expected_suggestion_revision, diagnostic_context())
                    if not verdict.eligible: return DryRunView(eligible=False, device_ready=False, would_send=False,
                        reason=verdict.reason, diagnostic_id=diagnostic_id)
                except SuggestionConflict:
                    return DryRunView(eligible=False, device_ready=False, would_send=False, reason="approval_stale", diagnostic_id=diagnostic_id)
                inspected = None
                service = self
                class InspectionOnlySender:
                    async def send(self, request):
                        nonlocal inspected
                        inspected = await service.controller.inspect(AndroidTarget(live_session_id=request.live_session_id,
                            account=request.target_account, device_serial=service.controller.serial))
                        return DeliveryResult(status="not_sent", reason="dry_run")
                # Same reservation code, isolated non-delivery database; never touches production dedup.
                journal = SQLiteDeliveryOperationStore(":memory:")
                coordinator = DeliveryCoordinator(self.suggestions, journal, InspectionOnlySender(), diagnostic_context)
                try:
                    await coordinator.execute(body.suggestion_id, body.approval_id, body.expected_suggestion_revision,
                        body.idempotency_key, timeout=45, device_serial=self.controller.serial)
                    _, _, final = self.suggestions.authoritative(body.suggestion_id, body.approval_id,
                        body.expected_suggestion_revision, diagnostic_context())
                    ready = bool(inspected and inspected.ready)
                    return DryRunView(eligible=final.eligible, device_ready=ready, would_send=final.eligible and ready,
                        reason=final.reason if not final.eligible else inspected.reason if inspected else "rejected",
                        diagnostic_id=diagnostic_id)
                except SuggestionConflict:
                    return DryRunView(eligible=False, device_ready=False, would_send=False, reason="approval_stale", diagnostic_id=diagnostic_id)
                finally:
                    await coordinator.aclose(); journal.close()
        return await self._owned(run)

    async def send(self, body):
        if not self.settings.enable_physical_tiktok_send:
            return DeliveryResult(status="not_sent", reason="physical_delivery_disabled")
        async def run():
            async with self.lock:
                self.prepare()
                if self.journal is not None:
                    try:
                        previous = self.journal.lookup(body.idempotency_key)
                        if previous and previous.suggestion_id == body.suggestion_id and previous.approval_id == body.approval_id and previous.suggestion_revision == body.expected_suggestion_revision:
                            return DeliveryResult(status="unknown", reason="interrupted") if previous.status == "reserved" else DeliveryResult(status=previous.status, reason=previous.reason)
                    except Exception:
                        self.paused = True
                if self.paused or self.closed or self.journal is None:
                    return DeliveryResult(status="needs_operator", reason="delivery_paused")
                if self.cooldown: return DeliveryResult(status="not_sent", reason="cooldown")
                if not self.controller.last.ready:
                    return DeliveryResult(status="needs_operator", reason="device_unavailable")
                sender = AndroidTikTokReplySender(self.controller, physical_enabled=True)
                coordinator = DeliveryCoordinator(self.suggestions, self.journal, sender, self.context)
                self.coordinator = coordinator
                sender.preflight = lambda request: (coordinator.preflight(request) and
                    request.device_serial == (self.controller.serial or self.controller.last.serial))
                try:
                    self.last_attempt = monotonic()
                    result = await coordinator.execute(body.suggestion_id, body.approval_id,
                        body.expected_suggestion_revision, body.idempotency_key, timeout=60,
                        device_serial=self.controller.serial or self.controller.last.serial)
                    if result.status in {"unknown", "needs_operator"}: self.paused = True
                    self.history.appendleft(DeliveryHistoryEntry(operation_id=coordinator.operation_id or uuid4().hex, suggestion_id=body.suggestion_id,
                        time=datetime.now(UTC).isoformat(), username=self.context().target_account or "", device=self.controller.serial,
                        result=result.status, reason=result.reason))
                    return result
                except JournalConflict:
                    self.paused = True
                    return DeliveryResult(status="needs_operator", reason="rejected")
                finally:
                    self.paused = self.paused or coordinator.failed
                    await coordinator.aclose()
        return await self._owned(run)

    def recent(self):
        if self.journal is None: return tuple(self.history)
        try:
            return tuple(DeliveryHistoryEntry(operation_id=o.operation_id, suggestion_id=o.suggestion_id,
                time=o.created_at, username=o.target_account, device=o.device_serial,
                result="unknown" if o.status == "reserved" else o.status,
                reason="interrupted" if o.status == "reserved" else o.reason)
                for o in self.journal.recent())
        except Exception:
            self.paused = True
            return tuple(self.history)

    async def aclose(self):
        self.closed = True
        await finish_owned(asyncio.create_task(self._close(), name="manual-delivery-shutdown"))

    async def _close(self):
        tasks = tuple(self.tasks)
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self.coordinator: await self.coordinator.aclose()
            await self.controller.aclose()
        finally:
            if self.journal is not None: self.journal.close()
