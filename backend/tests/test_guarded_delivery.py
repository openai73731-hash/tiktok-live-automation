"""Stage4B4: synthetic captures, mock ADB, no real phone or provider calls."""
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import xml.etree.ElementTree as ET

import pytest

from app.android.adb import AdbCommand, AdbTransport, AdbError
from app.android.controller import ADBAndroidController
from app.android.feed import host_feed, newly_confirmed, normalized
from app.android.models import ScreenState
from app.android.tiktok_ui import TIKTOK_YOU_4683 as PROFILE, rid, inspect_hierarchy, resolved_send_point, unique_node
from app.delivery.automatic import AutomaticEligibility
from app.delivery.journal import SQLiteDeliveryOperationStore, JournalConflict
from app.delivery.models import ApprovalInput, ManualDeliveryInput, DeliveryResult
from app.delivery.service import ManualDeliveryService
from app.delivery.suggestions import SuggestionConflict
from test_predelivery_hardening import suggestion, panel, rehash
from test_manual_send_rc import Transport, xml, target, ACCOUNT, SERIAL

FIXTURES = Path(__file__).parent / 'fixtures/android'
SEND_ACTIONS = {AdbCommand.TAP_SEND, AdbCommand.TAB, AdbCommand.ACTIVATE}


def capture(name):
    return (FIXTURES / ('stage4b4_' + name + '.xml')).read_text(encoding='utf-8-sig')


def posted(text, author='fixture_host', header='fixture_host'):
    root = ET.fromstring(capture('live_host'))
    for node in root.iter():
        if node.get('resource-id') == rid('f41'): node.set('text', '\u200e' + text)
        if node.get('resource-id') == rid('poq'): node.set('text', author)
        if node.get('resource-id') == rid('user_name'): node.set('text', header)
    return ET.tostring(root, encoding='unicode')


@pytest.fixture(autouse=True)
def forbid_real_adb(monkeypatch):
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=AssertionError('No real ADB')))


@pytest.fixture
async def phone_factory(suggestion, settings, tmp_path):
    services = []
    async def make(*, gate=False, confirmed=True):
        r = suggestion
        r.context = r.context.model_copy(update={'target_account': ACCOUNT})
        e = rehash(r.e, target_account=ACCOUNT)
        r.store.groups['live'][e.suggestion_id] = e
        r.store.approvals.clear()
        approval = r.store.approve(e.suggestion_id, ApprovalInput(expected_suggestion_revision=e.suggestion_revision), r.context)
        dest = target(e.live_session_id)
        t = Transport()
        c = ADBAndroidController(t, physical_enabled=gate, confirmation_interval=0)
        assert await c.verify_account(dest)
        t.xml = capture('live_viewer')
        assert (await c.inspect(dest)).live_verified
        t.xml = xml('composer')
        state = await c.inspect(dest)
        if confirmed: assert await c.confirm_send(dest, state.send_confirmation_token)
        config = settings.model_copy(update={'enable_physical_tiktok_send': gate})
        service = ManualDeliveryService(config, r.store, lambda: r.context, controller=c,
            journal_factory=lambda: SQLiteDeliveryOperationStore(tmp_path / (uuid4().hex + '.sqlite3')))
        services.append(service)
        body = ManualDeliveryInput(suggestion_id=e.suggestion_id, expected_suggestion_revision=e.suggestion_revision,
            approval_id=approval.approval_id, idempotency_key='fixture_operation')
        return SimpleNamespace(service=service, c=c, t=t, e=e, r=r, body=body, target=dest)
    yield make
    for service in services: await service.aclose()


def arm(phone):
    phone.service.set_mode('AUTOMATIC', phone.e.live_session_id, phone.e.active_runtime_revision)
    authorization = phone.r.store.authorize_auto(phone.e.suggestion_id, phone.service.auto_context())
    phone.body = phone.body.model_copy(update={'approval_id': authorization.approval_id})


def confirm_after_tap(phone, *, author='fixture_host', header='fixture_host', reply=None):
    async def after(command):
        if command == AdbCommand.TAP_SEND:
            phone.t.xml = posted(phone.e.suggested_reply if reply is None else reply, author, header)
    phone.t.after = after


def test_new_minimal_fixtures_and_overlay_shape():
    normal = inspect_hierarchy(capture('live_viewer'), PROFILE.package, ACCOUNT, PROFILE, account_bound=True)
    overlay = inspect_hierarchy(capture('overlay'), PROFILE.package, ACCOUNT, PROFILE, account_bound=True)
    assert normal['screen'] == ScreenState.LIVE_READY and normal['live_verified']
    assert overlay['screen'] == ScreenState.TRANSIENT_OVERLAY and not overlay['live_verified']
    assert not overlay['security_state']
    assert host_feed(capture('live_viewer')).pairs == frozenset({('fixture_viewer', 'Synthetic question?')})
    assert host_feed(capture('live_host')).pairs == frozenset({('fixture_host', 'Synthetic approved reply.')})
    for name in ('live_viewer', 'live_host', 'overlay'):
        assert all(rid(n) not in capture(name) for n in ('h4k', 'vp8', 'vpl'))


@pytest.mark.parametrize('gate', [False, True])
@pytest.mark.parametrize('confirmed', [False, True])
async def test_manual_fill_zero_send_actions_without_send_confirmation(phone_factory, gate, confirmed):
    p = await phone_factory(gate=gate, confirmed=confirmed)
    p.t.calls.clear()
    result = await p.service.fill(p.body)
    assert result.status == 'filled' and result.reason == 'filled_waiting_user_send'
    assert unique_node(p.t.xml, rid('h4k')).get('text') == p.e.suggested_reply
    commands = [cmd for cmd, _ in p.t.calls]
    assert commands.count(AdbCommand.SELECT_ALL) == commands.count(AdbCommand.DELETE) == commands.count(AdbCommand.TEXT) == 1
    assert not set(commands) & SEND_ACTIONS and p.t.activations == 0
    assert p.service.workflow == 'FILLED — SEND ON PHONE'
    assert p.service.journal.lookup(p.body.idempotency_key).status == 'filled'
    assert (await p.service.fill(p.body)).reason == 'waiting_user_send'
    assert p.service.mode == 'MANUAL'


@pytest.mark.parametrize('screen', ['live_viewer', 'overlay'])
async def test_wait_for_phone_composer_without_reservation_or_action(phone_factory, screen):
    p = await phone_factory()
    p.t.xml = capture(screen); p.t.calls.clear()
    first = await p.service.fill(p.body)
    assert first.reason == ('open_phone_composer' if screen == 'live_viewer' else 'transient_overlay')
    assert p.service.journal is None and not p.service.paused
    assert not any(cmd in SEND_ACTIONS | {AdbCommand.TEXT, AdbCommand.DELETE} for cmd, _ in p.t.calls)
    # Same identity can proceed after the operator returns to reviewed normal LIVE/composer.
    p.t.xml = capture('live_viewer'); await p.c.inspect(p.target)
    p.t.xml = xml('composer')
    assert (await p.service.fill(p.body)).status == 'filled'


@pytest.mark.parametrize('value', ['Привет', 'café', '你好', '100% sure', 'line\nbreak'])
async def test_manual_unicode_requires_copy_and_never_mutates_phone(phone_factory, value):
    p = await phone_factory()
    e = rehash(p.e, suggested_reply=value)
    # Keep the operator-approved binding explicit; unsupported input is a separate guard.
    from app.delivery.suggestions import reply_hash
    e = rehash(e, suggested_reply_hash=reply_hash(value))
    p.r.store.groups['live'][e.suggestion_id] = e
    p.r.store.approvals.clear()
    a = p.r.store.approve(e.suggestion_id, ApprovalInput(expected_suggestion_revision=e.suggestion_revision), p.r.context)
    body = p.body.model_copy(update={'expected_suggestion_revision': e.suggestion_revision, 'approval_id': a.approval_id})
    p.t.calls.clear()
    assert (await p.service.fill(body)).reason == 'unicode_entry_unavailable'
    assert not p.t.calls and p.service.journal is None


@pytest.mark.parametrize('mismatch', ['blank', 'input'])
async def test_manual_exact_readbacks_fail_closed(phone_factory, mismatch):
    p = await phone_factory(); original = p.t.run
    async def run(command, **kwargs):
        result = await original(command, **kwargs)
        if mismatch == 'blank' and command == AdbCommand.DELETE:
            root = ET.fromstring(p.t.xml)
            next(n for n in root.iter() if n.get('resource-id') == rid('h4k')).set('text', 'old input')
            p.t.xml = ET.tostring(root, encoding='unicode')
        return result
    p.t.run = run; p.t.mismatch = mismatch == 'input'; p.t.calls.clear()
    assert (await p.service.fill(p.body)).status == 'needs_operator'
    assert not any(cmd in SEND_ACTIONS for cmd, _ in p.t.calls)
    if mismatch == 'blank': assert not any(cmd == AdbCommand.TEXT for cmd, _ in p.t.calls)
    else: assert unique_node(p.t.xml, rid('h4k')).get('text') == ''


@pytest.mark.parametrize('change', ['version', 'code', 'account', 'session', 'runtime', 'expiry', 'focus', 'duplicate_input'])
async def test_manual_binding_and_focus_guards(phone_factory, change):
    p = await phone_factory(); p.t.calls.clear()
    if change == 'version': p.t.info = p.t.info.replace('46.8.3','46.8.4')
    if change == 'code': p.t.info = p.t.info.replace('2024608030','2024608031')
    if change in {'account','session','runtime'}:
        key = {'account':'target_account','session':'live_session_id','runtime':'active_runtime_revision'}[change]
        p.r.context = p.r.context.model_copy(update={key:'other' if change == 'account' else 'd'*32})
    if change == 'expiry': p.e._monotonic_deadline = 0
    if change == 'focus': p.t.xml = p.t.xml.replace('focused="true"','focused="false"')
    if change == 'duplicate_input':
        root = ET.fromstring(p.t.xml); n = unique_node(p.t.xml, rid('h4k')); root.append(n)
        p.t.xml = ET.tostring(root, encoding='unicode')
    try: assert (await p.service.fill(p.body)).status != 'filled'
    except SuggestionConflict: pass
    assert not any(cmd in SEND_ACTIONS | {AdbCommand.TEXT, AdbCommand.DELETE} for cmd, _ in p.t.calls)


@pytest.mark.parametrize('change', ['duplicate','missing','zero_bounds','outside_parent','disabled','invisible','wrong_parent'])
def test_selector_resolution_requires_exact_unique_visible_relationship(change):
    root = ET.fromstring(xml('composer'))
    parent = next(n for n in root.iter() if n.get('resource-id') == rid('vp8'))
    child = next(n for n in root.iter() if n.get('resource-id') == rid('vpl'))
    if change == 'duplicate': root.append(ET.fromstring(ET.tostring(parent)))
    elif change == 'missing': parent.remove(child)
    elif change == 'zero_bounds': child.set('bounds','[0,0][0,0]')
    elif change == 'outside_parent': child.set('bounds','[0,0][20,20]')
    elif change == 'disabled': child.set('enabled','false')
    elif change == 'invisible': child.set('visible-to-user','false')
    elif change == 'wrong_parent': parent.remove(child); root.append(child)
    assert resolved_send_point(ET.tostring(root,encoding='unicode'), PROFILE) is None


def test_selector_point_tracks_node_bounds_and_retired_keyboard_commands_rejected(tmp_path):
    text = xml('composer'); first = resolved_send_point(text, PROFILE)
    assert first == (1005,1272)
    root = ET.fromstring(text)
    child = next(n for n in root.iter() if n.get('resource-id') == rid('vpl'))
    child.set('bounds','[950,1200][1000,1300]')
    assert resolved_send_point(ET.tostring(root,encoding='unicode'), PROFILE) == (975,1250)
    exe = tmp_path/'adb.exe'; exe.touch(); t = AdbTransport(exe)
    assert t.argv(AdbCommand.TAP_SEND, serial=SERIAL, point=first)[-3:] == ['tap','1005','1272']
    for command in (AdbCommand.TAB, AdbCommand.ACTIVATE):
        with pytest.raises(AdbError): t.argv(command,serial=SERIAL)
    for point in (None, (-1,2), (1,9000), ('1',2), (True,2)):
        with pytest.raises(AdbError): t.argv(AdbCommand.TAP_SEND,serial=SERIAL,point=point)


async def test_automatic_default_and_false_gate_no_activation(phone_factory):
    p = await phone_factory(); p.t.calls.clear()
    assert p.service.mode == 'MANUAL' and p.service.view().auto_state == 'AUTO OFF'
    with pytest.raises(SuggestionConflict): arm(p)
    assert (await p.service.send(p.body)).reason == 'physical_delivery_disabled'
    p.service.offer_auto(p.e.suggestion_id)
    assert not p.t.calls and p.t.activations == 0 and p.service.auto_runner is None


async def test_auto_one_action_and_exact_host_feed_confirmation(phone_factory):
    p = await phone_factory(gate=True); arm(p); confirm_after_tap(p); p.t.calls.clear()
    result = await p.service.send(p.body)
    assert result == DeliveryResult(status='confirmed',reason='feed_confirmed')
    assert p.t.activations == 1
    assert not any(cmd in {AdbCommand.TAB,AdbCommand.ACTIVATE} for cmd, _ in p.t.calls)
    assert p.service.recent()[0].mode == 'AUTOMATIC'
    assert not p.service.paused
    assert await p.service.send(p.body) == result and p.t.activations == 1


@pytest.mark.parametrize('kind', ['none','viewer','different_text','different_header','preexisting'])
async def test_auto_ambiguous_or_absent_evidence_never_retries(phone_factory, kind):
    p = await phone_factory(gate=True); arm(p)
    if kind == 'viewer': confirm_after_tap(p,author='fixture_viewer')
    if kind == 'different_text': confirm_after_tap(p,reply='Different exact reply.')
    if kind == 'different_header': confirm_after_tap(p,header='changed_host')
    if kind == 'preexisting': p.c._host_snapshot = host_feed(posted(p.e.suggested_reply))
    first = await p.service.send(p.body)
    if kind == 'preexisting':
        assert first.reason == 'confirmation_ambiguous' and p.t.activations == 0
    else:
        assert first.status == 'unknown' and p.service.paused and p.t.activations == 1
    assert await p.service.send(p.body) == first
    assert (await p.service.send(p.body.model_copy(update={'idempotency_key':'another'}))).status != 'confirmed'
    assert p.t.activations == (0 if kind == 'preexisting' else 1)


@pytest.mark.parametrize('value', ['Synthetic approved reply','synthetic approved reply.','Synthetic  approved reply.','Synthetic approved reply.!','prefix Synthetic approved reply.'])
def test_confirmation_never_fuzzy_matches(value):
    before = host_feed(capture('live_viewer')); after = host_feed(posted(value))
    assert not newly_confirmed(before, after, 'Synthetic approved reply.')


async def test_manual_optional_confirmation_is_read_only(phone_factory):
    p = await phone_factory(); assert (await p.service.fill(p.body)).status == 'filled'
    p.t.xml = posted(p.e.suggested_reply); p.t.calls.clear()
    assert (await p.service.verify_fill()).status == 'confirmed'
    assert not any(cmd in SEND_ACTIONS | {AdbCommand.TEXT,AdbCommand.DELETE} for cmd, _ in p.t.calls)
    assert p.service.journal.lookup(p.body.idempotency_key).status == 'confirmed'
    assert p.service.pending_fill is None


@pytest.mark.parametrize('change', ['translation','unknown','processing_error','unicode','proof','runtime','knowledge','policy','session','expired','source','duplicate'])
async def test_deterministic_auto_rejects_unsafe_or_stale(phone_factory, change):
    p = await phone_factory(gate=True); arm(p); e=p.e; ctx=p.service.auto_context(); duplicate=False
    if change=='translation': e=rehash(e,translation_requires_review=True)
    elif change=='unknown': e=rehash(e,intent='unknown')
    elif change=='processing_error': e=rehash(e,processing_error='provider_unavailable')
    elif change=='unicode': e=rehash(e,suggested_reply='café')
    elif change=='proof': e=rehash(e,suggested_reply='A fabricated claim.')
    elif change in {'runtime','knowledge','policy','session'}:
        key={'runtime':'active_runtime_revision','knowledge':'knowledge_revision','policy':'policy_revision','session':'live_session_id'}[change]
        ctx=ctx.model_copy(update={key:'d'*(32 if change in {'runtime','session'} else 64)})
    elif change=='expired': e._monotonic_deadline=0
    elif change=='source': e=rehash(e,source='playground')
    elif change=='duplicate': duplicate=True
    assert not AutomaticEligibility.evaluate(e,ctx,duplicate=duplicate).eligible
    assert p.t.activations == 0


@pytest.mark.parametrize('surface', ['overlay','security'])
async def test_transient_and_security_pre_send_fail_closed(phone_factory,surface):
    p=await phone_factory(gate=True); arm(p)
    if surface=='overlay': p.t.xml=capture('overlay')
    else: p.t.xml=p.t.xml.replace('text="Test"','text="security check"')
    # Security may be appended even when the old text differs.
    if surface=='security':
        root=ET.fromstring(p.t.xml); ET.SubElement(root,'node',{'package':PROFILE.package,'text':'security check'})
        p.t.xml=ET.tostring(root,encoding='unicode')
    result=await p.service.send(p.body)
    assert result.status in {'not_sent','needs_operator'} and p.t.activations==0
    if surface=='security': assert p.service.paused
    assert p.c.last.screen == (ScreenState.TRANSIENT_OVERLAY if surface=='overlay' else ScreenState.SECURITY_CHALLENGE)


async def test_auto_queue_and_concurrent_same_envelope_one_activation(phone_factory):
    p=await phone_factory(gate=True); arm(p); confirm_after_tap(p)
    one,two=await asyncio.gather(p.service.send(p.body),p.service.send(p.body))
    assert one==two and one.status=='confirmed' and p.t.activations==1
    p.service.offer_auto(p.e.suggestion_id)
    assert p.service.auto_runner is None and p.service.auto_reason=='duplicate_operation'


async def test_offer_auto_owned_runner_only_fresh_candidate(phone_factory):
    p=await phone_factory(gate=True); arm(p); confirm_after_tap(p)
    p.service.offer_auto(p.e.suggestion_id)
    await asyncio.wait_for(p.service.auto_queue.join(),2)
    await p.service.auto_runner
    assert p.t.activations==1 and p.service.recent()[0].result=='confirmed'
    p.service.view(); p.service.recent()
    assert p.t.activations==1


async def test_cooldown_and_stale_mode_binding(phone_factory):
    p=await phone_factory(gate=True); arm(p)
    p.service.last_attempt=__import__('time').monotonic()
    assert (await p.service.send(p.body)).reason=='cooldown' and p.t.activations==0
    p.r.context=p.r.context.model_copy(update={'live_session_id':'b'*32})
    p.service.context_changed()
    assert p.service.paused
    with pytest.raises(SuggestionConflict): p.service.set_mode('AUTOMATIC',p.e.live_session_id,p.e.active_runtime_revision)


async def test_restart_unknown_keeps_dedup_and_defaults_manual(phone_factory):
    p=await phone_factory(gate=True); arm(p)
    assert (await p.service.send(p.body)).status=='unknown'
    path=p.service.journal.path
    p.service.journal.close(); p.service.journal=None
    restarted=SQLiteDeliveryOperationStore(path)
    assert restarted.unresolved() and restarted.has_operation(p.e.suggestion_id,p.e.comment_identity)
    with pytest.raises(JournalConflict):
        from test_android_delivery import request
        restarted.reserve(request(p.r).model_copy(update={'idempotency_key':'new','mode':'AUTOMATIC'}))
    restarted.close()
    p.service.prepare()
    assert p.service.paused and p.t.activations==1


@pytest.mark.parametrize('extra', [{'enable_physical_tiktok_send':True},{'resource_id':'vp8'},{'text':'replace'},{'point':[1,2]}])
async def test_browser_cannot_supply_gate_selectors_or_text(panel,extra):
    app,client=panel
    status=(await client.get('/api/admin/delivery/status')).json()
    body={'mode':'AUTOMATIC','expected_live_session_id':status['live_session_id'],
        'expected_active_runtime_revision':status['active_runtime_revision']}
    assert (await client.post('/api/admin/delivery/mode',json=body|extra)).status_code==422
    assert (await client.post('/api/admin/delivery/mode',json=body)).status_code==409
    assert not app.state.delivery.settings.enable_physical_tiktok_send
    assert app.state.delivery.mode=='MANUAL'


async def test_new_routes_preserve_csrf_and_empty_body_contract(panel):
    _,client=panel
    for route in ('delivery/fill','delivery/mode','delivery/verify-fill'):
        assert (await client.post('/api/admin/'+route,json={},headers={'X-Admin-CSRF':'wrong'})).status_code==403
    assert (await client.post('/api/admin/delivery/verify-fill',json={'text':'forbidden'})).status_code==422


def test_stage4b3_journal_migration_preserves_reserved_and_unique_keys(tmp_path):
    path=tmp_path/'legacy.sqlite3'; db=sqlite3.connect(path)
    db.execute('''CREATE TABLE operations (operation_id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,
        suggestion_id TEXT NOT NULL UNIQUE,suggestion_revision TEXT NOT NULL,comment_identity TEXT NOT NULL UNIQUE,
        approval_id TEXT NOT NULL,reply_hash TEXT NOT NULL,live_session_id TEXT NOT NULL,target_account TEXT NOT NULL,
        runtime_revision TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN
        ('reserved','confirmed','not_sent','unknown','needs_operator')),reason TEXT,device_serial TEXT,mode TEXT NOT NULL DEFAULT 'MANUAL')''')
    values=('a'*32,'old','b'*32,'c'*64,'d'*64,'e'*32,'f'*64,'1'*32,'fixture_owner','2'*32,
        datetime.now(UTC).isoformat(),'reserved',None,SERIAL,'MANUAL')
    db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',values);db.commit();db.close()
    journal=SQLiteDeliveryOperationStore(path)
    assert journal.lookup('old').status=='reserved' and journal.unresolved()
    assert journal.has_operation('b'*32,'d'*64)
    assert "'filled'" in journal.connection.execute("SELECT sql FROM sqlite_master WHERE name='operations'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError): journal.connection.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values + (None,))
    journal.close()


async def test_distinct_suggestions_serialize_entire_physical_delivery(phone_factory, monkeypatch):
    from app.admin.service import record_for
    from app.live.models import Comment
    import app.delivery.service as delivery_module
    p = await phone_factory(gate=True); arm(p)
    comment = Comment(username='fixture_viewer_two', text='Does the software speak?', message_id='456')
    result = await p.r.processor.process(comment)
    second = p.r.store.register(record_for(comment, result, 'tiktok', 1, uuid4().hex), comment,
        p.r.processor, p.e.active_runtime_revision, session_id=p.e.live_session_id, target_account=ACCOUNT)
    auth = p.r.store.authorize_auto(second.suggestion_id, p.service.auto_context())
    body2 = p.body.model_copy(update={'suggestion_id':second.suggestion_id,
        'expected_suggestion_revision':second.suggestion_revision,'approval_id':auth.approval_id,'idempotency_key':'fixture_two'})
    entered, release = asyncio.Event(), asyncio.Event()
    original, entries, states = p.c.submit, [], []
    active = maximum = 0
    ticks = iter(range(0, 10000, 10))
    monkeypatch.setattr(delivery_module, 'monotonic', lambda: next(ticks))
    p.service.on_idle = lambda: states.append(p.service.workflow)
    async def submit(request, *, guard):
        nonlocal active, maximum
        active += 1; maximum = max(maximum, active); entries.append(request.text)
        try:
            if len(entries) == 1: entered.set(); await release.wait()
            else:
                # Simulate the operator reopening the composer; no opening click exists.
                p.c._host_snapshot = host_feed(posted(p.e.suggested_reply))
                p.t.xml = xml('composer')
            async def after(command):
                if command == AdbCommand.TAP_SEND: p.t.xml = posted(request.text)
            p.t.after = after
            return await original(request, guard=guard)
        finally: active -= 1
    monkeypatch.setattr(p.c, 'submit', submit)
    first = asyncio.create_task(p.service.send(p.body))
    await asyncio.wait_for(entered.wait(), 1)
    other = asyncio.create_task(p.service.send(body2)); await asyncio.sleep(0)
    assert len(entries) == 1 and p.t.activations == 0
    release.set()
    results = await asyncio.gather(first, other)
    assert all(r.status == 'confirmed' for r in results)
    assert maximum == 1 and p.t.activations == 2 and entries[0] != entries[1]
    assert 'POST_SEND_VERIFY' in states and len(p.service.recent()) == 2


@pytest.mark.parametrize('change', ['expiry', 'disarm', 'session'])
async def test_queued_candidate_rechecked_after_cooldown(phone_factory, change):
    from time import monotonic
    p = await phone_factory(gate=True); arm(p)
    p.service.last_attempt = monotonic() - p.service.settings.automatic_delivery_min_interval_seconds + 0.04
    p.service.offer_auto(p.e.suggestion_id)
    await asyncio.sleep(0)
    if change == 'expiry': p.e._monotonic_deadline = 0
    elif change == 'disarm': p.service.set_mode('MANUAL', p.e.live_session_id, p.e.active_runtime_revision)
    else:
        p.r.context = p.r.context.model_copy(update={'live_session_id':'b'*32})
        p.service.context_changed()
    await asyncio.wait_for(p.service.auto_queue.join(), 1)
    await asyncio.gather(p.service.auto_runner, return_exceptions=True)
    assert p.t.activations == 0 and p.service.auto_queue.empty()
    assert not p.service.journal.has_operation(p.e.suggestion_id, p.e.comment_identity)


async def test_auto_runner_unexpected_failure_pauses_and_discards_queue(phone_factory, monkeypatch):
    p = await phone_factory(gate=True); arm(p)
    def failure(*args): raise ValueError('must never reach UI')
    monkeypatch.setattr(p.r.store, 'authorize_auto', failure)
    p.service.offer_auto(p.e.suggestion_id)
    await asyncio.wait_for(p.service.auto_queue.join(), 1); await p.service.auto_runner
    assert p.service.paused and p.service.auto_reason == 'sender_failed' and p.t.activations == 0
    assert p.service.auto_queue.empty()


async def test_cancel_manual_read_only_verification_settles_unknown(phone_factory, monkeypatch):
    p = await phone_factory(); assert (await p.service.fill(p.body)).status == 'filled'
    entered = asyncio.Event()
    async def blocked(*args): entered.set(); await asyncio.Event().wait()
    monkeypatch.setattr(p.c, 'verify_post_send', blocked)
    task = asyncio.create_task(p.service.verify_fill())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel(); await asyncio.sleep(0); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    operation = p.service.journal.lookup(p.body.idempotency_key)
    assert operation.status == 'unknown' and p.service.pending_fill is None and p.service.paused
    assert p.t.activations == 0 and not p.service.tasks and not p.service.lock.locked()


@pytest.mark.parametrize('name', ['user_info_container','oyh','pov','poq','f41'])
def test_confirmation_rejects_disabled_reviewed_feed_nodes(name):
    root = ET.fromstring(capture('live_host'))
    next(n for n in root.iter() if n.get('resource-id') == rid(name)).set('enabled','false')
    assert not newly_confirmed(host_feed(capture('live_viewer')), host_feed(ET.tostring(root, encoding='unicode')),
        'Synthetic approved reply.')


def test_manual_browser_fill_gate_false_identity_only_and_mode_binding():
    node = shutil.which('node')
    assert node, 'Node required for Stage 4B.4 browser regression'
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),crypto=require('crypto');
const els=new Map(),el=()=>({children:[],listeners:{},value:'',append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x},addEventListener(k,f){this.listeners[k]=f}});
const document={getElementById(id){if(!els.has(id))els.set(id,el());return els.get(id)}},window={confirm:()=>true};
vm.runInNewContext(fs.readFileSync('backend/app/static/admin/delivery.js','utf8'),{window,document,crypto,setTimeout(){}});
const calls=[],messages=[],status={mode:'MANUAL',inspection:{devices:[]},physical_delivery_enabled:false,physical_ready:false,workflow:'READY',live_session_id:'1'.repeat(32),active_runtime_revision:'2'.repeat(32)};
const api=async(path,body)=>{calls.push([path,body]);return path.endsWith('/history')?[]:path.endsWith('/fill')?{status:'filled',reason:'filled_waiting_user_send'}:status};
const delivery=window.createDeliveryControls({api,node:(_,__,s)=>Object.assign(el(),{textContent:s}),toast:s=>messages.push(s),state:{connected:true},refreshCards(){}});
(async()=>{await delivery.refresh();
const record={delivery_review:{fill_allowed:true,text_entry_supported:true,approval_state:'approved',approval:{approval_id:'a'.repeat(32)},envelope:{suggestion_id:'b'.repeat(32),suggestion_revision:'c'.repeat(64),source:'tiktok',status:'ready',expires_at:new Date(Date.now()+60000).toISOString(),suggested_reply:'PRIVATE IMMUTABLE'}}};
const actions=el(),send=el();delivery.attach(actions,record,send);
const fill=actions.children.find(e=>e.textContent.includes('FILL PHONE'));
assert.strictEqual(fill.disabled,false);assert.strictEqual(send.hidden,true);assert(!send.listeners.click);
await fill.listeners.click();const body=calls.find(([p])=>p.endsWith('/fill'))[1];
assert.deepStrictEqual(Object.keys(body).sort(),['approval_id','expected_suggestion_revision','idempotency_key','suggestion_id']);assert(!JSON.stringify(body).includes('PRIVATE IMMUTABLE'));
assert(messages.includes('Ответ введён в телефон. Нажмите отправить на телефоне.'));
assert(!calls.some(([p])=>p.endsWith('/send')));
record.delivery_review.fill_allowed=false;record.delivery_review.text_entry_supported=false;
const unicode=el();delivery.attach(unicode,record,el());assert(unicode.children[0].disabled);assert(unicode.children[0].title.includes('COPY'));
document.getElementById('delivery-mode').value='AUTOMATIC';await document.getElementById('delivery-mode').listeners.change();
const mode=calls.find(([p])=>p.endsWith('/mode'))[1];assert.deepStrictEqual(Object.keys(mode).sort(),['expected_active_runtime_revision','expected_live_session_id','mode']);assert.strictEqual(mode.expected_live_session_id,status.live_session_id);assert.strictEqual(mode.mode,'AUTOMATIC');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


async def test_disarm_cannot_rearm_old_sleeping_candidate(phone_factory):
    from time import monotonic
    p = await phone_factory(gate=True); arm(p)
    p.service.last_attempt = monotonic()
    p.service.offer_auto(p.e.suggestion_id); await asyncio.sleep(0)
    p.service.set_mode('MANUAL', p.e.live_session_id, p.e.active_runtime_revision)
    with pytest.raises(SuggestionConflict):
        p.service.set_mode('AUTOMATIC', p.e.live_session_id, p.e.active_runtime_revision)
    await asyncio.gather(p.service.auto_runner, return_exceptions=True)
    p.service.set_mode('AUTOMATIC', p.e.live_session_id, p.e.active_runtime_revision)
    assert p.service.auto_queue.empty() and p.t.activations == 0


async def test_auto_view_blocks_when_phone_returns_to_normal_live(phone_factory):
    p = await phone_factory(gate=True); arm(p); confirm_after_tap(p)
    # Production context uses ready_for; emulate it instead of the fixture's static readiness.
    p.service.context = lambda: p.r.context.model_copy(update={'device_available':p.c.ready_for(p.e.live_session_id, ACCOUNT)})
    assert (await p.service.send(p.body)).status == 'confirmed'
    view = p.service.view()
    assert view.auto_state == 'AUTO BLOCKED' and view.auto_reason == 'device_unavailable'
    assert not view.physical_ready
