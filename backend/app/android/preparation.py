"""Explicit MANUAL recovery; never rewrites a delivery journal or enables AUTO."""
from uuid import uuid4
import asyncio

from app.android.tiktok_ui import TIKTOK_YOU_4683
from app.delivery.models import AndroidTarget
from app.delivery.registry import read_store, REGISTRY_NAME
from app.delivery.suggestions import SuggestionConflict


def recovery_blocked(journal, state_dir, *, service=None):
    if service is not None:
        from app.android.delivery_evidence import unresolved
        return unresolved(service, allow_rejected=True)
    # A settled MANUAL rejection remains consumed, but does not prohibit a NEW
    # acceptance cycle. All ambiguous/unresolved operations still block recovery.
    query = """SELECT 1 FROM operations WHERE status IS NULL OR status NOT IN ('confirmed','not_sent','needs_operator')
        OR (status='needs_operator' AND (mode IS NULL OR mode!='MANUAL' OR reason IS NULL OR reason!='rejected'
            OR device_serial IS NULL OR device_serial!=?)) LIMIT 1"""
    params = (TIKTOK_YOU_4683.serial,)
    if journal.connection.execute(query, params).fetchone(): return True
    for name in (REGISTRY_NAME, "delivery.sqlite3", "windows-delivery.sqlite3"):
        with read_store(state_dir / name) as reader:
            if reader and reader.connection.execute(query, params).fetchone(): return True
    return False


async def prepare_mobile(service, runtime):
    def reject(reason):
        raise SuggestionConflict("operator_paused / recovery required: " + reason)

    async with runtime.lock:
        try: runtime.ensure_idle()
        except RuntimeError: reject("disconnect listener and wait for owned operations/cleanup")
        if runtime.transport != "android" or runtime.admin.delivery is not service:
            reject("active transport changed")
        if runtime.pending or runtime.config_error or runtime.closed:
            reject("Apply a valid Mobile configuration first")
        before = runtime.active_runtime_revision

        def idle():
            live = runtime.admin.live_view()
            coordinator = service.coordinator
            return (runtime.live.service is None and not live.connection_owned and live.state in {"disconnected", "stopped"}
                and service.mode == "MANUAL" and service.armed_binding is None and not service.closed
                and not service.pending_fill and service.auto_queue.empty()
                and (service.auto_runner is None or service.auto_runner.done())
                and not getattr(service.controller.transport, "processes", ())
                and (coordinator is None or coordinator.closed and not any(not t.done() for t in coordinator.tasks)))

        if service.busy or service.tasks or not idle(): reject("disconnect listener; AUTO OFF; wait for delivery cleanup")

        async def run():
            async with service.lock:
                if not idle(): reject("delivery is not idle")
                if service.paused and service._pause_reason not in {"manual_rejected", "no_send_rejected"}:
                    reject("pause is not a recoverable MANUAL rejection; UNKNOWN has NO RETRY")
                ctx = service.context()
                if not ctx.target_account or ctx.target_account != runtime.username:
                    reject("own account binding mismatch")
                target = AndroidTarget(live_session_id=ctx.live_session_id or "0" * 32,
                    account=ctx.target_account, device_serial=service.controller.serial)
                c = service.controller
                service.account_proof.prepared = None
                service.account_proof.revoke()
                try: installation = await c.prepare_installation(target)
                except ValueError: reject("fresh expected device/install preparation failed; security/login must be absent")
                try:
                    if service.journal is None: service.journal = service.journal_factory()
                    if recovery_blocked(service.journal, service.settings.state_dir, service=service):
                        service.paused = True
                        reject("unresolved delivery; UNKNOWN/reserved/filled has NO RETRY")
                except SuggestionConflict:
                    raise
                except Exception:
                    service.paused = True
                    reject("authoritative delivery journal unavailable")
                if not idle() or runtime.active_runtime_revision != before or runtime.pending:
                    reject("runtime or recovery evidence changed")
                from app.android.tiktok_ui import profile_account
                own_profile = profile_account(c._xml, ctx.target_account)
                if own_profile:
                    if not await c.verify_account(target): reject("fresh own Profile verification failed")
                    if not idle() or runtime.active_runtime_revision != before or runtime.pending:
                        reject("runtime or Profile evidence changed")
                # No await from the last checks through publication. Old approvals
                # become stale; no delivery row, one-shot latch or dedup key changes.
                runtime.active_runtime_revision = uuid4().hex
                runtime.live.session_invalidated = True
                service.suggestions.auto_authorizations.clear()
                c._live_evidence = c._manual_fill_evidence = c._send_binding = None
                c._candidate_key = c._candidate_token = None
                service.paused = False
                service._verified_runtime_revision = None
                service.account_proof.prepared = (runtime.username.casefold(), runtime.active_runtime_revision, installation)
                service.account_proof.state = "REQUIRED"
                if own_profile:
                    if not service.account_proof.record_profile(): reject("own Profile binding changed")
                service.workflow = "READY"

        try:
            async with asyncio.timeout(150):
                await service._owned(run)
        except TimeoutError:
            service.account_proof.prepared = None
            service.account_proof.revoke()
            reject("device preparation deadline exceeded")
        runtime.publish()
        return service.view()
