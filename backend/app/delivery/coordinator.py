"""Owned persistent operation boundary with authoritative reservation preflight."""
import asyncio
from uuid import uuid4

from app.delivery.models import DeliveryRequest, DeliveryResult, ReplySenderProtocol
from app.delivery.journal import DeliveryOperationStore, JournalConflict
from app.delivery.suggestions import SuggestionConflict
from app.lifecycle import finish_owned


class DeliveryCoordinator:
    def __init__(self, suggestions, journal: DeliveryOperationStore, sender: ReplySenderProtocol, context):
        self.suggestions, self.journal, self.sender, self.context = suggestions, journal, sender, context
        self.tasks = set()
        self.failed = False
        self.closed = False
        self.operation_id = None

    def preflight(self, request):
        try:
            e, _, verdict = self.suggestions.authoritative(request.suggestion_id, request.approval_id,
                request.suggestion_revision, self.context())
            operation = self.journal.lookup(request.idempotency_key)
            if operation is None or operation.status != "reserved" or operation.operation_id != request.operation_id:
                return False
            return (not self.closed and not self.failed and verdict.eligible and
                    e.suggested_reply == request.text and e.suggested_reply_hash == request.suggested_reply_hash and
                    e.live_session_id == request.live_session_id and e.target_account == request.target_account)
        except Exception:
            return False

    async def execute(self, suggestion_id, approval_id, expected_revision, idempotency_key, *, timeout=5,
                      device_serial=None):
        if self.closed or self.failed:
            try:
                operation = self.journal.lookup(idempotency_key)
                if operation and operation.suggestion_id == suggestion_id and operation.suggestion_revision == expected_revision and operation.approval_id == approval_id:
                    return DeliveryResult(status="unknown", reason="interrupted") if operation.status == "reserved" else DeliveryResult(status=operation.status, reason=operation.reason)
            except Exception:
                pass
            return DeliveryResult(status="needs_operator", reason="delivery_paused")
        e, _, verdict = self.suggestions.authoritative(suggestion_id, approval_id, expected_revision, self.context())
        if not verdict.eligible:
            raise SuggestionConflict(verdict.reason)
        request = DeliveryRequest(operation_id=uuid4().hex, idempotency_key=idempotency_key,
            suggestion_id=e.suggestion_id, suggestion_revision=e.suggestion_revision,
            comment_identity=e.comment_identity, approval_id=approval_id,
            suggested_reply_hash=e.suggested_reply_hash, text=e.suggested_reply,
            live_session_id=e.live_session_id, target_account=e.target_account,
            active_runtime_revision=e.active_runtime_revision, expires_at=e.expires_at, device_serial=device_serial)
        try:
            operation, reserved = self.journal.reserve(request)
        except JournalConflict:
            raise
        except Exception:
            self.failed = True
            return DeliveryResult(status="needs_operator", reason="journal_unavailable")
        self.operation_id = operation.operation_id
        if not reserved:
            if operation.status == "reserved":
                return DeliveryResult(status="unknown", reason="interrupted")
            return DeliveryResult(status=operation.status, reason=operation.reason)
        entered = False

        async def dispatch():
            nonlocal entered
            if not self.preflight(request):
                return DeliveryResult(status="not_sent", reason="rejected")
            entered = True
            return await self.sender.send(request)

        task = asyncio.create_task(dispatch(), name="owned-manual-delivery")
        self.tasks.add(task)
        def completed(done):
            self.tasks.discard(done)
            if not done.cancelled(): done.exception()
        task.add_done_callback(completed)
        result = DeliveryResult(status="unknown", reason="interrupted")
        cancelled = False
        try:
            done, _ = await asyncio.wait((task,), timeout=timeout)
            if not done:
                result = DeliveryResult(status="unknown", reason="timeout")
            else:
                raw = task.result()
                result = DeliveryResult.model_validate(raw.model_dump())
        except asyncio.CancelledError:
            cancelled = True
            result = DeliveryResult(status="unknown", reason="cancelled")
        except Exception:
            result = DeliveryResult(status="unknown", reason="sender_failed")
        finally:
            async def finalize():
                nonlocal result
                if not task.done():
                    task.cancel()
                    _, pending = await asyncio.wait((task,), timeout=timeout)
                    if pending:
                        self.failed = True  # Keep ownership and prohibit further dispatch.
                        result = DeliveryResult(status="unknown", reason="timeout")
                if task.done() and not task.cancelled(): task.exception()
                try:
                    self.journal.finish(request.operation_id, result)
                except Exception:
                    self.failed = True
                    result = DeliveryResult(status="unknown" if entered else "needs_operator", reason="journal_unavailable")
                if result.status in {"unknown", "needs_operator"}:
                    self.failed = True
            await finish_owned(asyncio.create_task(finalize(), name="delivery-finalize"))
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def aclose(self):
        self.closed = True
        tasks = tuple(self.tasks)
        for task in tasks: task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            if pending: self.failed = True
