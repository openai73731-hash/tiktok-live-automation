"""Explicit ACK grants only a fresh event boundary, never retries an operation."""
from app.android.delivery_evidence import acknowledge, unknown_snapshot, unresolved
from app.android.tiktok_ui import TIKTOK_YOU_4683
from app.delivery.models import AndroidTarget
from app.delivery.suggestions import SuggestionConflict
from app.live.models import ingestion_boundary


class UnknownRecovery:
    def __init__(self, service):
        self.service = service
        self.scope = self.installation = self.boundary = None

    def view(self):
        try:
            values, revision = unknown_snapshot(self.service)
            return len(values), revision if values else None
        except Exception: return 0, None

    def authorized(self):
        s, c = self.service, self.service.controller
        return bool(self.scope and s.account_proof.scope() == self.scope and c._installation == self.installation
            and c._account_binding == (self.installation, self.scope[0]))

    def old_event(self, envelope):
        if self.boundary is None: return False
        receipt = envelope._ingestion_receipt
        return (not self.authorized() or not envelope._source_message_id or receipt is None
            or receipt.monotonic < self.boundary[0] or receipt.sequence <= self.boundary[1]
            or (receipt.account.casefold(), receipt.runtime_revision, receipt.session_id) != self.scope[:3])

    async def acknowledge(self, body):
        s, c = self.service, self.service.controller
        def reject(): raise SuggestionConflict("ACK UNKNOWN requires fresh own account/LIVE/device, AUTO OFF, idle delivery and the current UNKNOWN snapshot. Old IDs remain consumed.")
        if s.busy or s.tasks: reject()
        async def run():
            async with s.lock:
                runtime = s.account_proof.runtime
                ctx = s.context()
                def idle():
                    return (s.mode == "MANUAL" and not s.closed and not s.pending_fill and s.armed_binding is None
                        and s.auto_queue.empty() and (s.auto_runner is None or s.auto_runner.done())
                        and (s.coordinator is None or s.coordinator.closed and not s.coordinator.tasks)
                        and not getattr(c.transport, "processes", ()))
                scope = s.account_proof.scope(ignore_pause=True)
                if (not runtime or not scope or not idle() or not ctx.connected or not ctx.active
                        or ctx.live_session_id != body.expected_live_session_id
                        or ctx.active_runtime_revision != body.expected_active_runtime_revision
                        or s._verified_runtime_revision != ctx.active_runtime_revision): reject()
                target = AndroidTarget(account=scope[0], live_session_id=scope[2], device_serial=c.serial)
                state = await c.inspect(target)
                if (not idle() or s.account_proof.scope(ignore_pause=True) != scope or state.security_state
                        or state.screen.value != "LIVE_READY" or not state.live_verified or not state.account_verified
                        or not state.profile_verified or c._profile != TIKTOK_YOU_4683
                        or state.serial != TIKTOK_YOU_4683.serial or not c._installation
                        or c._account_binding != (c._installation, scope[0])): reject()
                try:
                    if s.journal is None: s.journal = s.journal_factory()
                    acknowledge(s, body.expected_unknown_revision, scope[0], state.serial)
                    if unresolved(s, allow_rejected=True): reject()
                except Exception:
                    reject()
                # No await through ACK/boundary/publication. Every old row is
                # unchanged; pre-ACK suggestions and missing IDs fail closed.
                self.scope, self.installation, self.boundary = scope, c._installation, ingestion_boundary()
                s.suggestions.auto_authorizations.clear()
                s.account_proof.prepared = (scope[0], scope[1], c._installation)
                s.account_proof.challenge = None
                s.account_proof.manual_scope = None
                s.paused = False
                s.workflow, s.auto_reason = "READY", "automatic_mode_required"
                s.on_idle()
                return s.view()
        return await s._owned(run)
