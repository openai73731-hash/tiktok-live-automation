"""Only a completed production attempt that never reached Send can resume NEW events."""
from app.android.delivery_evidence import record_no_send, unresolved
from app.android.tiktok_ui import TIKTOK_YOU_4683
from app.delivery.models import AndroidTarget
from app.live.models import ingestion_boundary


class TechnicalRecovery:
    def __init__(self, service):
        self.service = service
        self.scope = self.installation = self.boundary = None
        self.proven_operation = None

    def old_event(self, envelope):
        s = self.service
        if self.boundary is None or s.account_proof.scope(ignore_pause=True) != self.scope: return False
        receipt = envelope._ingestion_receipt
        return (receipt is None or receipt.monotonic < self.boundary[0] or receipt.sequence <= self.boundary[1]
            or (receipt.account.casefold(), receipt.runtime_revision, receipt.session_id) != self.scope[:3])

    async def settle(self, result, coordinator, scope, installation, generation):
        s,c = self.service,self.service.controller
        self.proven_operation = None
        operation = coordinator.operation_id
        target = AndroidTarget(account=scope[0],live_session_id=scope[2],device_serial=c.serial or c.last.serial) if scope else None
        def current():
            return bool(scope and not s.closed and not s.paused and s.mode=='AUTOMATIC'
                and s.auto_generation==generation and s.account_proof.scope()==scope and c._installation==installation
                and s.armed_binding==(scope[2],scope[0],scope[1]) and c._profile==TIKTOK_YOU_4683
                and c._send_attempt==(operation,False,installation,target)
                and not any(not t.done() for t in coordinator.tasks)
                and not getattr(c.transport,'processes',()))
        if result.status!='needs_operator' or result.reason!='rejected' or not current(): return False
        state = await c.inspect(target)
        if not current() or state.security_state or not c.ready_for(scope[2],scope[0]): return False
        try:
            record_no_send(s,operation,target,scope[1])
            if unresolved(s,allow_rejected=True): return False
        except Exception: return False
        self.proven_operation = operation
        if s.settings.mobile_acceptance_one_shot: return False  # Never rearm a spent one-shot.
        self.scope,self.installation,self.boundary = scope,installation,ingestion_boundary()
        s.suggestions.auto_authorizations.clear()
        s.auto_generation += 1  # Old periodic capabilities and queued admissions expire.
        while not s.auto_queue.empty(): s.auto_queue.get_nowait(); s.auto_queue.task_done()
        return True
