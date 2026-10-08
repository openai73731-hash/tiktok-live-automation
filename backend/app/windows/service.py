"""Discovery-stage Admin bridge. Startup/GET/SSE perform no Windows or ADB I/O."""
from uuid import uuid4

from app.delivery.models import DeliveryResult, DryRunView
from app.delivery.service import DeliveryView
from app.delivery.suggestions import SuggestionConflict
from app.windows.sender import WindowsLiveStudioReplySender


class WindowsManualDeliveryService:
    def __init__(self, settings):
        self.settings = settings
        self.sender = WindowsLiveStudioReplySender()
        self.closed = self.paused = False
        self.on_idle = lambda: None

    @property
    def busy(self): return False

    @property
    def delivery_available(self): return False

    def device_ready(self, context): return False

    def text_supported(self, text): return False

    def can_send(self, review): return False

    def view(self):
        return DeliveryView(transport="windows_live_studio", instance_id=self.settings.instance_id,
            account_label=self.settings.account_label,
            windows_status=self.sender.get_status().discovery, physical_delivery_enabled=False,
            physical_ready=False, delivery="NOT READY", paused=self.paused, busy=False,
            reason="not_configured")

    def require_discovered(self, serial):
        if serial is not None:
            raise SuggestionConflict("Android selection is unavailable in Windows mode.")

    def set_serial(self, serial):
        # Preserve the legacy overlay for a future Android startup, with no ADB I/O.
        pass

    async def inspect(self):
        return self.view()  # Use the explicit local CLI probe for private UI trees.

    async def capture(self):
        raise SuggestionConflict("Use the local Windows discovery probe.")

    async def trust_action(self, **kwargs):
        raise SuggestionConflict("No reviewed Windows UI profile is available.")

    def prepare(self):
        # Existing route prepares the journal, not text. Windows cannot reserve yet.
        return self.view()

    async def dry_run(self, body):
        return DryRunView(eligible=False, device_ready=False, would_send=False,
            reason="delivery_unavailable", diagnostic_id=uuid4().hex)

    async def send(self, body):
        return DeliveryResult(status="not_sent", reason="not_configured")

    def recent(self): return ()

    async def aclose(self): self.closed = True
