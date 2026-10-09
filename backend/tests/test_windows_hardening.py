"""PC-SEND-2A production coordinator/SQLite/sender regressions, no native I/O."""
import asyncio
import ctypes
from datetime import UTC, datetime, timedelta
from uuid import uuid4
import sqlite3

import pytest
from pydantic import ValidationError
from TikTokLive.events import CommentEvent

from app.ai.models import ReplySuggestion
from app.delivery.journal import SQLiteDeliveryOperationStore
from app.delivery.models import AutoDeliveryEligibility, ManualDeliveryInput
from app.delivery.suggestions import durable_delivery_key
from app.live.listener import comment_from_event
from app.live.models import LiveStatus
from app.windows.native_input import NativeInput
from app.windows.service import WindowsManualDeliveryService
from app.windows.target import TargetRejected
from test_predelivery_hardening import suggestion
from test_windows_sender import (service, FakeNative, arm, until, echo, configuration,
    delivery_body, new_approved)

pytestmark = [pytest.mark.windows, pytest.mark.live_studio]


@pytest.fixture(autouse=True)
def no_native(monkeypatch):
    def forbidden(*args, **kwargs): pytest.fail('Real native I/O forbidden')
    monkeypatch.setattr(ctypes, 'WinDLL', forbidden)
    monkeypatch.setattr(NativeInput, '_load', forbidden)


def body(e, a, key=None):
    return ManualDeliveryInput(suggestion_id=e.suggestion_id, expected_suggestion_revision=e.suggestion_revision,
        approval_id=a.approval_id, idempotency_key=key or uuid4().hex)


async def accept(service, *, one_shot=False):
    """Acceptance through the real send/observe/confirm methods, not seeded state."""
    s, n, r = service
    if one_shot:
        cfg = s.sender.config.model_copy(update={'acceptance_one_shot': True})
        s.sender.config = cfg
        s.settings = s.settings.model_copy(update={'windows': cfg})
    await arm(s.sender, n)
    ids = iter(str(v) for v in range(8000, 8100))
    n.on_enter = lambda: s.observe_comment(echo(s.sender, message_id=next(ids)), r.context.live_session_id, 'own')
    assert (await s._execute(delivery_body(r), 'send_once')).status == 'confirmed'
    assert s.sender.composer == 'UNKNOWN'
    s.sender.confirm_empty()
    assert s.sender.empty_after_verified_confirmed
    return s, n, r


@pytest.mark.parametrize('phase', ['ARMED', 'FILLING', 'SENDING'])
async def test_focus_loss_inside_real_sql_preflight_blocks_next_emission(service, phase):
    s, n, r = service
    await arm(s.sender, n)
    s.prepare()
    lookup, changed = s.journal.lookup, False
    def race(key):
        nonlocal changed
        row = lookup(key)
        if row and row.status == 'reserved' and s.sender.state == phase and not changed:
            changed = True
            n.lose_focus()  # Same synchronous latch as the native hook thread.
        return row
    s.journal.lookup = race
    result = await s._execute(delivery_body(r), 'send_once' if phase == 'SENDING' else 'fill')
    assert changed and result.status in {'unknown', 'needs_operator'}
    assert n.enters == 0
    if phase != 'SENDING': assert n.batches == []
    assert s.sender.target is None and s.paused


async def test_final_native_guard_observes_pending_cancellation(service):
    s, n, r = service
    await arm(s.sender, n)
    n.before_emit = lambda: asyncio.current_task().cancel()
    with pytest.raises(asyncio.CancelledError): await s._execute(delivery_body(r), 'fill')
    assert s.journal.recent()[0].status == 'unknown'
    assert n.batches == [] and s.sender.target is None


@pytest.mark.parametrize('when,offset', [('after_arm', -4), ('after_enter', -4), ('after_enter', 4)])
async def test_clock_jump_with_stale_unseen_sdk_echo_never_verifies(service, when, offset):
    s, n, r = service
    move = [0]
    s.sender.wall_clock = lambda: datetime.now(UTC) + timedelta(seconds=move[0])
    await arm(s.sender, n)
    old_time = int(datetime.now(UTC).timestamp()) - 1
    def old_echo():
        move[0] = offset
        event = CommentEvent().from_dict({'user': {'id': '123', 'displayId': 'own'},
            'common': {'msgId': '777001', 'createTime': str(old_time)}, 'content': s.sender.pending['text']})
        # Installed SDK contract; server time is old, receipt is newly received.
        event.common.create_time = old_time
        c = comment_from_event(event).model_copy(update={'timestamp': s.sender.wall_clock()})
        s.observe_comment(c, r.context.live_session_id, 'own')
    n.on_enter = old_echo
    if when == 'after_arm': move[0] = offset
    result = await s._execute(delivery_body(r), 'send_once')
    assert result.status == 'unknown' and s.sender.clock_verification == 'unstable'
    assert s.sender.verification != 'VERIFIED' and s.paused
    assert n.enters == (0 if when == 'after_arm' else 1)
    before = len(n.batches)
    assert (await s._execute(delivery_body(r), 'send_once')).status == 'unknown'
    assert len(n.batches) == before


@pytest.mark.parametrize('bad', ['same_second', 'missing_time', 'receipt_before'])
async def test_clock_bridge_keeps_strong_echo_requirements(service, bad):
    s, n, r = service
    await arm(s.sender, n)
    def receipt():
        c = echo(s.sender)
        if bad == 'same_second': c = c.model_copy(update={'server_created_at': s.sender.pending['wall'].replace(microsecond=0)})
        elif bad == 'missing_time': c = c.model_copy(update={'server_created_at': None})
        else: c._received_monotonic = s.sender.pending['mono'] - .001
        s.observe_comment(c, r.context.live_session_id, 'own')
    n.on_enter = receipt
    assert (await s._execute(delivery_body(r), 'send_once')).status == 'unknown'
    assert n.enters == 1 and not s.sender.auto_enabled and s.sender.target is None


async def test_stable_clock_bridge_and_explicit_composer_acceptance(service):
    s, n, _ = await accept(service)
    assert s.sender.clock_verification == 'stable' and n.enters == 1


async def test_old_approved_backlog_never_crosses_auto_epoch_and_arm(service):
    s, n, r = await accept(service)
    old = [await new_approved(r, message_id=str(i)) for i in (124, 125)]
    s.set_auto(True)
    watermark = s.sender.auto_watermark
    assert all(not s.auto_eligibility(e.suggestion_id).eligible for e, _ in old)
    s.start_workflow(empty_confirmed=True)
    await until(lambda: len(n.watches) == 2 and n.watches[-1].started)
    n.focus(); await s.arm_task
    await asyncio.sleep(.02)
    assert n.enters == 1 and s.sender.auto_watermark == watermark
    assert len(s.journal.recent()) == 1  # Only the manual acceptance operation.
    e, _ = await new_approved(r, '126')
    assert e.auto_send_eligible is False  # AI cannot grant authority.
    assert s.auto_eligibility(e.suggestion_id).eligible  # Separate trusted policy.
    s.approved(e.suggestion_id)
    await until(lambda: not s.auto_pending)
    assert n.enters == 2 and s.journal.recent()[0].mode == 'AUTO'


@pytest.mark.parametrize('outcome', ['confirmed', 'timeout', 'partial'])
async def test_one_shot_auto_consumes_budget_before_enter_and_leaves_second_unused(service, outcome):
    s, n, r = await accept(service, one_shot=True)
    s.set_auto(True)
    fresh = [await new_approved(r, str(i)) for i in (124, 125)]
    if outcome == 'timeout': n.on_enter = None
    elif outcome == 'partial':
        def partial(): raise TargetRejected('partial_input')
        n.on_enter = partial
    for e, _ in fresh: s.approved(e.suggestion_id)
    await until(lambda: not s.auto_pending)
    assert n.enters == 2  # One manual acceptance + at most one AUTO Enter.
    assert s.sender.acceptance_spent and not s.sender.auto_enabled and s.sender.target is None
    assert s.sender.state == 'DISARMED'
    assert s.journal.has_envelope_operation(fresh[0][0])
    assert not s.journal.has_envelope_operation(fresh[1][0])
    assert len(s.journal.recent()) == 2
    with pytest.raises(TargetRejected): s.set_auto(True)
    await arm(s.sender, n)  # A new ARM never replenishes the process budget.
    assert s.sender.acceptance_spent


async def test_new_epoch_rejects_old_queue_and_old_approvals(service):
    s, n, r = await accept(service)
    s.set_auto(True)
    first_epoch = s.sender.auto_epoch_id
    e, a = await new_approved(r)
    assert s.auto_eligibility(e.suggestion_id).eligible
    s.set_auto(False); s.set_auto(True)
    assert s.sender.auto_epoch_id != first_epoch
    assert s.auto_eligibility(e.suggestion_id).reason == 'before_epoch'
    assert (await s._execute(body(e, a), 'auto', auto_epoch_id=first_epoch)).status == 'not_sent'
    assert n.enters == 1
    current_epoch = s.sender.auto_epoch_id
    s.set_auto(True)
    assert s.sender.auto_epoch_id == current_epoch
    s.live_state(LiveStatus(username='own', mode='real', state='reconnecting'))
    assert not s.sender.auto_enabled and s.sender.auto_epoch_id == current_epoch
    assert not s.auto_eligibility(e.suggestion_id).eligible


async def test_epoch_and_watermark_are_published_together(service):
    s, n, r = await accept(service)
    observed = []
    s.on_idle = lambda: observed.append(s.sender.get_status())
    s.set_auto(True)
    assert observed and all(w.auto_epoch_id == s.sender.auto_epoch_id and
        w.auto_watermark == r.store.sequence and w.auto_epoch_state == 'ACTIVE' for w in observed)


async def test_trusted_auto_decision_is_rechecked_during_production_preflight(service, monkeypatch):
    s, n, r = await accept(service)
    s.set_auto(True)
    e, a = await new_approved(r)
    original = s.auto_eligibility
    def revoked(suggestion_id, **kwargs):
        if kwargs.get('operation'): return AutoDeliveryEligibility(reason='manual_ineligible')
        return original(suggestion_id, **kwargs)
    monkeypatch.setattr(s, 'auto_eligibility', revoked)
    assert (await s._execute(body(e, a), 'auto', auto_epoch_id=s.sender.auto_epoch_id)).status == 'needs_operator'
    assert n.enters == 1  # Only the preceding manual acceptance.
    ai_values = {name: getattr(r.e, name) for name in ReplySuggestion.model_fields}
    assert ReplySuggestion.model_validate(ai_values).auto_send_eligible is False
    with pytest.raises(ValidationError, match='auto_send_eligible'):
        ReplySuggestion.model_validate(ai_values | {'auto_send_eligible': True})


@pytest.mark.parametrize('outcome', ['filled_only', 'unknown', 'reserved'])
@pytest.mark.parametrize('legacy', [False, True])
async def test_new_service_runtime_session_same_sdk_comment_remains_consumed(suggestion, tmp_path, outcome, legacy):
    r = suggestion
    cfg = configuration(enable_physical_windows_send=True)
    path = tmp_path / 'restart.sqlite3'
    n = FakeNative(cfg.windows)
    s = WindowsManualDeliveryService(cfg, r.store, lambda: r.context, native=n,
        journal_factory=lambda: SQLiteDeliveryOperationStore(path))
    try:
        await arm(s.sender, n)
        result = await s._execute(delivery_body(r), 'fill' if outcome == 'filled_only' else 'send_once')
        assert result.reason == 'filled_only' if outcome == 'filled_only' else result.status == 'unknown'
        if outcome == 'reserved':
            s.journal.connection.execute("UPDATE operations SET status='reserved',reason=NULL")
    finally: await s.aclose()
    if legacy:
        db = sqlite3.connect(path)
        db.execute('DROP INDEX durable_delivery_key_unique')
        db.execute('ALTER TABLE operations DROP COLUMN durable_delivery_key')
        db.commit(); db.close()
    e, a = await new_approved(r, '123', session=uuid4().hex, runtime=uuid4().hex)
    assert e.comment_identity != r.e.comment_identity and e.durable_delivery_key == r.e.durable_delivery_key
    r.context = r.context.model_copy(update={'live_session_id': e.live_session_id, 'active_runtime_revision': e.active_runtime_revision})
    n2 = FakeNative(cfg.windows)
    second = WindowsManualDeliveryService(cfg, r.store, lambda: r.context, native=n2,
        journal_factory=lambda: SQLiteDeliveryOperationStore(path))
    try:
        second.start_workflow(empty_confirmed=True, body=body(e, a), mode='send_once')
        await until(lambda: bool(n2.watches) and n2.watches[-1].started)
        n2.focus(); await second.arm_task
        assert n2.batches == [] and second.journal.has_envelope_operation(e)
        rows = second.journal.recent()
        assert len(rows) == 1 and rows[0].status == ('reserved' if outcome == 'reserved' else 'not_sent' if outcome == 'filled_only' else 'unknown')
        assert rows[0].durable_delivery_key is None if legacy else rows[0].durable_delivery_key == e.durable_delivery_key
    finally:
        await second.aclose(); await second.aclose()


def test_durable_key_account_source_and_weak_fallback_scope():
    assert durable_delivery_key('tiktok', 'Own', '123') == durable_delivery_key('tiktok', 'own', '123')
    assert durable_delivery_key('tiktok', 'other', '123') != durable_delivery_key('tiktok', 'own', '123')
    assert durable_delivery_key('replay', 'own', '123') is None
    assert durable_delivery_key('tiktok', 'own', None) is None


async def test_two_real_journal_connections_new_session_keys_race_only_one_types(suggestion, tmp_path):
    r = suggestion
    cfg = configuration(enable_physical_windows_send=True)
    e, a = await new_approved(r, '123', session=uuid4().hex, runtime=uuid4().hex)
    ctx2 = r.context.model_copy(update={'live_session_id': e.live_session_id, 'active_runtime_revision': e.active_runtime_revision})
    path = tmp_path / 'race.sqlite3'
    n1, n2 = FakeNative(cfg.windows), FakeNative(cfg.windows)
    first = WindowsManualDeliveryService(cfg, r.store, lambda: r.context, native=n1,
        journal_factory=lambda: SQLiteDeliveryOperationStore(path))
    second = WindowsManualDeliveryService(cfg, r.store, lambda: ctx2, native=n2,
        journal_factory=lambda: SQLiteDeliveryOperationStore(path))
    try:
        await arm(first.sender, n1); await arm(second.sender, n2)
        first.prepare(); second.prepare()  # Both connections open before either reserves.
        results = await asyncio.gather(first._execute(delivery_body(r), 'fill'), second._execute(body(e, a), 'fill'))
        assert sorted(v.reason for v in results) == ['filled_only', 'rejected']
        assert bool(n1.batches) != bool(n2.batches)
        assert len(first.journal.recent()) == len(second.journal.recent()) == 1
        assert not first.journal.connection.in_transaction and not second.journal.connection.in_transaction
    finally:
        await first.aclose(); await second.aclose()


async def test_one_shot_cancel_after_enter_never_replenishes_budget(service):
    s, n, r = await accept(service, one_shot=True)
    s.set_auto(True)
    e, a = await new_approved(r)
    n.on_enter = None
    task = asyncio.create_task(s._execute(body(e, a), 'auto', auto_epoch_id=s.sender.auto_epoch_id))
    await until(lambda: n.enters == 2)
    assert not s.sender.auto_enabled and s.sender.acceptance_spent
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert s.journal.recent()[0].status == 'unknown' and s.sender.target is None
    assert s.sender.state == 'DISARMED'


async def test_same_sdk_id_other_account_and_fallback_do_not_false_dedup(service):
    s, n, r = service
    await arm(s.sender, n)
    assert (await s._execute(delivery_body(r), 'fill')).reason == 'filled_only'
    e, _ = await new_approved(r, '123', account='other')
    assert not s.journal.has_envelope_operation(e)
    from app.live.models import Comment
    from app.delivery.suggestions import comment_identity
    weak = Comment(username='viewer', text='same weak input')
    first, kind = comment_identity(weak, 'tiktok', 'a'*32, 'own')
    second, _ = comment_identity(weak, 'tiktok', 'b'*32, 'own')
    assert kind == 'fingerprint' and first != second
