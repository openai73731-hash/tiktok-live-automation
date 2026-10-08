"""PC-SEND-1 capability boundary. No native input implementation is shipped."""
from typing import Literal

from app.delivery.models import DeliveryRequest, DeliveryResult, FrozenModel


class WindowsSenderStatus(FrozenModel):
    stage: Literal["PC-SEND-1"] = "PC-SEND-1"
    discovery: Literal["probe_required"] = "probe_required"
    prepare_available: Literal[False] = False
    submit_available: Literal[False] = False


class WindowsLiveStudioReplySender:
    """Reuse ReplySenderProtocol; a diagnostic candidate never authorizes input.

    healthcheck/get_status are cached capabilities with no desktop reads. Explicit
    discovery runs in the separate probe process. Android's physical flag cannot
    enable Windows actions. A reviewed real UI profile precedes PC-SEND-2/3.
    """

    def get_status(self) -> WindowsSenderStatus:
        return WindowsSenderStatus()

    async def healthcheck(self) -> WindowsSenderStatus:
        return self.get_status()

    async def prepare(self, request: DeliveryRequest) -> DeliveryResult:
        return DeliveryResult(status="not_sent", reason="not_configured")

    async def send(self, request: DeliveryRequest) -> DeliveryResult:
        return DeliveryResult(status="not_sent", reason="not_configured")
