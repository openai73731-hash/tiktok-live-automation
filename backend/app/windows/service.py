"""Windows transport reuses authoritative approval, coordinator and durable dedup."""
import asyncio
from time import monotonic
from uuid import uuid4
from contextlib import contextmanager
import sqlite3

from app.delivery.coordinator import DeliveryCoordinator
from app.delivery.journal import SQLiteDeliveryOperationStore, JournalConflict
from app.delivery.models import DeliveryResult, DryRunView, ManualDeliveryInput, AutoDeliveryEligibility
from app.delivery.service import DeliveryView, ManualDeliveryService
from app.delivery.suggestions import SuggestionConflict
from app.windows.sender import WindowsLiveStudioReplySender
from app.windows.target import TargetRejected
from app.lifecycle import finish_owned


class WindowsManualDeliveryService(ManualDeliveryService):
    def __init__(self, settings, suggestions=None, context=None, *, native=None, journal_factory=None, journal_path=None):
        self.settings, self.suggestions = settings, suggestions
        self.context = context or (lambda: None)
        self.sender = WindowsLiveStudioReplySender(settings, self.context, native=native)
        self.sender.changed = lambda: self.on_idle()
        self.closed = self.paused = False
        self.on_idle = lambda: None
        self.tasks = set()
        self.lock = asyncio.Lock()
        self.arm_task = None
        self.journal = self.coordinator = None
        self.journal_factory = journal_factory or (lambda: SQLiteDeliveryOperationStore(settings.state_dir / 'windows-delivery.sqlite3'))
        self.journal_path = journal_path or settings.state_dir / 'windows-delivery.sqlite3'
        self.last_attempt = float('-inf')
        self.auto_pending = set()
        if suggestions is not None:
            suggestions.auto_registration_epoch = self.registration_epoch

    @property
    def configured(self): return self.settings.windows.executable_path is not None

    @property
    def busy(self):
        return self.lock.locked() or self.sender.lock.locked() or bool(self.arm_task and not self.arm_task.done())

    @property
    def delivery_available(self): return self.configured and self.settings.windows.manual_fill_enabled

    def device_ready(self, context):
        sender = self.sender
        return (sender.target is not None and not sender.invalid.is_set() and sender.armed_at is not None and
                sender.clock() - sender.armed_at < sender.config.arm_ttl_seconds and sender.session == context.live_session_id)

    def text_supported(self, text):
        from app.windows.native_input import unicode_events
        try: return self.delivery_available and bool(list(unicode_events(text or '')))
        except (ValueError, TargetRejected): return False

    def can_send(self, review):
        if not review.eligibility.eligible or not self.sender.get_status().prepare_available or self.busy or self.paused: return False
        return not self.consumed(review.envelope)

    @contextmanager
    def read_journal(self):
        """GET never creates/migrates a DB. Existing durable results survive UI refresh/restart."""
        if self.journal is not None:
            yield self.journal
            return
        path = self.journal_path
        if not path.exists():
            yield None
            return
        reader = SQLiteDeliveryOperationStore.__new__(SQLiteDeliveryOperationStore)
        reader.connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)
        reader.connection.row_factory = sqlite3.Row
        try: yield reader
        finally: reader.connection.close()

    def consumed(self, envelope):
        try:
            from app.delivery.registry import consumed
            if consumed(self.settings.state_dir, envelope): return True
            with self.read_journal() as journal:
                return bool(journal and journal.has_envelope_operation(envelope))
        except Exception: return True

    def uncertain(self):
        if self.sender.last_send_status == 'SEND_UNKNOWN': return True
        try:
            from app.delivery.registry import read_store
            for name in ('delivery.sqlite3', 'windows-delivery.sqlite3'):
                path = self.settings.state_dir / name
                if path == self.journal_path: continue
                with read_store(path) as legacy:
                    if legacy and legacy.unresolved(): return True
            with self.read_journal() as journal:
                owned = self.coordinator.operation_id if self.coordinator and not self.coordinator.closed and self.lock.locked() else None
                return bool(journal and journal.unresolved(excluding=owned))
        except Exception: return True

    @property
    def cooldown(self): return monotonic() - self.last_attempt < self.settings.windows.cooldown_seconds

    def view(self):
        status = self.sender.get_status()
        ready = self.configured and status.prepare_available and not self.closed and not self.paused and not self.cooldown
        return DeliveryView(transport='windows_live_studio', instance_id=self.settings.instance_id,
            account_label=self.settings.account_label, windows_status='operator_arm_required' if self.configured else 'probe_required',
            windows_sender=status, physical_delivery_enabled=self.settings.enable_physical_windows_send,
            delivery_uncertain=self.uncertain(),
            physical_ready=ready, delivery='FILL READY' if ready else 'NOT READY', paused=self.paused, busy=self.busy,
            reason='delivery_paused' if self.paused or self.closed else 'not_configured' if not self.configured else 'cooldown' if self.cooldown else 'ready' if ready else 'device_unavailable')

    def require_discovered(self, serial):
        if serial is not None: raise SuggestionConflict('Android selection is unavailable in Windows mode.')

    def set_serial(self, serial): self.sender.invalidate('runtime_changed')

    def revoke_authority(self):
        self.sender.invalidate('runtime_changed', operator=True)

    async def inspect(self): return self.view()

    async def capture(self): raise SuggestionConflict('Use the local Windows discovery probe.')

    async def trust_action(self, **kwargs): raise SuggestionConflict('Use explicit Windows arm.')

    def prepare(self):
        if self.delivery_available and self.journal is None:
            try:
                self.journal = self.journal_factory()
                if self.journal.unresolved(): self.paused = True
            except Exception: self.paused = True
        return self.view()

    def start_workflow(self, *, empty_confirmed, body=None, mode='fill'):
        if self.closed or self.busy or not self.delivery_available or not empty_confirmed:
            raise SuggestionConflict('Windows arm unavailable.')
        if mode == 'send_once' and (not self.settings.enable_physical_windows_send or not self.settings.windows.broadcaster_user_id):
            raise SuggestionConflict('Windows physical send disabled.')
        self.prepare()
        if self.journal is None:
            self.sender.invalidate('journal_unavailable')
            raise SuggestionConflict('Windows journal unavailable.')
        async def run():
            try:
                await self.sender.arm(empty_confirmed=empty_confirmed)
                # Explicit empty-field attestation clears the global pause for NEW
                # messages only. Durable unknown/reserved identities never unlock.
                self.paused = False
                if body is not None: await self._execute(body, mode)
                elif self.sender.auto_enabled: self.queue_approved()
            except (TargetRejected, TimeoutError):
                self.sender.invalidate('arm_failed')
            finally: self.on_idle()
        self.arm_task = asyncio.create_task(self._owned(run), name='windows-arm-workflow')
        return self.view()

    async def disarm(self):
        self.sender.invalidate('operator_disarm', operator=True)
        if self.arm_task and not self.arm_task.done():
            self.arm_task.cancel(); await asyncio.gather(self.arm_task, return_exceptions=True)
        if not self.sender.cleanup_failed: self.sender.invalidate('operator_disarm', operator=True)
        return self.view()

    async def dry_run(self, body):
        return DryRunView(eligible=False, device_ready=False, would_send=False, reason='delivery_unavailable', diagnostic_id=uuid4().hex)

    async def send(self, body):
        if not self.delivery_available: return DeliveryResult(status='not_sent', reason='not_configured')
        if self.busy: return DeliveryResult(status='not_sent', reason='rejected')
        async def run(): return await self._execute(body, 'send_once')
        return await self._owned(run)

    async def _execute(self, body, mode, *, auto_epoch_id=None):
        async with self.lock:
            self.prepare()
            if self.journal is None: return DeliveryResult(status='needs_operator', reason='journal_unavailable')
            try: previous = self.journal.lookup(body.idempotency_key)
            except Exception:
                self.paused = True; self.sender.invalidate('journal_unavailable')
                return DeliveryResult(status='needs_operator', reason='journal_unavailable')
            if previous:
                if (previous.suggestion_id, previous.approval_id, previous.suggestion_revision) != (body.suggestion_id, body.approval_id, body.expected_suggestion_revision):
                    return DeliveryResult(status='not_sent', reason='rejected')
                return DeliveryResult(status='unknown', reason='interrupted') if previous.status == 'reserved' else DeliveryResult(status=previous.status, reason=previous.reason)
            if self.paused or self.closed: return DeliveryResult(status='needs_operator', reason='delivery_paused')
            if mode == 'auto' and (auto_epoch_id is None or not self.auto_eligibility(body.suggestion_id, epoch_id=auto_epoch_id).eligible):
                return DeliveryResult(status='not_sent', reason='rejected')
            if self.cooldown and mode == 'auto':
                # Waiting before a first attempt is not an input retry. The bounded
                # owned queue rechecks all live/approval/native guards afterwards.
                await asyncio.sleep(max(0, self.settings.windows.cooldown_seconds - (monotonic() - self.last_attempt)))
                if not self.auto_eligibility(body.suggestion_id, epoch_id=auto_epoch_id).eligible or not self.sender.check():
                    return DeliveryResult(status='not_sent', reason='rejected')
            if self.cooldown: return DeliveryResult(status='not_sent', reason='cooldown')
            self.sender.operation_mode = mode
            coordinator = DeliveryCoordinator(self.suggestions, self.journal, self.sender, self.context)
            self.coordinator = coordinator
            def preflight(request):
                return coordinator.preflight(request) and (mode != 'auto' or
                    self.auto_eligibility(request.suggestion_id, epoch_id=auto_epoch_id, operation=True).eligible)
            self.sender.preflight = preflight
            try:
                self.last_attempt = monotonic()
                result = await coordinator.execute(body.suggestion_id, body.approval_id, body.expected_suggestion_revision,
                    body.idempotency_key, timeout=60, mode='AUTO' if mode == 'auto' else 'MANUAL')
                if result.status in {'unknown', 'needs_operator'}:
                    self.paused = True
                    self.sender.invalidate('delivery_unknown', operator=self.sender.clock_verification == 'unstable' or
                        self.sender.config.acceptance_one_shot and self.sender.acceptance_spent)
                return result
            except JournalConflict:
                return DeliveryResult(status='not_sent', reason='rejected')
            except BaseException:
                self.sender.invalidate('sender_exception', operator=self.sender.clock_verification == 'unstable' or
                    self.sender.config.acceptance_one_shot and self.sender.acceptance_spent)
                raise
            finally:
                await coordinator.aclose(); self.on_idle()

    def observe_comment(self, comment, session_id, account):
        try: self.sender.observe(comment, session_id, account)
        except Exception: self.sender.invalidate('echo_observer_failed')

    def live_state(self, status):
        if status.state != 'connected' or status.terminal_state is not None: self.sender.invalidate('live_not_connected')

    def set_auto(self, enabled):
        self.sender.set_auto(enabled, watermark=self.suggestions.sequence)

    def receipt_after_epoch(self, envelope):
        s, r = self.sender, envelope._ingestion_receipt
        ctx = self.context()
        return bool(r and ctx and s.auto_enabled_monotonic is not None and s.ingestion_watermark is not None and
            r.monotonic >= s.auto_enabled_monotonic and r.sequence > s.ingestion_watermark and
            r.account == envelope.target_account == ctx.target_account and
            r.session_id == envelope.live_session_id == ctx.live_session_id and
            r.runtime_revision == envelope.active_runtime_revision == ctx.active_runtime_revision and
            r.message_id and r.message_id == envelope._source_message_id and
            r.broadcaster_user_id == s.config.broadcaster_user_id)

    def registration_epoch(self, envelope):
        return self.sender.auto_epoch_id if self.sender.auto_enabled and self.receipt_after_epoch(envelope) else None

    def auto_eligibility(self, suggestion_id, *, epoch_id=None, operation=False):
        """Server-owned authorization. AI's legacy advisory false grants nothing.

        Epoch + server creation/approval clocks are private and cannot be supplied
        by JSON. Recomputed at enqueue, reservation and every emission preflight.
        """
        s = self.sender
        reason = 'eligible'
        if self.closed or self.paused: reason = 'delivery_paused'
        elif s.config.acceptance_one_shot and s.acceptance_spent: reason = 'acceptance_spent'
        elif not s.auto_enabled: reason = 'auto_off'
        elif epoch_id is not None and epoch_id != s.auto_epoch_id: reason = 'epoch_mismatch'
        elif not self.settings.enable_physical_windows_send or not s.config.broadcaster_user_id: reason = 'physical_gate'
        elif not s.empty_after_verified_confirmed or s.accepted_target != s.target: reason = 'composer_acceptance'
        else:
            try:
                review = self.suggestions.view(suggestion_id, self.context())
                e, approval = review.envelope, review.approval
                if not approval: reason = 'approval_required'
                elif (e._registered_auto_epoch_id != s.auto_epoch_id or not self.receipt_after_epoch(e) or
                      s.auto_enabled_monotonic is None or e._created_monotonic is None or approval._approved_monotonic is None or
                      e._created_monotonic < s.auto_enabled_monotonic or approval._approved_monotonic < s.auto_enabled_monotonic or
                      s.auto_watermark is None or e._created_sequence is None or approval._approved_sequence is None or
                      e._created_sequence <= s.auto_watermark or approval._approved_sequence <= s.auto_watermark): reason = 'before_epoch'
                elif not review.eligibility.eligible: reason = 'manual_ineligible'
                elif e.username.casefold() == (e.target_account or '').casefold(): reason = 'own_comment'
                elif (s.state not in ({'ARMED', 'FILLING', 'FILLED', 'SENDING'} if operation else {'ARMED'}) or
                      not self.device_ready(self.context()) or not s._context_ok()): reason = 'not_armed'
                elif not operation and self.consumed(e): reason = 'unused_required'
            except Exception: reason = 'manual_ineligible'
        return AutoDeliveryEligibility(eligible=reason == 'eligible', reason=reason, epoch_id=s.auto_epoch_id)

    def approved(self, suggestion_id):
        if not self.sender.auto_enabled or self.paused or suggestion_id in self.auto_pending or len(self.auto_pending) >= 8: return
        try:
            review = self.suggestions.view(suggestion_id, self.context())
            e = review.envelope
            if not self.auto_eligibility(suggestion_id).eligible: return
            body = ManualDeliveryInput(suggestion_id=e.suggestion_id, expected_suggestion_revision=e.suggestion_revision,
                approval_id=review.approval.approval_id, idempotency_key='windows_auto_' + e.suggestion_id)
            epoch_id = self.sender.auto_epoch_id
        except Exception: return
        self.auto_pending.add(suggestion_id)
        async def run():
            if self.sender.auto_enabled: await self._execute(body, 'auto', auto_epoch_id=epoch_id)
        async def owned_run():
            try:
                await self._owned(run)
            finally: self.auto_pending.discard(suggestion_id)
        self.sender.own(owned_run())

    def offer_auto(self, suggestion_id):
        # Windows admission waits for its own post-epoch operator approval.
        # Never enter the inherited Android queue or content authorization path.
        return None

    def queue_approved(self):
        if self.suggestions:
            for e in tuple(self.suggestions.groups['live'].values()): self.approved(e.suggestion_id)

    def recent(self):
        from app.delivery.service import DeliveryHistoryEntry
        try:
            with self.read_journal() as journal:
                return tuple(DeliveryHistoryEntry(operation_id=o.operation_id, suggestion_id=o.suggestion_id, time=o.created_at,
                    username=o.target_account, device=None, result='unknown' if o.status == 'reserved' else o.status,
                    reason='interrupted' if o.status == 'reserved' else o.reason, mode=o.mode) for o in journal.recent()) if journal else ()
        except Exception:
            self.paused = True; return ()

    async def aclose(self):
        await finish_owned(asyncio.create_task(self._close(), name='windows-delivery-shutdown'))

    async def _close(self):
        self.closed = True
        await self.disarm()
        tasks = tuple(self.tasks)
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        if self.coordinator: await self.coordinator.aclose()
        await self.sender.aclose()
        if self.journal: self.journal.close()
