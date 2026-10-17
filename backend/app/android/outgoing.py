"""Owned exact real-SDK outgoing receipts. No UI/text/ADB success implies Send."""
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic

from app.android.challenge_text import is_challenge_text
from app.android.delivery_evidence import claim_echo
from app.live.models import ingestion_boundary

SDK_WAIT_SECONDS = 12


@dataclass
class PendingEcho:
    operation_id: str
    text: str
    scope: tuple
    installation: tuple
    created: float
    sequence: int
    wall: datetime
    expires: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    invalid: bool = False


class OutgoingConfirmation:
    def __init__(self, service):
        self.service = service
        self.pending = None
        self.seen = set()

    def available(self, target):
        s, c = self.service, self.service.controller
        scope = s.account_proof.scope()
        return bool(scope and scope[0] == target.account.casefold() and scope[2] == target.live_session_id
            and target.device_serial == c.last.serial and c._installation
            and c._account_binding == (c._installation, scope[0]))

    def begin(self, operation_id, target, text, *, seconds=90):
        self.pending = None
        if not self.available(target): return False
        s, c = self.service, self.service.controller
        scope = s.account_proof.scope()
        now, sequence = ingestion_boundary()
        self.pending = PendingEcho(operation_id, text, scope, c._installation, now, sequence,
            datetime.now(UTC), now + seconds)
        return True

    def valid(self, p):
        s, c = self.service, self.service.controller
        now = monotonic()
        if (self.pending is not p or p.invalid or now < p.created or now >= p.expires
                or abs((datetime.now(UTC) - p.wall).total_seconds() - (now - p.created)) > 2
                or s.account_proof.scope() != p.scope or c._installation != p.installation
                or c._account_binding != (p.installation, p.scope[0])):
            p.invalid = True
            return False
        return True

    def observe(self, comment):
        receipt = comment._sdk_receipt
        if receipt is None or comment.source != "tiktok": return False
        key = (self.service.account_proof.configured(), comment.message_id)
        fresh = bool(comment.message_id and key not in self.seen and len(self.seen) < 10_000)
        if fresh: self.seen.add(key)
        if is_challenge_text(comment.text): return False
        own = bool(comment.author_handle and comment.author_handle.casefold() == key[0])
        p = self.pending
        if not own: return False
        # Own SDK comments never re-enter the AI pipeline, even after settlement.
        if not p or not self.valid(p) or p.event.is_set(): return True
        original = comment._ingestion_receipt
        if (not fresh or not comment._sdk_text_exact or comment.text != p.text
                or receipt != (p.scope[3], p.scope[4]) or original is None
                or (original.account.casefold(), original.session_id, original.runtime_revision) != (p.scope[0], p.scope[2], p.scope[1])
                or original.message_id != comment.message_id or original.sequence != comment._ingestion_sequence
                or not p.created <= comment._received_monotonic < p.expires
                or comment._ingestion_sequence <= p.sequence or comment.server_created_at is None
                or not p.wall - timedelta(seconds=1) <= comment.server_created_at <= datetime.now(UTC) + timedelta(seconds=5)):
            return True
        try:
            if self.service.account_proof.durable_consumed(comment, p.scope): return True
            claim_echo(self.service, p.scope[0], comment.message_id, p.operation_id)
        except Exception:
            p.invalid = True
            return True
        p.event.set()
        return True

    async def wait(self, operation_id, guard, *, seconds=None):
        p = self.pending
        if p is None or p.operation_id != operation_id: return False
        deadline = min(p.expires, monotonic() + (SDK_WAIT_SECONDS if seconds is None else seconds))
        while self.valid(p) and guard():
            if p.event.is_set(): return True
            remaining = deadline - monotonic()
            if remaining <= 0: return False
            try: await asyncio.wait_for(p.event.wait(), min(.25, remaining))
            except TimeoutError: pass
        return False

    def finish(self, operation_id):
        if self.pending and self.pending.operation_id == operation_id: self.pending = None
