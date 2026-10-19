"""Real API, LiveService, processor, policy, SQLite and native adapter; fake SDK/OS only."""
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock
import xml.etree.ElementTree as ET

import httpx
import pytest

from app.android.composer_open import resolved_composer_open_point
from app.android.tiktok_ui import TIKTOK_YOU_4683 as PROFILE, rid, resolved_send_point
from app.delivery.journal import JournalConflict
from app.delivery.registry import UnifiedDeliveryOperationStore
from app.live.models import Comment
from app.main import create_app
from test_android_hierarchy_acquisition import FakeDevice, Process
from test_manual_send_rc import ACCOUNT, xml
from test_mobile_account_challenge import SDK, OWN, until

FIXTURES = Path(__file__).parent / 'fixtures/android'


def captured(name): return (FIXTURES / ('you_4683_open_' + name + '.xml')).read_text(encoding='utf-8')


class Phone(FakeDevice):
    def __init__(self):
        super().__init__([xml('profile').replace('@'+ACCOUNT, '@'+OWN)], activity='com.windukk.mod.icon.default')
        self.sent = self.opened = 0
        self.selected = False
        self.after_send = None
        self.stall = False
        self.space = False

    @property
    def current(self): return self.snapshots[0]

    @current.setter
    def current(self, value): self.snapshots = [value]

    async def spawn(self, *args, **kwargs):
        if 'input' not in args: return await super().spawn(*args, **kwargs)
        self.calls.append(args)
        command = args[args.index('input')+1]
        root = ET.fromstring(self.current)
        entry = next((n for n in root.iter() if n.get('resource-id') == rid('h4k')), None)
        if command == 'tap':
            point = tuple(map(int, args[-2:]))
            if point == resolved_composer_open_point(self.current, PROFILE):
                self.opened += 1
                if not self.stall: self.current = captured('menu')
            elif point == resolved_composer_open_point(self.current, PROFILE, menu=True):
                self.opened += 1
                self.current = captured('composer')
            else:
                assert point == resolved_send_point(self.current, PROFILE)
                self.sent += 1
                text = entry.get('text')
                self.current = captured('host')
                if self.after_send: self.after_send(text)
        elif command == 'text':
            entry.set('text', shlex.split(args[-1])[0].replace('%s', ' ') + (' ' if self.space else ''))
            self.selected = False
            for n in root.iter():
                if n.get('resource-id') in {rid('vp8'),rid('vpl')}:
                    for k in ('enabled','clickable','focusable'): n.set(k,'true')
            self.current = ET.tostring(root, encoding='unicode')
        elif command == 'keycombination': self.selected = True
        elif command == 'keyevent':
            entry.set('text', '' if self.selected else entry.get('text','')[:-1])
            self.selected = False
            self.current = ET.tostring(root, encoding='unicode')
        else: pytest.fail('Unreviewed native action')
        return Process()


@pytest.fixture
async def phone_api(settings, tmp_path, monkeypatch):
    phone, clients = Phone(), []
    executable = tmp_path / 'adb.exe'; executable.touch()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=phone.spawn))
    def sdk(**kwargs):
        assert kwargs['unique_id'] == OWN
        client = SDK(); clients.append(client); return client
    monkeypatch.setattr('app.live.listener.create_sdk_client', sdk)
    monkeypatch.setattr('app.android.outgoing.SDK_WAIT_SECONDS', .02)
    cfg = settings.model_copy(update=dict(tiktok_username=OWN, tiktok_transport='android', adb_executable=executable,
        state_dir=tmp_path/'state', log_dir=tmp_path/'logs', enable_physical_tiktok_send=True,
        enable_physical_windows_send=False, mobile_acceptance_one_shot=True))
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as http:
            http.headers['X-Admin-CSRF'] = app.state.admin.csrf_token
            m = SimpleNamespace(app=app,http=http,phone=phone,clients=clients,service=app.state.delivery,
                runtime=app.state.operator_runtime,config=cfg)
            assert (await http.post('/api/admin/delivery/prepare',json={})).status_code == 200
            assert (await http.post('/api/admin/device/verify-account',json={})).status_code == 200
            phone.current = captured('host')
            assert (await http.post('/api/admin/live/connect',json={'username':OWN})).status_code == 202
            await until(lambda: m.service.account_proof.valid())
            m.service.controller.confirmation_interval = 0
            yield m


async def reply(m, identity='2001', *, approve=True):
    count = len(m.app.state.admin.live_history)
    m.clients[-1].comment('Is it automatic?',author='external_viewer',identity=identity)
    await until(lambda: len(m.app.state.admin.live_history) > count)
    record = m.app.state.admin.live_history[0]
    e = m.app.state.admin.suggestions.get(record.id)
    assert e.status == 'ready'
    if not approve: return e, None
    approved = await m.http.post(f'/api/admin/suggestions/{e.suggestion_id}/approve',json={'expected_suggestion_revision':e.suggestion_revision})
    assert approved.status_code == 200, approved.text
    return e, dict(suggestion_id=e.suggestion_id,expected_suggestion_revision=e.suggestion_revision,
        approval_id=approved.json()['approval']['approval_id'],idempotency_key='manual_'+identity)


async def fill(m, identity='2001'):
    e, body = await reply(m, identity)
    m.phone.current = captured('host')
    assert (await m.http.post('/api/admin/device/inspect',json={})).json()['inspection']['live_verified']
    m.phone.current = captured('composer')
    result = await m.http.post('/api/admin/delivery/fill',json=body)
    assert result.json() == {'status':'filled','reason':'filled_waiting_user_send'}, result.text
    return e, body


async def unknown(m):
    e, body = await fill(m)
    m.service.outgoing.pending.expires = m.service.outgoing.pending.created  # Bounded receipt expiry, never a fake confirmation.
    result = await m.http.post('/api/admin/delivery/verify-fill',json={})
    assert result.json()['status'] == 'unknown' and m.service.paused
    op = m.service.journal.lookup(body['idempotency_key'])
    before = tuple(m.service.journal.connection.execute('SELECT * FROM operations WHERE operation_id=?',(op.operation_id,)).fetchone())
    return e, body, op, before


async def acknowledge(m):
    m.phone.current = xml('profile').replace('@'+ACCOUNT,'@'+OWN)
    assert (await m.http.post('/api/admin/device/verify-account',json={})).status_code == 200
    m.phone.current = captured('host')
    status = (await m.http.get('/api/admin/delivery/status')).json()
    body = dict(confirmed=True,expected_unknown_revision=status['unknown_ack_revision'],
        expected_live_session_id=m.runtime.live.session_id,expected_active_runtime_revision=m.runtime.active_runtime_revision)
    response = await m.http.post('/api/admin/delivery/acknowledge-unknown',json=body)
    assert response.status_code == 200, response.text
    assert not response.json()['paused'] and response.json()['auto_state'] == 'AUTO OFF'
    return body


async def arm_auto(m):
    root = ET.fromstring(captured('composer'))
    # Operator reviews the control with a harmless draft, then clears it. On
    # the real empty composer vpl is not clickable until text has been entered.
    for n in root.iter():
        if n.get('resource-id') == rid('h4k'): n.set('text','Reviewed draft')
        if n.get('resource-id') in {rid('vp8'),rid('vpl')}:
            for k in ('enabled','clickable','focusable'): n.set(k,'true')
    m.phone.current = ET.tostring(root,encoding='unicode')
    status = (await m.http.post('/api/admin/device/inspect',json={})).json()
    token = status['inspection']['send_confirmation_token']
    assert token, [(k,status['inspection'][k]) for k in ('screen','reason','account_verified','live_verified','send_control_state')]
    result = await m.http.post('/api/admin/device/confirm-send',json={'token':token,'confirmed':True})
    assert result.status_code == 200, result.text
    m.phone.current = captured('host')
    assert (await m.http.post('/api/admin/device/inspect',json={})).json()['physical_ready'] is False
    result = await m.http.post('/api/admin/delivery/mode',json={'mode':'AUTOMATIC',
        'expected_live_session_id':m.runtime.live.session_id,'expected_active_runtime_revision':m.runtime.active_runtime_revision})
    assert result.status_code == 200, result.text
    assert result.json()['auto_state'] == 'AUTO ARMED'


async def test_manual_real_sdk_echo_confirms_without_host_feed(phone_api):
    m = phone_api
    e, body = await fill(m)
    assert m.phone.sent == 0
    assert (await m.http.post('/api/admin/delivery/verify-fill',json={'sdk_echo_confirmed':True})).status_code == 422
    m.clients[-1].comment(e.suggested_reply,identity='3001')
    response = await m.http.post('/api/admin/delivery/verify-fill',json={})
    assert response.json() == {'status':'confirmed','reason':'sdk_echo_confirmed'}
    assert m.service.workflow == 'SENT CONFIRMED' and m.service.account_proof.manual_accepted
    assert len(m.app.state.admin.live_history) == 1  # Own echo is intercepted before AI.
    assert m.service.journal.lookup(body['idempotency_key']).status == 'confirmed'


@pytest.mark.parametrize('bad',['foreign','case','space','old_time','missing_time','duplicate','no_sdk','old_generation','old_runtime','pre_boundary','consumed'])
async def test_echo_rejects_untrusted_stale_or_reused_receipts(phone_api,bad):
    m = phone_api
    e, _ = await fill(m)
    p = m.service.outgoing.pending
    if bad == 'duplicate': m.service.outgoing.seen.add((OWN,'3001'))
    if bad == 'old_generation': p.scope = (*p.scope[:4],p.scope[4]+1)
    if bad == 'old_runtime': p.scope = (p.scope[0],'f'*32,*p.scope[2:])
    if bad == 'pre_boundary': p.sequence += 100
    if bad == 'consumed':
        from app.android.delivery_evidence import claim_echo
        claim_echo(m.service,OWN,'3001','a'*32)
    text = e.suggested_reply.lower() if bad == 'case' else e.suggested_reply + ' ' if bad == 'space' else e.suggested_reply
    if bad == 'no_sdk':
        forged = Comment(username=OWN,author_handle=OWN,text=text,message_id='3001',server_created_at=datetime.now(UTC))
        forged._sdk_text_exact = True
        m.runtime.ingestion.bind(forged,OWN,p.scope[2],p.scope[1],None)
        assert not m.service.outgoing.observe(forged)
    elif bad == 'missing_time':
        comment = Comment(username=OWN,author_handle=OWN,text=text,message_id='3001')
        comment._sdk_receipt = (p.scope[3],p.scope[4])
        comment._sdk_text_exact = True
        m.runtime.ingestion.bind(comment,OWN,p.scope[2],p.scope[1],None)
        m.service.outgoing.observe(comment)
    else:
        m.clients[-1].comment(text,author='foreign' if bad == 'foreign' else OWN,identity='3001',
            created=datetime.now(UTC)-timedelta(seconds=60) if bad == 'old_time' else None)
    assert not p.event.is_set()


async def test_ack_keeps_unknown_bytes_consumed_and_allows_one_new_automatic_send(phone_api):
    m = phone_api
    old, old_body, op, before = await unknown(m)
    # A late perfect old echo must never reclassify the settled UNKNOWN.
    m.clients[-1].comment(old.suggested_reply,identity='3001')
    await acknowledge(m)
    assert not m.service.account_proof.manual_accepted  # ACK is distinct operator authority.
    assert (await m.http.post('/api/admin/delivery/fill',json=old_body)).status_code == 409
    assert (await m.http.post(f'/api/admin/suggestions/{old.suggestion_id}/approve',json={'expected_suggestion_revision':old.suggestion_revision})).status_code == 409
    m.phone.space = True
    m.phone.after_send = lambda text:m.clients[-1].comment(text,identity='3002')
    await arm_auto(m)
    fresh, _ = await reply(m,'2002',approve=False)
    await until(lambda: m.service.mode == 'MANUAL' and m.service.acceptance_spent)
    assert m.phone.opened == 2 and m.phone.sent == 1, (m.service.controller.last.screen.value,
        m.service.controller.last.reason,m.service.controller.last.account_verified,m.service.controller.last.live_verified,m.service.auto_reason)
    assert m.service.workflow == 'SENT CONFIRMED' and not m.service.paused
    latest = m.service.journal.lookup('auto_'+fresh.suggestion_id)
    assert latest.status == 'confirmed' and latest.reason == 'sdk_echo_confirmed'
    assert tuple(m.service.journal.connection.execute('SELECT * FROM operations WHERE operation_id=?',(op.operation_id,)).fetchone()) == before
    restarted = UnifiedDeliveryOperationStore(m.config.state_dir)
    assert restarted.has_envelope_operation(old) and restarted.lookup(old_body['idempotency_key']).status == 'unknown'
    restarted.close()
    # One-shot is spent; no second automatic activation or rearm.
    result = await m.http.post('/api/admin/delivery/mode',json={'mode':'AUTOMATIC',
        'expected_live_session_id':m.runtime.live.session_id,'expected_active_runtime_revision':m.runtime.active_runtime_revision})
    assert result.status_code == 409 and m.phone.sent == 1


async def test_unopened_composer_is_unknown_without_second_tap_or_input(phone_api):
    m = phone_api
    await unknown(m); await acknowledge(m); await arm_auto(m)
    m.phone.stall = True
    e, _ = await reply(m,'2002',approve=False)
    await until(lambda:m.service.paused)
    assert m.phone.opened == 1 and m.phone.sent == 0
    assert m.service.journal.lookup('auto_'+e.suggestion_id).status == 'unknown'


async def test_connection_generation_change_after_more_stops_before_second_tap(phone_api, monkeypatch):
    from app.android.adb import AdbCommand
    m = phone_api
    await unknown(m); await acknowledge(m); await arm_auto(m)
    m.phone.calls.clear()
    transport = m.service.controller.transport
    original = transport.run
    async def changed_generation(command, **kwargs):
        result = await original(command, **kwargs)
        if command == AdbCommand.TAP_COMPOSER:
            m.runtime.live.service.listener.connection_generation += 1
        return result
    monkeypatch.setattr(transport, 'run', changed_generation)
    e, _ = await reply(m, '2002', approve=False)
    await until(lambda: m.service.mode == 'MANUAL' and m.service.acceptance_spent)
    assert m.phone.opened == 1 and m.phone.sent == 0
    assert not any('text' in call for call in m.phone.calls)
    assert m.service.journal.lookup('auto_'+e.suggestion_id).status == 'unknown'


@pytest.mark.parametrize('failure',['revision','runtime','session','not_explicit','security','offline','pending','reserved','foreign','journal'])
async def test_ack_fails_closed_without_touching_unknown_or_one_shot(phone_api,failure):
    m = phone_api
    _, _, op, before = await unknown(m)
    m.phone.current = xml('profile').replace('@'+ACCOUNT,'@'+OWN)
    assert (await m.http.post('/api/admin/device/verify-account',json={})).status_code == 200
    m.phone.current = captured('host')
    if failure in {'reserved','foreign'}:
        row = dict(m.service.journal.connection.execute('SELECT * FROM operations WHERE operation_id=?',(op.operation_id,)).fetchone())
        row.update(operation_id='a'*32,idempotency_key='other',suggestion_id='b'*32,comment_identity='c'*64,
            durable_delivery_key='d'*64,status='reserved' if failure=='reserved' else 'unknown',
            target_account='foreign' if failure=='foreign' else OWN)
        m.service.journal.connection.execute('INSERT INTO operations ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
    status = (await m.http.get('/api/admin/delivery/status')).json()
    body = dict(confirmed=True,expected_unknown_revision=status['unknown_ack_revision'],
        expected_live_session_id=m.runtime.live.session_id,expected_active_runtime_revision=m.runtime.active_runtime_revision)
    if failure=='revision':body['expected_unknown_revision']='f'*64
    if failure=='runtime':body['expected_active_runtime_revision']='f'*32
    if failure=='session':body['expected_live_session_id']='f'*32
    if failure=='not_explicit':body['confirmed']=1
    if failure=='security':m.phone.current=m.phone.current.replace('Synthetic Host','security check')
    if failure=='offline':m.phone.offline=True
    if failure=='pending':m.service.pending_fill=(None,)
    if failure=='journal':(m.config.state_dir/'android-delivery-evidence.sqlite3').write_bytes(b'invalid sqlite')
    spent=m.service.acceptance_spent=True
    result=await m.http.post('/api/admin/delivery/acknowledge-unknown',json=body)
    assert result.status_code in {409,422}, result.text
    assert m.service.paused and m.service.mode=='MANUAL' and m.service.acceptance_spent is spent
    assert tuple(m.service.journal.connection.execute('SELECT * FROM operations WHERE operation_id=?',(op.operation_id,)).fetchone())==before
    assert m.phone.sent==0 and m.phone.opened==0
    if failure=='pending':m.service.pending_fill=None


async def test_ack_preserves_spent_latch_and_rejects_old_unconsumed_receipt(phone_api):
    m=phone_api
    await unknown(m)
    # This unused pre-ACK receipt also cannot be rejuvenated by a late approval.
    old,_=await reply(m,'2002',approve=False)
    m.service.acceptance_spent=True
    await acknowledge(m)
    assert m.service.acceptance_spent
    result=await m.http.post(f'/api/admin/suggestions/{old.suggestion_id}/approve',json={'expected_suggestion_revision':old.suggestion_revision})
    assert result.status_code==409 and m.phone.sent==0


async def test_clock_instability_is_sticky_for_late_perfect_echo(phone_api):
    m=phone_api
    e,_=await fill(m)
    p=m.service.outgoing.pending
    created=p.created
    p.created-=30
    m.clients[-1].comment(e.suggested_reply,identity='3001')
    assert p.invalid and not p.event.is_set()
    p.created=created
    m.clients[-1].comment(e.suggested_reply,identity='3002')
    assert not p.event.is_set()


async def test_echo_lookup_failure_cannot_become_confirmation(phone_api):
    m=phone_api
    e,_=await fill(m)
    (m.config.state_dir/'delivery.sqlite3').write_bytes(b'invalid authoritative journal')
    p=m.service.outgoing.pending
    m.clients[-1].comment(e.suggested_reply,identity='3001')
    assert p.invalid and not p.event.is_set() and m.phone.sent==0


@pytest.mark.parametrize('bad',['duplicate','class','disabled','outside','hidden','security','foreign','label','relationship'])
@pytest.mark.parametrize('menu',[False,True])
def test_reviewed_opener_rejects_ambiguous_or_unreviewed_geometry(bad,menu):
    root = ET.fromstring(captured('menu' if menu else 'host'))
    assert resolved_composer_open_point(ET.tostring(root,encoding='unicode'),PROFILE,menu=menu)
    target = next(n for n in root.iter() if n.get('resource-id') == rid('z_o') and n.get('text') == 'Оставить комментарий') if menu else next(n for n in root.iter() if n.get('resource-id') == rid('ttlive_toolbar_more'))
    if bad == 'duplicate': root.append(ET.fromstring(ET.tostring(target)))
    elif bad == 'class': target.set('class','android.widget.EditText')
    elif bad == 'disabled': target.set('enabled','false')
    elif bad == 'outside': target.set('bounds','[0,0][8192,8192]')
    elif bad == 'hidden': target.set('visible-to-user','false')
    elif bad == 'security': target.set('content-desc','security check')
    elif bad == 'foreign': target.set('package','foreign.app')
    elif bad == 'label': target.set('text','Foreign') if menu else target.set('resource-id',rid('other'))
    elif bad == 'relationship':
        parent = next(n for n in root.iter() if target in list(n)); parent.remove(target);root.append(target)
    assert resolved_composer_open_point(ET.tostring(root,encoding='unicode'),PROFILE,menu=menu) is None
