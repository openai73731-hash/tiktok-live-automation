"""Ephemeral two-channel own-account proof. Never performs Android input."""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import asyncio
import logging
from time import monotonic
from typing import Literal

from app.android.models import DeviceModel, ScreenState
from app.android.challenge_text import is_challenge_text, new_challenge_text
from app.android.feed import HostFeed, host_feed
from app.android.tiktok_ui import TIKTOK_YOU_4683, inspect_hierarchy
from app.delivery.models import AndroidTarget
from app.delivery.registry import REGISTRY_NAME, read_store
from app.delivery.suggestions import SuggestionConflict, comment_identity, durable_delivery_key
from app.live.models import LiveState, ingestion_boundary
from app.live.service import LiveService
from app.live.listener import LiveListener
from app.logging_config import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME + ".android")


class ChallengeChecks(DeviceModel):
    challenge_active: bool
    source_matches: bool
    session_matches: bool
    author_present: bool
    author_matches: bool
    token_matches: bool
    original_text_exact: bool
    message_identity_fresh: bool
    receipt_fresh: bool
    server_time_present: bool
    server_time_fresh: bool


class AccountChallengeView(DeviceModel):
    state: Literal["REQUIRED", "WAITING", "CHECKING", "VERIFIED", "EXPIRED", "INVALIDATED"] = "REQUIRED"
    token: str | None = None
    expires_in_seconds: int = 0
    available: bool = False
    last_checks: ChallengeChecks | None = None


@dataclass
class Challenge:
    token: str
    scope: tuple
    created: float
    sequence: int
    wall_created: datetime
    expires: float
    host_snapshot: HostFeed | None
    consumed: bool = False


class MobileAccountChallenge:
    def __init__(self, service):
        self.service = service
        self.runtime = None
        self.prepared = self.challenge = self.verified = None
        self.state = "REQUIRED"
        self.manual_scope = None
        self.connection_attempt = None
        self.last_checks = None
        # Never evict identities and allow them to become fresh again.
        self.seen = set()

    def configured(self):
        return self.runtime.username.casefold() if self.runtime else ""

    def scope(self):
        r, s = self.runtime, self.service
        if (r is None or r.closed or r.transport != "android" or r.admin.delivery is not s or r.pending or r.config_error
                or s.closed or s.paused or r.live.session_invalidated): return None
        live = r.live.service
        # ReplayListener subclasses LiveListener: exact type + real mode matters.
        if (type(live) is not LiveService or type(live.listener) is not LiveListener
                or not live._running or r.live.task is None or r.live.task.done()): return None
        listener = live.listener
        status = live.status
        if (status.mode != "real" or status.state != LiveState.CONNECTED or status.terminal_state is not None
                or not listener.session_active or not listener.connected.is_set()
                or listener.connected_handle != self.configured()
                or listener.settings.live_mode != "real" or status.username.casefold() != self.configured()
                or r.admin.live_status.state != LiveState.CONNECTED or not r.live.session_id): return None
        return (self.configured(), r.active_runtime_revision, r.live.session_id, listener, listener.connection_generation)

    def prepared_now(self):
        r, c = self.runtime, self.service.controller
        return bool(r and self.prepared and self.prepared[:2] == (self.configured(), r.active_runtime_revision)
            and self.prepared[2] == c._installation and c.serial == self.prepared[2][0])

    def valid(self):
        c = self.service.controller
        return bool(self.verified and self.prepared_now() and self.scope() == self.verified
            and c._account_binding == (c._installation, self.verified[0]))

    @property
    def manual_accepted(self):
        return bool(self.manual_scope and self.prepared_now() and self.scope() == self.manual_scope)

    def manual_verified(self):
        scope, c = self.scope(), self.service.controller
        if scope and self.prepared_now() and c._account_binding == (c._installation, scope[0]):
            self.manual_scope = scope

    def revoke(self):
        changed = self.challenge is not None or self.verified is not None
        self.challenge = self.verified = None
        self.manual_scope = None
        if changed: self.state = "INVALIDATED"
        c = self.service.controller
        if getattr(c, "_account_scope_guard", None) is not None:
            c.invalidate_trust()
            c.last = c.last.model_copy(update={"account_verified": False, "live_verified": False,
                "send_confirmation_token": None, "send_control_state": "UNAVAILABLE"})
        return changed

    def check(self):
        if self.verified is not None and not self.valid(): self.revoke()
        ch = self.challenge
        if ch is not None:
            if not self.prepared_now() or self.scope() != ch.scope: self.revoke()
            elif monotonic() >= ch.expires:
                self.revoke()
                self.state = "EXPIRED"

    def view(self):
        self.check()
        ch = self.challenge
        return AccountChallengeView(state=self.state,
            token=ch.token if ch and not ch.consumed else None,
            expires_in_seconds=max(0, int(ch.expires - monotonic())) if ch else 0,
            last_checks=self.last_checks,
            available=bool(self.prepared_now() and self.scope() and ch is None and self.verified is None))

    def target(self, scope):
        return AndroidTarget(account=scope[0], live_session_id=scope[2], device_serial=self.prepared[2][0])

    def reviewed_device(self, state):
        c = self.service.controller
        return (state.device in {"CONNECTED", "READY"} and len(state.devices) == 1 and state.devices[0].state == "CONNECTED"
            and state.serial == self.prepared[2][0] and state.profile_verified and not state.security_state
            and c._profile == TIKTOK_YOU_4683 and c._installation == self.prepared[2])

    def host(self, state):
        return self.reviewed_device(state) and state.screen == ScreenState.LIVE_READY and state.live_verified

    async def issue(self):
        s = self.service
        async def run():
            async with s.lock:
                self.check()
                scope = self.scope()
                if (not scope or not self.prepared_now() or self.challenge or self.verified or s.pending_fill or s.mode != "MANUAL"
                        or s.armed_binding is not None): raise SuggestionConflict("Prepare Mobile; connect own real LIVE; AUTO OFF; challenge unavailable.")
                state = await s.controller.inspect(self.target(scope))
                if not self.host(state) or not self.prepared_now() or self.scope() != scope:
                    raise SuggestionConflict("Fresh reviewed host LIVE/device/install evidence required.")
                now, sequence = ingestion_boundary()
                self.challenge = Challenge(new_challenge_text(), scope, now, sequence,
                    datetime.now(UTC), now + 60, host_feed(s.controller._xml))
                self.last_checks = None
                self.state = "WAITING"
            return s.view()
        return await s._owned(run)

    def connected(self):
        scope = self.scope()
        if not scope or not self.prepared_now() or scope == self.connection_attempt: return
        self.connection_attempt = scope
        task = asyncio.create_task(self.issue(), name="mobile-account-challenge-issue")
        self.service.tasks.add(task)
        def done(task):
            self.service.tasks.discard(task)
            if not task.cancelled(): task.exception()  # Static conflict remains REQUIRED in UI.
            self.service.on_idle()
        task.add_done_callback(done)

    def durable_consumed(self, comment, scope):
        s = self.service
        identity, _ = comment_identity(comment, "tiktok", scope[2], scope[0])
        args = ("", identity, durable_delivery_key("tiktok", scope[0], comment.message_id))
        kwargs = dict(source="tiktok", message_id=comment.message_id, account=scope[0])
        if s.journal and s.journal._duplicate(*args, **kwargs): return True
        for name in (REGISTRY_NAME, "delivery.sqlite3", "windows-delivery.sqlite3"):
            with read_store(s.settings.state_dir / name) as reader:
                if reader and reader._duplicate(*args, **kwargs): return True
        return False

    def observe(self, comment):
        # Return True to intercept the reserved challenge namespace BEFORE queue/AI.
        reserved = is_challenge_text(comment.text)
        receipt = comment._sdk_receipt
        key = (self.configured(), comment.message_id)
        duplicate = key in self.seen or len(self.seen) >= 10_000
        if receipt is not None and comment.message_id and not duplicate: self.seen.add(key)
        if not reserved: return False
        self.check()
        ch = self.challenge
        # Boolean diagnostics only: no token, text, author/UID or SDK payload.
        # These fields explain a rejection; the authoritative guards below stay unchanged.
        self.last_checks = ChallengeChecks(
            challenge_active=bool(ch and not ch.consumed),
            source_matches=bool(ch and comment.source == "tiktok" and receipt == (ch.scope[3], ch.scope[4])),
            session_matches=bool(ch and self.scope() == ch.scope),
            author_present=comment.author_handle is not None,
            author_matches=bool(ch and comment.author_handle and comment.author_handle.casefold() == ch.scope[0]),
            token_matches=bool(ch and comment.text == ch.token),
            original_text_exact=comment._sdk_text_exact,
            message_identity_fresh=bool(comment.message_id and not duplicate),
            receipt_fresh=bool(ch and ch.created <= comment._received_monotonic < ch.expires and comment._ingestion_sequence > ch.sequence),
            server_time_present=comment.server_created_at is not None,
            server_time_fresh=bool(ch and comment.server_created_at and ch.wall_created - timedelta(seconds=1)
                <= comment.server_created_at <= datetime.now(UTC) + timedelta(seconds=5)))
        logger.info("mobile_account_challenge_evidence", extra={"fields":self.last_checks.model_dump()})
        if (ch is None or ch.consumed or duplicate or comment.source != "tiktok" or not comment._sdk_text_exact
                or receipt != (ch.scope[3], ch.scope[4]) or self.scope() != ch.scope
                or not comment.message_id or comment.author_handle is None or comment.author_handle.casefold() != ch.scope[0]
                or comment.text != ch.token or comment._received_monotonic < ch.created or comment._ingestion_sequence <= ch.sequence
                or comment._received_monotonic >= ch.expires or comment.server_created_at is None
                or not ch.wall_created - timedelta(seconds=1) <= comment.server_created_at <= datetime.now(UTC) + timedelta(seconds=5)):
            self.service.on_idle()
            return True
        try:
            if self.durable_consumed(comment, ch.scope): return True
        except Exception:
            self.revoke()
            self.service.on_idle()
            return True
        ch.consumed = True  # No await through consumption/scheduling; no replay can refresh it.
        self.state = "CHECKING"
        task = asyncio.create_task(self.complete(ch, comment), name="mobile-account-challenge-readback")
        self.service.tasks.add(task)
        def done(task):
            self.service.tasks.discard(task)
            if not task.cancelled(): task.exception()
            self.service.on_idle()
        task.add_done_callback(done)
        self.service.on_idle()
        return True

    async def complete(self, ch, comment):
        s, c = self.service, self.service.controller
        try:
            async with s.lock:
                if self.challenge is not ch or self.scope() != ch.scope: return
                state = await c.inspect(self.target(ch.scope))
                # Sending the human challenge can leave the exact reviewed composer
                # open. Read it afresh, keep the ORIGINAL host lease, and grant no
                # account authority until all SDK/scope/install/durable guards pass.
                fresh_host = self.prepared_now() and self.host(state)
                composer = (self.prepared_now() and self.reviewed_device(state)
                    and state.screen == ScreenState.COMMENT_INPUT_READY and state.comment_input_found
                    and monotonic() - ch.created < 90)
                logger.info("mobile_account_challenge_readback", extra={"fields":dict(
                    screen=state.screen.value, reason=state.reason, security_ui=state.security_state,
                    prepared=self.prepared_now(), fresh_host=bool(fresh_host), reviewed_composer=bool(composer))})
                if (self.challenge is not ch or self.scope() != ch.scope or not self.prepared_now()
                        or monotonic() >= ch.expires or not (fresh_host or composer) or self.durable_consumed(comment, ch.scope)):
                    self.revoke()
                    return
                self.verified, self.challenge, self.state = ch.scope, None, "VERIFIED"
                c._account_binding = (c._installation, ch.scope[0])
                c._account_scope_guard = self.valid
                c._live_evidence = (c._installation, ch.scope[0], ch.scope[2], monotonic() if fresh_host else ch.created)
                c._host_snapshot = host_feed(c._xml) if fresh_host else ch.host_snapshot
                parsed = inspect_hierarchy(c._xml, c.last.package, ch.scope[0], c._profile, account_bound=True, live_bound=True)
                c.last = c.last.model_copy(update=parsed)
                s._verified_runtime_revision = ch.scope[1]
        except BaseException:
            self.revoke()
            raise
