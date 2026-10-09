"""Operator-intent Windows transport and single-attempt keyboard delivery."""
import asyncio
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from time import monotonic
import threading
import unicodedata
from typing import Literal
from pydantic import Field

from app.delivery.models import DeliveryResult, FrozenModel
from app.windows.native_input import NativeInput, unicode_events
from app.windows.target import TargetRejected, focused, resolve


def normalized(text): return unicodedata.normalize('NFC', text).strip()


class WindowsSenderStatus(FrozenModel):
    stage: str = 'PC-SEND-2'
    state: Literal['DISARMED', 'ARMING', 'ARMED', 'FILLING', 'FILLED', 'SENDING', 'VERIFYING', 'NEED_OPERATOR', 'ERROR'] = 'DISARMED'
    discovery: str = 'operator_arm_required'
    prepare_available: bool = False
    submit_available: bool = False
    armed_age_seconds: int | None = None
    foreground_guard: bool = False
    auto_enabled: bool = False
    composer: Literal['UNKNOWN', 'EMPTY_ATTESTED', 'FILLED_UNVERIFIED'] = 'UNKNOWN'
    verification: Literal['UNAVAILABLE', 'AVAILABLE', 'WEAK', 'VERIFYING', 'VERIFIED', 'TIMEOUT'] = 'UNAVAILABLE'
    last_send_status: Literal['NONE', 'FILLED_ONLY', 'SENT_CONFIRMED', 'SEND_UNKNOWN', 'NOT_SENT'] = 'NONE'
    reason: str = 'disarmed'
    watch_seconds: float = 15
    empty_after_verified_confirmed: bool = False
    watch_ready: bool = False
    target_process_id: int | None = None
    target_instance: str | None = Field(default=None, max_length=96)


class WindowsLiveStudioReplySender:
    def __init__(self, settings=None, context=None, *, native=None, clock=monotonic):
        from app.config import Settings
        self.settings = settings or Settings(_env_file=None)
        self.config = self.settings.windows
        self.context = context or (lambda: None)
        self.native = native or NativeInput(self.config)
        self.clock = clock
        self.state, self.reason = 'DISARMED', 'disarmed'
        self.target = self.watch = self.monitor = None
        self.session = self.runtime = None
        self.account = None
        self.armed_at = None
        self.composer, self.last_send_status = 'UNKNOWN', 'NONE'
        self.verification = 'AVAILABLE' if self.config.broadcaster_user_id else 'UNAVAILABLE'
        self.auto_enabled = False
        self.accepted_target = self.confirmed_target = None
        self.confirmed_at = None
        self.invalid = threading.Event()
        self.focus_event = asyncio.Event()
        self.tasks = set()
        self.lock = asyncio.Lock()
        self.pending = None
        self.seen = OrderedDict()
        self.closed = False
        self.watch_ready = False
        self.cleanup_failed = False
        self.generation = 0
        self.preflight = lambda request: False
        self.operation_mode = 'fill'
        self.changed = lambda: None

    @property
    def empty_after_verified_confirmed(self):
        return self.accepted_target is not None

    def own(self, coroutine):
        task = asyncio.create_task(coroutine, name='windows-owned-operation')
        self.tasks.add(task)
        def done(value):
            self.tasks.discard(value)
            if not value.cancelled(): value.exception()
        task.add_done_callback(done)
        return task

    def invalidate(self, reason='disarmed', *, operator=False):
        self.invalid.set()
        self.generation += 1
        self.watch_ready = False
        self.target = None
        self.state = 'DISARMED' if operator else 'NEED_OPERATOR'
        self.reason, self.composer = reason, 'UNKNOWN'
        if reason != 'explicit_rearm': self.auto_enabled = False
        # An observed verified echo can be reviewed after switching to Admin.
        # It authorizes no input; fresh explicit ARM remains mandatory.
        if reason not in {'foreground_lost', 'ttl_expired', 'explicit_rearm'}:
            self.accepted_target = self.confirmed_target = None
        self.focus_event.set()
        if self.pending: self.pending['wake'].set()
        if self.watch:
            watch, self.watch = self.watch, None
            async def close():
                try: await watch.close()
                except BaseException:
                    self.cleanup_failed = True
                    self.state, self.reason = 'ERROR', 'watch_cleanup_failed'
            self.own(close())
        self.changed()

    def _context_ok(self):
        ctx = self.context()
        return bool(ctx and ctx.connected and ctx.active and ctx.live_session_id and
                    ctx.live_session_id == self.session and ctx.active_runtime_revision == self.runtime and ctx.target_account == self.account)

    def check(self):
        try:
            if self.closed or self.target is None or not self._context_ok():
                raise TargetRejected('live_or_runtime_changed')
            if self.armed_at is None or self.clock() - self.armed_at >= self.config.arm_ttl_seconds:
                raise TargetRejected('ttl_expired')
            snapshot = self.native.snapshot()
            if resolve(snapshot) != self.target: raise TargetRejected('target_changed')
            if self.invalid.is_set() or not focused(snapshot, self.target): raise TargetRejected('foreground_lost')
            return True
        except Exception as exc:
            reason = str(exc) if isinstance(exc, TargetRejected) else 'sender_exception'
            self.invalidate(reason)
            return False

    def _event(self, kind, generation=None):
        if generation is not None and generation != self.generation: return
        if kind in {'destroyed', 'failed'}:
            self.invalidate('target_destroyed' if kind == 'destroyed' else 'watch_failed')
        elif self.state == 'ARMING':
            if kind == 'focus': self.focus_event.set()
        elif self.target: self.check()

    async def arm(self, *, empty_confirmed):
        if self.closed or self.lock.locked() or not empty_confirmed or not self.config.manual_fill_enabled:
            raise TargetRejected('arm_rejected')
        if self.target: self.invalidate('explicit_rearm', operator=True)
        if self.tasks: await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self.cleanup_failed: raise TargetRejected('cleanup_failed')
        async with self.lock:
            ctx = self.context()
            if not ctx or not ctx.connected or not ctx.active: raise TargetRejected('live_inactive')
            self.state, self.reason, self.composer = 'ARMING', 'click_empty_composer', 'UNKNOWN'
            if (self.session, self.runtime, self.account) != (ctx.live_session_id, ctx.active_runtime_revision, ctx.target_account):
                self.accepted_target = self.confirmed_target = None
                self.auto_enabled = False
            self.session, self.runtime = ctx.live_session_id, ctx.active_runtime_revision
            self.account = ctx.target_account
            self.focus_event.clear(); self.invalid.clear()
            try:
                self.target = resolve(self.native.snapshot())
                if self.accepted_target != self.target: self.accepted_target = None
                loop = asyncio.get_running_loop()
                self.generation += 1
                generation = self.generation
                self.watch = self.native.watch(self.target, lambda kind: loop.call_soon_threadsafe(self._event, kind, generation), self.invalid)
                await self.watch.start()
                self.watch_ready = True
                self.changed()
                until = self.clock() + self.config.focus_watch_seconds
                while self.target and self.state == 'ARMING':
                    await asyncio.wait_for(self.focus_event.wait(), max(0.001, until - self.clock()))
                    self.focus_event.clear()
                    if not self.target or self.state != 'ARMING': break
                    if self._context_ok() and focused(self.native.snapshot(), self.target):
                        self.invalid.clear()  # Fresh explicit focus, not automatic re-arm.
                        self.armed_at = self.clock(); self.composer = 'EMPTY_ATTESTED'
                        self.state, self.reason = 'ARMED', 'ready'
                        if not self.check(): break
                        self.monitor = self.own(self._monitor())
                        self.changed(); return self.get_status()
                raise TargetRejected('arm_rejected')
            except BaseException:
                self.invalidate('arm_timeout_or_failed')
                raise

    async def _monitor(self):
        try:
            while self.target and not self.closed:
                await asyncio.sleep(0.1)
                if self.target is None or self.closed: return
                if not self.check(): return
        except BaseException:
            if not self.closed: self.invalidate('monitor_failed')

    def get_status(self):
        age = None if self.target is None or self.armed_at is None else max(0, int(self.clock() - self.armed_at))
        valid = self.target is not None and not self.invalid.is_set() and self._context_ok() and self.armed_at is not None and self.clock() - self.armed_at < self.config.arm_ttl_seconds
        ready = valid and self.state == 'ARMED' and self.composer == 'EMPTY_ATTESTED'
        return WindowsSenderStatus(state=self.state, prepare_available=ready and self.config.manual_fill_enabled,
            submit_available=ready and self.settings.enable_physical_windows_send and bool(self.config.broadcaster_user_id),
            armed_age_seconds=age, foreground_guard=valid, auto_enabled=self.auto_enabled, composer=self.composer,
            verification=self.verification, last_send_status=self.last_send_status, reason=self.reason,
            watch_seconds=self.config.focus_watch_seconds, empty_after_verified_confirmed=self.empty_after_verified_confirmed,
            watch_ready=self.watch_ready, target_process_id=self.target.pid if self.target else None,
            target_instance=self.target.instance if self.target else None)

    async def healthcheck(self): return self.get_status()

    def observe(self, comment, session_id, account):
        if not comment.message_id: return
        key = (session_id, comment.message_id)
        if key in self.seen: return
        self.seen[key] = True
        while len(self.seen) > 1000: self.seen.popitem(last=False)
        pending = self.pending
        if not pending or session_id != self.session or account != pending['account']: return
        if comment.user_id != self.config.broadcaster_user_id or normalized(comment.text) != pending['text']: return
        if not comment.author_handle or comment.server_created_at is None:
            self.verification = 'WEAK'; self.changed(); return
        if comment.author_handle.casefold() != account.casefold(): return
        if (comment._received_monotonic <= pending['mono'] or comment.timestamp <= pending['wall'] or
                comment.server_created_at <= pending['wall'] or
                comment.server_created_at > pending['wall'] + timedelta(seconds=self.config.verification_seconds) or
                self.clock() > pending['deadline']): return
        pending['verified'] = True; pending['wake'].set()

    async def prepare(self, request): return await self.send(request)

    async def send(self, request):
        if request is None: return DeliveryResult(status='not_sent', reason='not_configured')
        if self.lock.locked(): return DeliveryResult(status='not_sent', reason='rejected')
        async with self.lock:
            physical = False
            enter = self.operation_mode in {'send_once', 'auto'}
            if not self.config.manual_fill_enabled: return DeliveryResult(status='not_sent', reason='physical_delivery_disabled')
            if enter and (not self.settings.enable_physical_windows_send or not self.config.broadcaster_user_id or
                          self.operation_mode == 'auto' and (not self.auto_enabled or not self.empty_after_verified_confirmed)):
                return DeliveryResult(status='not_sent', reason='physical_delivery_disabled')
            def guard():
                nonlocal physical
                allowed = self.check() and self.preflight(request) and request.live_session_id == self.session
                if self.operation_mode == 'auto': allowed = allowed and self.auto_enabled and self.empty_after_verified_confirmed
                if allowed: physical = True
                return allowed
            try:
                list(unicode_events(request.text))
                if self.composer != 'EMPTY_ATTESTED' or not self.check() or not self.preflight(request):
                    self.invalidate('composer_or_approval_unknown')
                    return DeliveryResult(status='needs_operator', reason='operator_required')
                self.state, self.composer = 'FILLING', 'UNKNOWN'
                await self.native.fill(request.text, guard)
                self.state, self.composer = 'FILLED', 'FILLED_UNVERIFIED'
                self.last_send_status = 'FILLED_ONLY'; self.changed()
                if not enter: return DeliveryResult(status='not_sent', reason='filled_only')
                if not guard(): raise TargetRejected('guard_failed')
                self.state = 'SENDING'
                self.pending = {'account': request.target_account, 'text': normalized(request.text),
                    'mono': self.clock(), 'wall': datetime.now(UTC), 'deadline': self.clock() + self.config.verification_seconds,
                    'wake': asyncio.Event(), 'verified': False}
                self.verification = 'VERIFYING'
                await self.native.enter(guard)
                self.state = 'VERIFYING'
                self.changed()
                await asyncio.wait_for(self.pending['wake'].wait(), self.config.verification_seconds)
                if not self.pending['verified'] or not self.check(): raise TargetRejected('verification_failed')
                self.last_send_status, self.verification = 'SENT_CONFIRMED', 'VERIFIED'
                self.confirmed_target, self.confirmed_at = self.target, self.clock()
                self.composer = 'EMPTY_ATTESTED' if self.empty_after_verified_confirmed else 'UNKNOWN'
                self.state, self.reason = 'ARMED', 'ready' if self.empty_after_verified_confirmed else 'confirm_empty_or_rearm'
                return DeliveryResult(status='confirmed', reason='echo_verified')
            except BaseException as exc:
                self.last_send_status = 'SEND_UNKNOWN' if physical else 'NOT_SENT'
                if self.verification != 'WEAK':
                    self.verification = 'TIMEOUT' if isinstance(exc, TimeoutError) else 'UNAVAILABLE'
                self.invalidate('delivery_unknown' if physical else 'sender_exception')
                if isinstance(exc, asyncio.CancelledError): raise
                return DeliveryResult(status='unknown' if physical else 'needs_operator', reason='timeout' if isinstance(exc, TimeoutError) else 'operator_required')
            finally:
                self.pending = None; self.changed()

    def confirm_empty(self):
        if (not self.confirmed_target or self.confirmed_at is None or self.clock() - self.confirmed_at > 60 or
                not self._context_ok() or resolve(self.native.snapshot()) != self.confirmed_target):
            raise TargetRejected('acceptance_required')
        self.accepted_target = self.confirmed_target
        if self.target == self.accepted_target: self.composer = 'EMPTY_ATTESTED'
        self.changed()

    def set_auto(self, enabled):
        if enabled and (not self._context_ok() or not self.settings.enable_physical_windows_send or not self.config.broadcaster_user_id or not self.empty_after_verified_confirmed):
            raise TargetRejected('auto_unavailable')
        self.auto_enabled = enabled; self.changed()

    async def aclose(self):
        self.closed = True
        self.invalidate('shutdown', operator=True)
        tasks = tuple(self.tasks)
        for task in tasks:
            if task is self.monitor: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
