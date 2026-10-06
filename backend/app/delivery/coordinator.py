"""Testable operation boundary; deliberately absent from production routing."""
import asyncio
from uuid import uuid4

from app.delivery.models import DeliveryRequest, DeliveryResult, ReplySenderProtocol
from app.delivery.journal import DeliveryOperationStore
from app.delivery.suggestions import DeliveryEligibility, SuggestionConflict


class DeliveryCoordinator:
    def __init__(self, suggestions, journal: DeliveryOperationStore, sender: ReplySenderProtocol, context):
        self.suggestions, self.journal, self.sender, self.context = suggestions, journal, sender, context

    async def execute(self, suggestion_id, approval_id, expected_revision, idempotency_key, *, timeout=5):
        e = self.suggestions.get(suggestion_id)
        approval = self.suggestions.approvals.get(suggestion_id)
        # No browser-provided reply text. Read the exact immutable server value.
        if e.suggestion_revision != expected_revision or approval is None or approval.approval_id != approval_id:
            raise SuggestionConflict("Approval binding conflict")
        verdict = DeliveryEligibility.evaluate(e, approval, self.context())
        if not verdict.eligible:
            raise SuggestionConflict(verdict.reason)
        request = DeliveryRequest(operation_id=uuid4().hex, idempotency_key=idempotency_key,
            suggestion_id=e.suggestion_id, suggestion_revision=e.suggestion_revision,
            comment_identity=e.comment_identity, approval_id=approval_id,
            suggested_reply_hash=e.suggested_reply_hash, text=e.suggested_reply,
            live_session_id=e.live_session_id, target_account=e.target_account,
            active_runtime_revision=e.active_runtime_revision, expires_at=e.expires_at)
        operation, reserved = self.journal.reserve(request)
        if not reserved:
            # A previous uncertain attempt cannot authorize another side effect.
            if operation.status == "reserved":
                return DeliveryResult(status="unknown", reason="interrupted")
            return DeliveryResult(status=operation.status, reason=operation.reason)
        try:
            async def dispatch():
                # Recheck after task scheduling, immediately before the boundary.
                if not DeliveryEligibility.evaluate(e, approval, self.context()).eligible:
                    return DeliveryResult(status="not_sent", reason="rejected")
                return await self.sender.send(request)
            task = asyncio.create_task(dispatch(), name="owned-mock-delivery")
            task.add_done_callback(lambda completed: None if completed.cancelled() else completed.exception())
            done, _ = await asyncio.wait((task,), timeout=timeout)
            if not done:
                task.cancel()
                # Never turn a late response after the deadline into confirmed.
                await asyncio.wait((task,), timeout=timeout)
                result = DeliveryResult(status="unknown", reason="timeout")
            else:
                result = task.result()
            result = DeliveryResult.model_validate(result.model_dump())
        except TimeoutError:
            result = DeliveryResult(status="unknown", reason="timeout")
        except asyncio.CancelledError:
            if not task.done(): task.cancel()
            self.journal.finish(request.operation_id, DeliveryResult(status="unknown", reason="cancelled"))
            from app.lifecycle import finish_owned
            async def settle():
                await asyncio.wait((task,), timeout=timeout)
                if task.done() and not task.cancelled(): task.exception()
            await finish_owned(asyncio.create_task(settle(), name="mock-delivery-cleanup"))
            raise
        except Exception:
            result = DeliveryResult(status="unknown", reason="sender_failed")
        self.journal.finish(request.operation_id, result)
        return result
