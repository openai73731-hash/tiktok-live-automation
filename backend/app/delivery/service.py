"""Manual fill and separately armed AUTO, with owned work and lazy persistent dedup."""
import asyncio
from types import SimpleNamespace
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4
from typing import Literal

from app.android.adb import AdbTransport
from app.android.controller import ADBAndroidController
from app.android.models import DeviceInspection
from app.delivery.models import (FrozenModel, AndroidTarget, DeliveryResult, DryRunView, DeliveryReason, EligibilityReason)
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
    fill_ready: bool = False
    mobile_one_shot: Literal["DISABLED", "AVAILABLE", "SPENT"] = "DISABLED"
    mode: Literal["MANUAL", "AUTOMATIC"] = "MANUAL"
    auto_state: Literal["AUTO OFF", "AUTO ARMED", "AUTO BLOCKED", "AUTO SENDING", "AUTO PAUSED"] = "AUTO OFF"
    auto_reason: EligibilityReason | DeliveryReason | Literal["queue_full"] = "automatic_mode_required"
    workflow: Literal["READY", "WAITING FOR PHONE INPUT", "FILLING", "FILLED — SEND ON PHONE",
        "AUTO_RESERVED", "AUTO SENDING", "POST_SEND_VERIFY", "SENT CONFIRMED", "AUTO BLOCKED", "UNKNOWN"] = "READY"
    live_session_id: str | None = None
    active_runtime_revision: str = "unavailable"
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
    mode: Literal["MANUAL", "AUTOMATIC", "AUTO"] = "MANUAL"
    result: Literal["filled", "confirmed", "not_sent", "unknown", "needs_operator"]
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
        self.mode = "MANUAL"
        self.armed_binding = None
        self.auto_queue = asyncio.Queue(maxsize=10)
        self.auto_runner = None
        self.workflow = "READY"
        self.auto_reason = "automatic_mode_required"
        self.pending_fill = None
        self.acceptance_spent = False
        self.controller.on_stage = self._stage

    def _stage(self, stage):
        self.workflow = stage
        self.on_idle()

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

    def consumed(self, envelope):
        from app.delivery.registry import consumed
        try:
            return bool(self.journal and self.journal.has_envelope_operation(envelope)) or consumed(self.settings.state_dir, envelope)
        except Exception: return True

    @property
    def cooldown(self):
        return monotonic() - self.last_attempt < (self.settings.automatic_delivery_min_interval_seconds if self.mode == "AUTOMATIC" else self.settings.manual_delivery_min_interval_seconds)

    def view(self):
        inspected = self.controller.last
        ready = (not self.closed and not self.paused and not self.cooldown and getattr(self.controller, "current_ready", inspected.ready) and
                 self.context().device_available and self.settings.enable_physical_tiktok_send and self.mode == "AUTOMATIC")
        reason = "delivery_paused" if self.closed or self.paused else "cooldown" if self.cooldown else (
            "physical_delivery_disabled" if not self.settings.enable_physical_tiktok_send else "ready" if ready else "device_unavailable")
        auto_ctx = self.auto_context()
        auto_reason = self.auto_reason
        sending = self.workflow in {"AUTO_RESERVED", "AUTO SENDING", "POST_SEND_VERIFY"}
        if self.mode == "AUTOMATIC" and not self.paused and not sending:
            if not auto_ctx.delivery_available: auto_reason = "delivery_unavailable"
            elif not auto_ctx.device_available: auto_reason = "device_unavailable"
            elif self.cooldown: auto_reason = "cooldown"
        return DeliveryView(instance_id=self.settings.instance_id, account_label=self.settings.account_label, inspection=inspected, physical_delivery_enabled=self.settings.enable_physical_tiktok_send,
            physical_ready=ready, fill_ready=inspected.fill_ready,
            mobile_one_shot="DISABLED" if not self.settings.mobile_acceptance_one_shot else "SPENT" if self.acceptance_spent else "AVAILABLE",
            delivery="DRY RUN READY" if inspected.ready else "NOT READY",
            paused=self.paused, busy=self.busy, reason=reason, mode=self.mode,
            auto_state="AUTO OFF" if self.mode == "MANUAL" else "AUTO PAUSED" if self.paused else
                "AUTO SENDING" if sending else
                "AUTO ARMED" if auto_ctx.delivery_available and auto_ctx.device_available and auto_reason in {"eligible", "feed_confirmed"} else "AUTO BLOCKED",
            auto_reason=auto_reason, workflow=self.workflow, live_session_id=self.context().live_session_id,
            active_runtime_revision=self.context().active_runtime_revision)

    def require_discovered(self, serial):
        if serial is not None and serial not in {d.serial for d in self.controller.last.devices}:
            raise SuggestionConflict("Выберите устройство из последнего списка backend.")

    def set_serial(self, serial):
        self.controller.serial = serial
        if hasattr(self.controller, "invalidate_trust"): self.controller.invalidate_trust()
        self.controller.last = DeviceInspection(reason="not_checked")

    def revoke_authority(self):
        self.mode, self.armed_binding = "MANUAL", None
        self.auto_reason = "automatic_mode_required"
        if self.suggestions is not None: self.suggestions.auto_authorizations.clear()
        while not self.auto_queue.empty():
            self.auto_queue.get_nowait(); self.auto_queue.task_done()
        if self.auto_runner is not None and not self.auto_runner.done(): self.auto_runner.cancel()
        if hasattr(self.controller, "invalidate_trust"): self.controller.invalidate_trust()

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
                inspected = await self.controller.inspect(target)
                if inspected.security_state:
                    self.paused, self.auto_reason = True, "security_ui"
                return self.view()
        return await self._owned(run)

    async def capture(self):
        async def run():
            async with self.lock:
                return await self.controller.capture()
        return await self._owned(run)

    def prepare(self, *, fill=False):
        if self.closed or (not fill and not self.settings.enable_physical_tiktok_send): return self.view()
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

    def fill_context(self):
        # Input preparation has its own profile/lease/focus checks in the controller.
        # It is independent of the startup SEND gate and of Send-control confirmation.
        ctx = self.context()
        return ctx.model_copy(update={"delivery_available": self.mode == "MANUAL" and not self.closed,
            "device_available": True, "operator_paused": ctx.operator_paused or self.paused})

    def auto_context(self, *, confirming=False):
        ctx = self.context()
        bound = self.armed_binding == (ctx.live_session_id, ctx.target_account, ctx.active_runtime_revision)
        return ctx.model_copy(update={"delivery_available": bool(self.mode == "AUTOMATIC" and bound
            and self.settings.enable_physical_tiktok_send and not self.closed),
            # After the ONE activation, normal LIVE intentionally has no composer.
            # Only the read-only confirmation phase may omit input readiness; the
            # controller still verifies exact device/account/profile/normal LIVE.
            "device_available": ctx.device_available or confirming,
            "operator_paused": ctx.operator_paused or self.paused})

    def auto_verdict(self, envelope):
        from app.delivery.automatic import AutomaticEligibility
        try:
            duplicate = self.consumed(envelope)
        except Exception:
            self.paused = True
            duplicate = True
        return AutomaticEligibility.evaluate(envelope, self.auto_context(), duplicate=duplicate)

    def can_fill(self, review):
        from app.delivery.suggestions import DeliveryEligibility
        if self.mode != "MANUAL" or self.closed or self.paused or self.busy or self.pending_fill: return False
        e = review.envelope
        if not DeliveryEligibility.evaluate(e, review.approval, self.fill_context()).eligible: return False
        if not self.controller.actions or not self.controller.actions.supports(e.suggested_reply): return False
        try: return not self.consumed(e)
        except Exception: return False

    def set_mode(self, mode, expected_session, expected_runtime):
        ctx = self.context()
        if expected_session != ctx.live_session_id or expected_runtime != ctx.active_runtime_revision:
            raise SuggestionConflict("Mode binding conflict")
        if mode == "MANUAL":
            self.mode, self.armed_binding = "MANUAL", None
            self.auto_reason = "automatic_mode_required"
            while not self.auto_queue.empty():
                self.auto_queue.get_nowait(); self.auto_queue.task_done()
            if self.auto_runner is not None and not self.auto_runner.done(): self.auto_runner.cancel()
            self.on_idle()
            return self.view()
        if (self.busy or self.closed or self.auto_runner is not None and not self.auto_runner.done()
                or self.settings.mobile_acceptance_one_shot and self.acceptance_spent
                or not self.settings.enable_physical_tiktok_send
                or not ctx.connected or not ctx.active or not ctx.device_available or self.pending_fill):
            raise SuggestionConflict("Automatic mode unavailable")
        self.prepare()
        if self.paused: raise SuggestionConflict("Delivery paused; do not retry an unknown operation")
        self.mode = "AUTOMATIC"
        self.armed_binding = (ctx.live_session_id, ctx.target_account, ctx.active_runtime_revision)
        self.auto_reason = "eligible"
        self.on_idle()
        return self.view()

    def context_changed(self):
        if self.mode == "AUTOMATIC" and (not self.auto_context().delivery_available or not self.context().connected):
            self.paused, self.auto_reason = True, "live_session_mismatch"

    def offer_auto(self, suggestion_id):
        # Called ONLY for a fresh real LIVE completion, never GET/SSE/reconnect or old history.
        if self.closed or self.mode != "AUTOMATIC" or self.paused: return
        e = self.suggestions.get(suggestion_id)
        verdict = self.auto_verdict(e)
        self.auto_reason = verdict.reason
        if not verdict.eligible: return
        if self.auto_queue.full():
            self.auto_reason = "queue_full"
            return
        self.auto_queue.put_nowait(suggestion_id)
        if self.auto_runner is None or self.auto_runner.done():
            self.auto_runner = asyncio.create_task(self._run_auto(), name="owned-auto-delivery")
            self.tasks.add(self.auto_runner)
            def complete(task):
                self.tasks.discard(task)
                if not task.cancelled(): task.exception()
                self.on_idle()
            self.auto_runner.add_done_callback(complete)

    async def _run_auto(self):
        try:
            while not self.auto_queue.empty() and self.mode == "AUTOMATIC" and not self.closed and not self.paused:
                suggestion_id = self.auto_queue.get_nowait()
                try:
                    delay = self.settings.automatic_delivery_min_interval_seconds - (monotonic() - self.last_attempt)
                    if delay > 0: await asyncio.sleep(delay)
                    e = self.suggestions.get(suggestion_id)
                    verdict = self.auto_verdict(e)
                    self.auto_reason = verdict.reason
                    if not verdict.eligible: continue
                    authorization = self.suggestions.authorize_auto(suggestion_id, self.auto_context())
                    body = SimpleNamespace(suggestion_id=e.suggestion_id, expected_suggestion_revision=e.suggestion_revision,
                        approval_id=authorization.approval_id, idempotency_key="auto_" + e.suggestion_id)
                    await self.send(body)
                except SuggestionConflict:
                    self.auto_reason = "auto_ineligible"
                except Exception:
                    self.paused, self.auto_reason = True, "sender_failed"
                finally:
                    self.auto_queue.task_done()
        finally:
            # Disarming or pausing discards pending candidates; it never retries history.
            while not self.auto_queue.empty():
                self.auto_queue.get_nowait(); self.auto_queue.task_done()

    async def fill(self, body):
        async def run():
            async with self.lock:
                if self.mode != "MANUAL" or self.paused or self.closed:
                    return DeliveryResult(status="needs_operator", reason="delivery_paused")
                if self.pending_fill: return DeliveryResult(status="not_sent", reason="waiting_user_send")
                e, _, verdict = self.suggestions.authoritative(body.suggestion_id, body.approval_id,
                    body.expected_suggestion_revision, self.fill_context())
                if not verdict.eligible: raise SuggestionConflict("Fill binding rejected")
                if not self.controller.actions or not self.controller.actions.supports(e.suggested_reply):
                    return DeliveryResult(status="needs_operator", reason="unicode_entry_unavailable")
                target = AndroidTarget(live_session_id=e.live_session_id, account=e.target_account,
                    device_serial=self.controller.serial or self.controller.last.serial)
                state = await self.controller.inspect(target)
                # Opening the composer is an operator action. No reservation is spent while waiting.
                if state.security_state:
                    self.paused = True
                    return DeliveryResult(status="needs_operator", reason="security_ui")
                if state.screen.value == "TRANSIENT_OVERLAY":
                    self.workflow = "WAITING FOR PHONE INPUT"
                    return DeliveryResult(status="not_sent", reason="transient_overlay")
                if not state.fill_ready:
                    self.workflow = "WAITING FOR PHONE INPUT"
                    reason = "open_phone_composer" if state.live_verified and state.account_verified else "unknown_screen"
                    return DeliveryResult(status="needs_operator", reason=reason)
                self.prepare(fill=True)
                if self.paused or self.journal is None: return DeliveryResult(status="needs_operator", reason="delivery_paused")
                service = self
                class FillSender:
                    async def send(self, request):
                        from app.delivery.models import AndroidSubmission
                        outcome = await service.controller.fill(AndroidSubmission(operation_id=request.operation_id,
                            target=target, text=request.text, expires_at=request.expires_at),
                            guard=lambda: coordinator.preflight(request) and service.mode == "MANUAL")
                        return DeliveryResult.model_validate(outcome.model_dump())
                coordinator = DeliveryCoordinator(self.suggestions, self.journal, FillSender(), self.fill_context)
                self.coordinator = coordinator
                before = getattr(self.controller, "_host_snapshot", None)
                self.workflow = "FILLING"
                self.on_idle()
                try:
                    result = await coordinator.execute(body.suggestion_id, body.approval_id,
                        body.expected_suggestion_revision, body.idempotency_key, timeout=60, device_serial=target.device_serial)
                    if result.status == "filled":
                        self.workflow = "FILLED — SEND ON PHONE"
                        self.pending_fill = (body, target, before, e.suggested_reply, coordinator.operation_id)
                    else:
                        self.workflow = "UNKNOWN" if result.status == "unknown" else "WAITING FOR PHONE INPUT"
                        if result.status in {"unknown", "needs_operator"}: self.paused = True
                    return result
                except JournalConflict:
                    return DeliveryResult(status="needs_operator", reason="rejected")
                finally:
                    self.paused = self.paused or coordinator.failed
                    await coordinator.aclose()
        return await self._owned(run)

    async def verify_fill(self):
        async def run():
            async with self.lock:
                if not self.pending_fill: raise SuggestionConflict("No pending fill")
                body, target, before, reply, operation_id = self.pending_fill
                def guard():
                    try:
                        _, _, verdict = self.suggestions.authoritative(body.suggestion_id, body.approval_id,
                            body.expected_suggestion_revision, self.fill_context())
                        return verdict.eligible
                    except SuggestionConflict: return False
                cancelled = False
                result = DeliveryResult(status="unknown", reason="interrupted")
                try:
                    async with self.controller.lock:
                        confirmed = await self.controller.verify_post_send(target, before, reply, guard)
                    result = DeliveryResult(status="confirmed" if confirmed else "unknown",
                        reason="feed_confirmed" if confirmed else "interrupted")
                except asyncio.CancelledError:
                    cancelled = True
                    result = DeliveryResult(status="unknown", reason="cancelled")
                except Exception:
                    result = DeliveryResult(status="unknown", reason="sender_failed")
                try: self.journal.settle_fill(operation_id, result)
                except Exception:
                    self.paused = True
                    result = DeliveryResult(status="unknown", reason="journal_unavailable")
                self.pending_fill = None
                self.workflow = "SENT CONFIRMED" if result.status == "confirmed" else "UNKNOWN"
                if result.status == "unknown": self.paused = True
                if cancelled: raise asyncio.CancelledError
                return result
        return await self._owned(run)

    async def send(self, body):
        if not self.settings.enable_physical_tiktok_send:
            return DeliveryResult(status="not_sent", reason="physical_delivery_disabled")
        if self.mode != "AUTOMATIC":
            return DeliveryResult(status="not_sent", reason="automatic_mode_required")
        async def run():
            async with self.lock:
                self.prepare()
                if self.journal is not None:
                    try:
                        previous = self.journal.lookup(body.idempotency_key)
                        if previous and previous.suggestion_id == body.suggestion_id and previous.approval_id == body.approval_id and previous.suggestion_revision == body.expected_suggestion_revision:
                            return DeliveryResult(status="unknown", reason="interrupted") if previous.status == "reserved" else DeliveryResult(status=previous.status, reason=previous.reason)
                    except Exception: self.paused = True
                if self.paused or self.closed or self.journal is None:
                    return DeliveryResult(status="needs_operator", reason="delivery_paused")
                if self.settings.mobile_acceptance_one_shot and self.acceptance_spent:
                    return DeliveryResult(status="not_sent", reason="auto_ineligible")
                delay = self.settings.automatic_delivery_min_interval_seconds
                if monotonic() - self.last_attempt < delay: return DeliveryResult(status="not_sent", reason="cooldown")
                e = self.suggestions.get(body.suggestion_id)
                verdict = self.auto_verdict(e)
                if not verdict.eligible:
                    self.auto_reason = verdict.reason
                    return DeliveryResult(status="not_sent", reason="auto_ineligible")
                sender = AndroidTikTokReplySender(self.controller, physical_enabled=True)
                coordinator = DeliveryCoordinator(self.suggestions, self.journal, sender,
                    lambda: self.auto_context(confirming=self.workflow == "POST_SEND_VERIFY"),
                    mode="AUTOMATIC", authority=self.suggestions.automatic_authoritative)
                self.coordinator = coordinator
                sender.preflight = lambda request: (coordinator.preflight(request) and
                    request.device_serial == (self.controller.serial or self.controller.last.serial))
                try:
                    self.last_attempt = monotonic()
                    if self.settings.mobile_acceptance_one_shot: self.acceptance_spent = True
                    self.workflow = "AUTO_RESERVED"
                    self.on_idle()
                    result = await coordinator.execute(body.suggestion_id, body.approval_id,
                        body.expected_suggestion_revision, body.idempotency_key, timeout=60,
                        device_serial=self.controller.serial or self.controller.last.serial)
                    if result.status in {"unknown", "needs_operator"}: self.paused = True
                    self.workflow = "SENT CONFIRMED" if result.status == "confirmed" else "UNKNOWN" if result.status == "unknown" else "AUTO BLOCKED"
                    self.auto_reason = result.reason
                    return result
                except JournalConflict:
                    self.paused = True
                    return DeliveryResult(status="needs_operator", reason="rejected")
                finally:
                    self.paused = self.paused or coordinator.failed
                    await coordinator.aclose()
                    if self.settings.mobile_acceptance_one_shot and self.acceptance_spent:
                        self.mode, self.armed_binding = "MANUAL", None
                        while not self.auto_queue.empty():
                            self.auto_queue.get_nowait(); self.auto_queue.task_done()
        return await self._owned(run)

    def recent(self):
        if self.journal is None: return tuple(self.history)
        try:
            return tuple(DeliveryHistoryEntry(operation_id=o.operation_id, suggestion_id=o.suggestion_id,
                time=o.created_at, username=o.target_account, device=o.device_serial,
                mode=o.mode, result="unknown" if o.status == "reserved" else o.status,
                reason="interrupted" if o.status == "reserved" else o.reason)
                for o in self.journal.recent())
        except Exception:
            self.paused = True
            return tuple(self.history)

    async def aclose(self):
        self.closed = True
        self.revoke_authority()
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
