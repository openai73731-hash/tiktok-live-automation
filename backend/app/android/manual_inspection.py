"""Fresh Profile/MANUAL evidence, with one acquisition recheck before any input."""
import asyncio
import json
import logging
from time import monotonic

from app.android.models import ScreenState
from app.android.tiktok_ui import unique_node

logger = logging.getLogger(__name__)
RECHECK_TIMEOUT = 8


class ManualInspection:
    def __init__(self, controller, target, runtime_revision=None):
        self.controller, self.target = controller, target
        self.runtime_revision = runtime_revision
        self.recheck_available = True
        self.input_started = False

    async def inspect(self):
        async with self.controller.lock:
            return await self.read()

    async def read(self):
        c = self.controller
        state = await c._inspect(self.target, manual=True)
        if c._hierarchy_unavailable and self.recheck_available and not self.input_started:
            self.recheck_available = False
            try:
                async with asyncio.timeout(RECHECK_TIMEOUT):
                    await asyncio.sleep(0.1)
                    state = await c._inspect(self.target, manual=True, compressed=True)
            except TimeoutError:
                c._hierarchy_unavailable = True
                c.last = state = state.model_copy(update={"screen": ScreenState.UNKNOWN_SCREEN, "reason": "hierarchy_unavailable"})
        if c._hierarchy_unavailable:
            c.invalidate_trust()
        return state

    def block(self, reason):
        c, target, state = self.controller, self.target, self.controller.last
        node = unique_node(c._xml, c._profile.input_id, c._profile.package) if c._profile else None
        evidence = c._manual_fill_evidence
        diagnostic = dict(c._inspection_trace, evidence_timestamp=c._inspection_timestamp,
            evidence_age_seconds=round(monotonic() - c._inspection_started, 3),
            classified_screen=state.screen.value, security_ui=state.security_state,
            composer={k: node.get(k) for k in ("resource-id", "class", "focused", "enabled", "clickable", "focusable")} if node is not None else None,
            runtime_revision=self.runtime_revision,
            session_match=bool(evidence and evidence[2] == target.live_session_id),
            account_match=bool(state.account_verified and c._account_binding == (c._installation, target.account)),
            device_match=state.serial == target.device_serial,
            install_match=bool(c._account_binding and c._account_binding[0] == c._installation),
            input_started=self.input_started, final_block_reason=reason)
        logger.warning("ANDROID_MANUAL_FILL_BLOCKED %s", json.dumps(diagnostic, sort_keys=True))
