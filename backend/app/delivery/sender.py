"""Bridge exact server requests to the independent Android device abstraction."""
from app.delivery.models import AndroidSubmission, AndroidTarget, DeliveryResult


class AndroidTikTokReplySender:
    def __init__(self, controller, *, physical_enabled=False, preflight=lambda request: False):
        self.controller = controller
        self.physical_enabled = physical_enabled
        self.preflight = preflight

    async def send(self, request):
        if not self.physical_enabled:
            return DeliveryResult(status="not_sent", reason="physical_delivery_disabled")
        if not self.preflight(request):
            return DeliveryResult(status="not_sent", reason="rejected")
        submission = AndroidSubmission(operation_id=request.operation_id,
            target=AndroidTarget(live_session_id=request.live_session_id, account=request.target_account,
                                 device_serial=request.device_serial),
            text=request.text, expires_at=request.expires_at)
        outcome = await self.controller.submit(submission, guard=lambda: self.preflight(request))
        return DeliveryResult.model_validate(outcome.model_dump())
