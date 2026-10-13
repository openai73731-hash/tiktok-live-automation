"""PC-SEND-2B: production pipeline/coordinator/SQLite, mocked OS and SDK transport."""
import asyncio
from contextlib import asynccontextmanager
import ctypes
from datetime import timedelta
import json
import os
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from TikTokLive.events import CommentEvent

from app.admin.service import record_for
from app.delivery.journal import SQLiteDeliveryOperationStore
from app.delivery.models import ApprovalInput, DeliveryRequest, DeliveryResult, ManualDeliveryInput, SuggestionEnvelope
from app.live.listener import LiveListener
from app.live.models import Comment, LiveStatus
from app.live.service import LiveService
from app.main import create_app
from app.windows.native_input import NativeInput
from app.windows.preparation import StartupPreparation
from test_predelivery_hardening import suggestion
from test_windows_sender import service, arm, until, echo, new_approved, configuration, FakeNative
from test_windows_hardening import accept, body

pytestmark = [pytest.mark.windows, pytest.mark.live_studio]


@pytest.fixture(autouse=True)
def no_native(monkeypatch):
    def forbidden(*a, **k): pytest.fail('Real Windows I/O forbidden')
    monkeypatch.setattr(ctypes, 'WinDLL', forbidden)
    monkeypatch.setattr(NativeInput, '_load', forbidden)


def sdk_comment(listener, message_id='124'):
    event = CommentEvent().from_dict({'user':{'nickname':'viewer'}, 'content':'Is it automatic?',
        'common':{'msgId':message_id}})
    listener.on_comment(event)


async def delayed_pipeline(s, r, settings):
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    generate = r.processor.engine.generate
    async def delayed(request):
        entered.set()
        await release.wait()
        return await generate(request)
    r.processor.engine.generate = delayed
    records, receipts = [], []
    async def process(comment):
        result = await r.processor.process(comment)  # real processor, safety and composition
        assert result.status == 'ready'
        e = r.store.register(record_for(comment, result, 'tiktok', 1, uuid4().hex), comment,
            r.processor, r.context.active_runtime_revision,
            session_id=r.context.live_session_id, target_account='own')
        records.append(e); completed.set()
    live = LiveService(settings.model_copy(update={'live_mode':'real', 'tiktok_username':'own'}), on_comment=process)
    def receipt(comment):
        comment.bind_ingestion('own', r.context.live_session_id, r.context.active_runtime_revision, '123')
        receipts.append(comment)
        s.observe_comment(comment, r.context.live_session_id, 'own')
    live.on_receipt = receipt
    consumer = asyncio.create_task(live._consume())
    return live, consumer, entered, release, completed, records, receipts, generate


@pytest.mark.parametrize('received_after', [False, True])
async def test_receipt_delayed_real_live_service_processor_epoch_and_arm(service, settings, received_after):
    s, n, r = await accept(service)
    live, consumer, entered, release, completed, records, receipts, generate = await delayed_pipeline(s, r, settings)
    baseline = len(n.batches), n.enters, len(s.journal.recent())
    try:
        if received_after: s.set_auto(True)
        sdk_comment(live.listener)
        await asyncio.wait_for(entered.wait(), 1)
        if not received_after: s.set_auto(True)
        release.set(); await asyncio.wait_for(completed.wait(), 1)
        e = records[0]
        a = r.store.approve(e.suggestion_id, ApprovalInput(expected_suggestion_revision=e.suggestion_revision), r.context)
        await arm(s.sender, n)  # fresh operator workflow after delayed AI/approval
        assert s.auto_eligibility(e.suggestion_id).eligible is received_after
        s.approved(e.suggestion_id)
        await until(lambda: not s.auto_pending)
        if received_after:
            assert n.enters == baseline[1] + 1 and s.journal.recent()[0].mode == 'AUTO'
        else:
            assert e._created_monotonic >= s.sender.auto_enabled_monotonic
            assert receipts[0]._received_monotonic <= s.sender.auto_enabled_monotonic
            assert (await s._execute(body(e, a), 'auto', auto_epoch_id=s.sender.auto_epoch_id)).status == 'not_sent'
            assert (len(n.batches), n.enters, len(s.journal.recent())) == baseline
            assert not s.journal.has_envelope_operation(e)  # ZERO reservation/Unicode/Enter
    finally:
        release.set(); consumer.cancel(); await asyncio.gather(consumer, return_exceptions=True)
        r.processor.engine.generate = generate


async def test_equal_monotonic_boundary_is_ordered_by_trusted_ingestion_sequence(service, monkeypatch):
    import app.live.models as models
    s, n, r = await accept(service)
    instant = s.sender.clock()
    monkeypatch.setattr(models, 'ingestion_boundary',
        lambda clock=None, *, receipt=False, original=models.ingestion_boundary: original(lambda: instant, receipt=receipt))
    s.sender.clock = lambda: instant
    old = Comment(username='viewer', text='Is it automatic?', message_id='124')
    old.bind_ingestion('own', r.context.live_session_id, r.context.active_runtime_revision, '123')
    s.set_auto(True)
    for comment, eligible in [(old, False), (Comment(username='viewer', text='Is it automatic?', message_id='125'), True)]:
        comment.bind_ingestion('own', r.context.live_session_id, r.context.active_runtime_revision, '123')
        result = await r.processor.process(comment)
        e = r.store.register(record_for(comment, result, 'tiktok', 1, uuid4().hex), comment,
            r.processor, r.context.active_runtime_revision, session_id=r.context.live_session_id, target_account='own')
        r.store.approve(e.suggestion_id, ApprovalInput(expected_suggestion_revision=e.suggestion_revision), r.context)
        assert comment._received_monotonic == s.sender.auto_enabled_monotonic
        assert s.auto_eligibility(e.suggestion_id).eligible is eligible


@pytest.mark.parametrize('wait', ['lock', 'cooldown', 'reconnect', 'runtime'])
async def test_old_receipt_cannot_gain_authority_after_wait_or_rebinding(service, settings, wait):
    s, n, r = await accept(service)
    old = Comment(username='viewer', text='Is it automatic?', message_id='124')
    old.bind_ingestion('own', r.context.live_session_id, r.context.active_runtime_revision, '123')
    s.set_auto(True)
    result = await r.processor.process(old)
    e = r.store.register(record_for(old, result, 'tiktok', 1, uuid4().hex), old, r.processor,
        r.context.active_runtime_revision, session_id=r.context.live_session_id, target_account='own')
    a = r.store.approve(e.suggestion_id, ApprovalInput(expected_suggestion_revision=e.suggestion_revision), r.context)
    baseline = len(n.batches), len(s.journal.recent())
    if wait in {'reconnect', 'runtime'}:
        s.sender.invalidate('runtime_changed' if wait == 'runtime' else 'live_not_connected')
        changes = {'active_runtime_revision' if wait == 'runtime' else 'live_session_id': uuid4().hex}
        r.context = r.context.model_copy(update=changes)
        with pytest.raises(ValueError, match='ingestion_binding_changed'):
            old.bind_ingestion('own', r.context.live_session_id, r.context.active_runtime_revision, '123')
        await arm(s.sender, n)
        s.sender.accepted_target = s.sender.target  # isolated renewal; receipt must still fail
        s.set_auto(True)
    if wait == 'cooldown': s.last_attempt = __import__('time').monotonic()
    await s.lock.acquire()
    task = asyncio.create_task(s._execute(body(e, a), 'auto', auto_epoch_id=s.sender.auto_epoch_id))
    await asyncio.sleep(.01)
    assert not task.done()
    s.lock.release()
    assert (await task).status == 'not_sent'
    assert (len(n.batches), len(s.journal.recent())) == baseline


@pytest.mark.parametrize('wait', ['lock', 'cooldown'])
async def test_queued_auto_loses_epoch_during_real_serial_wait_without_reservation(service, wait):
    from time import monotonic
    s, n, r = await accept(service)
    s.set_auto(True)
    e, a = await new_approved(r)
    epoch = s.sender.auto_epoch_id
    baseline = len(n.batches), len(s.journal.recent())
    if wait == 'lock': await s.lock.acquire()
    else:
        s.settings = s.settings.model_copy(update={'windows':s.settings.windows.model_copy(update={'cooldown_seconds':.1})})
        s.last_attempt = monotonic()
    task = asyncio.create_task(s._execute(body(e, a), 'auto', auto_epoch_id=epoch))
    await asyncio.sleep(.01)
    assert not task.done()
    s.set_auto(False); s.set_auto(True)
    if wait == 'lock': s.lock.release()
    assert (await task).status == 'not_sent'
    assert (len(n.batches), len(s.journal.recent())) == baseline


@pytest.mark.parametrize('good_echoes,cancel', [(1, False), (3, False), (2, True)])
@pytest.mark.parametrize('auto', [False, True])
async def test_clock_failure_then_perfect_echoes_is_terminal_in_coordinator_journal(service, good_echoes, cancel, auto):
    s, n, r = await accept(service, one_shot=auto)
    if auto:
        s.set_auto(True); r.e, r.approval = await new_approved(r)
    else:
        r.e, r.approval = await new_approved(r)
    observed = []
    s.sender.changed = lambda: observed.append(s.sender.get_status())
    def receipts():
        bad = echo(s.sender, message_id='9100')
        bad = bad.model_copy(update={'timestamp':bad.timestamp + timedelta(seconds=4)})
        assert s.sender.clock() < s.sender.pending['deadline']
        s.observe_comment(bad, r.context.live_session_id, 'own')
        assert s.sender.pending['clock_failed'] and not s.sender.auto_enabled
        for i in range(good_echoes):
            s.observe_comment(echo(s.sender, message_id=str(9200+i)), r.context.live_session_id, 'own')
        assert not s.sender.pending['verified']
        if cancel: asyncio.current_task().cancel()
    n.on_enter = receipts
    operation = s._execute(body(r.e, r.approval), 'auto' if auto else 'send_once', auto_epoch_id=s.sender.auto_epoch_id)
    if cancel:
        with pytest.raises(asyncio.CancelledError): await operation
    else:
        assert (await operation).status == 'unknown'
    assert s.journal.recent()[0].status == 'unknown'
    assert all(not (v.clock_verification == 'unstable' and v.verification == 'VERIFIED') for v in observed)
    assert s.sender.verification != 'VERIFIED' and s.sender.target is None and not s.sender.auto_enabled
    assert s.sender.state == 'DISARMED' and s.consumed(r.e) and s.view().delivery_uncertain
    if auto: assert s.sender.acceptance_spent and s.sender.get_status().acceptance_status == 'SPENT'
    baseline = len(n.batches)
    assert (await s._execute(body(r.e, r.approval), 'send_once')).status in {'not_sent', 'needs_operator'}
    assert len(n.batches) == baseline


@pytest.fixture
def windows_api_factory(settings, tmp_path, monkeypatch):
    # Real LiveService/owned consumer and real SDK adapter; no SDK client/network.
    async def transport(self):
        self.status = LiveStatus(username=self.settings.tiktok_username, mode='real', state='connected')
        if self.on_state: self.on_state(self.status)
        try: await asyncio.Event().wait()
        finally: self.status = self.status.model_copy(update={'cleanup_state':'complete'})
    monkeypatch.setattr(LiveListener, 'run', transport)
    config = settings.model_copy(update={'tiktok_transport':'windows_live_studio', 'tiktok_username':'d3pp1s',
        'windows':configuration().windows, 'state_dir':tmp_path/'state', 'log_dir':tmp_path/'logs'})
    return lambda: open_windows_api(config)


@asynccontextmanager
async def open_windows_api(config):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        # Isolate read-only preparation from the operator's ignored real artifacts.
        app.state.admin.preparation = StartupPreparation(config, root=config.project_config_path.parent)
        native = FakeNative(config.windows); app.state.delivery.sender.native = native
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as c:
            c.headers['X-Admin-CSRF'] = (await c.get('/api/admin/session')).json()['csrf_token']
            c.headers['Origin'] = 'http://127.0.0.1:8000'
            yield app, c, native


@pytest.fixture
async def windows_api(windows_api_factory):
    async with windows_api_factory() as value:
        yield value


@pytest.mark.parametrize('handle', ['d3pp1s', '@d3pp1s', ' @D3PP1S '])
async def test_api_connect_binds_normalized_own_account_and_receipt_scope(windows_api, handle):
    app, c, _ = windows_api
    runtime = app.state.operator_runtime
    response = await c.post('/api/admin/live/connect', json={'username':handle,
        'expected_active_runtime_revision':runtime.active_runtime_revision})
    assert response.status_code == 202
    await until(lambda: app.state.admin.live_status.state == 'connected')
    sdk_comment(runtime.live.service.listener)
    await until(lambda: app.state.admin.live_history)
    e = next(iter(app.state.admin.suggestions.groups['live'].values()))
    receipt = e._ingestion_receipt
    assert receipt.account == e.target_account == app.state.admin.delivery_context().target_account == 'd3pp1s'
    assert receipt.session_id == runtime.live.session_id and receipt.runtime_revision == runtime.active_runtime_revision
    assert receipt.broadcaster_user_id == '123' and receipt.message_id == '124'


@pytest.mark.parametrize('handle,stale', [('other', False), ('@OTHER', True), ('d3pp1s', True), ('d3pp1s', None)])
async def test_foreign_or_stale_connect_rejected_before_any_runtime_mutation(windows_api, handle, stale):
    app, c, n = windows_api
    runtime = app.state.operator_runtime
    before = (runtime.active_runtime_revision, runtime.username, runtime.live.session_id,
        runtime.live.processor, runtime.live.task, app.state.admin.history_session)
    payload = {'username':handle}
    if stale is not None: payload['expected_active_runtime_revision'] = uuid4().hex if stale else runtime.active_runtime_revision
    response = await c.post('/api/admin/live/connect', json=payload)
    assert response.status_code == 409
    assert before == (runtime.active_runtime_revision, runtime.username, runtime.live.session_id,
        runtime.live.processor, runtime.live.task, app.state.admin.history_session)
    assert not n.watches and not n.batches


async def test_browser_cannot_supply_ingestion_or_epoch_authority(windows_api):
    app, c, _ = windows_api
    for field in ['_ingestion_receipt', 'received_monotonic', 'ingestion_sequence', 'session_id', 'auto_epoch_id']:
        response = await c.post('/api/admin/live/connect', json={'username':'d3pp1s', field:1})
        assert response.status_code == 422
        response = await c.post('/api/admin/windows/auto', json={'enabled':True, field:1})
        assert response.status_code == 422
    assert app.state.operator_runtime.live.service is None


@pytest.mark.parametrize('replace_runtime', [False, True])
async def test_sdk_replay_after_full_reconnect_or_apply_keeps_first_ingestion(windows_api, replace_runtime):
    app, c, _ = windows_api
    runtime = app.state.operator_runtime
    async def connect():
        assert (await c.post('/api/admin/live/connect', json={'username':'d3pp1s',
            'expected_active_runtime_revision':runtime.active_runtime_revision})).status_code == 202
        await until(lambda: app.state.admin.live_status.state == 'connected')
        sdk_comment(runtime.live.service.listener)
        await until(lambda: app.state.admin.live_history)
        return next(reversed(app.state.admin.suggestions.groups['live'].values()))
    first = await connect()
    receipt = first._ingestion_receipt
    await runtime.live.disconnect(runtime.live.session_id)
    if replace_runtime:
        await runtime.apply(runtime.saved_config_revision, runtime.active_runtime_revision)
    second = await connect()
    assert second.live_session_id != first.live_session_id
    assert second._ingestion_receipt is None  # first receipt belongs to the retired session
    assert runtime.ingestion.receipts[('d3pp1s', '123', '124')] is receipt
    assert not app.state.delivery.auto_eligibility(second.suggestion_id).eligible


def test_ingestion_ledger_is_bounded_without_rejuvenating_eviction():
    from app.live.models import IngestionLedger
    ledger = IngestionLedger(capacity=1)
    for message_id, session in [('124', 'a'*32), ('125', 'a'*32), ('124', 'c'*32)]:
        comment = Comment(username='viewer', text='Is it automatic?', message_id=message_id)
        ledger.bind(comment, 'own', session, 'b'*32, '123')
        if message_id == '125': assert comment._ingestion_receipt is None
        else: assert comment._ingestion_receipt.session_id == 'a'*32
    assert len(ledger.receipts) == 1


async def test_private_receipt_never_round_trips_through_envelope_json(service):
    s, n, r = await accept(service)
    s.set_auto(True)
    e, _ = await new_approved(r)
    wire = e.model_dump(mode='json')
    assert not any('ingestion' in key or 'monotonic' in key or 'sequence' in key for key in wire)
    rebuilt = SuggestionEnvelope.model_validate(wire)
    assert rebuilt._ingestion_receipt is None and rebuilt._registered_auto_epoch_id is None
    from pydantic import ValidationError
    with pytest.raises(ValidationError): SuggestionEnvelope.model_validate(wire | {'_ingestion_receipt':e._ingestion_receipt})


async def test_apply_account_revision_revokes_old_arm_auto_and_connect_token(windows_api):
    app, c, _ = windows_api
    runtime, sender = app.state.operator_runtime, app.state.delivery.sender
    sender.auto_enabled = True
    sender.target = object()  # existing authority is revoked without native discovery
    before = runtime.active_runtime_revision
    view = (await c.get('/api/admin/operator/settings')).json()
    response = await c.post('/api/admin/operator/settings', json={'expected_saved_revision':view['saved_config_revision'],
        'settings':{'tiktok':{'username':'other'}}})
    assert response.status_code == 200
    view = response.json()
    response = await c.post('/api/admin/operator/apply', json={'expected_saved_revision':view['saved_config_revision'],
        'expected_active_runtime_revision':before})
    assert response.status_code == 200 and runtime.active_runtime_revision != before
    assert sender.target is None and not sender.auto_enabled and runtime.live.session_id is None
    assert not runtime.view().connect_allowed
    for handle in ['d3pp1s', 'other']:
        assert (await c.post('/api/admin/live/connect', json={'username':handle,
            'expected_active_runtime_revision':before})).status_code == 409


async def test_consumed_unknown_persists_across_read_only_service_restart(service, tmp_path):
    from app.windows.service import WindowsManualDeliveryService
    s, n, r = service
    # Point the injected real journal and the fixed production read-only location together.
    from app.delivery.journal import SQLiteDeliveryOperationStore
    s.settings = s.settings.model_copy(update={'state_dir':tmp_path})
    s.journal_factory = lambda: SQLiteDeliveryOperationStore(tmp_path/'windows-delivery.sqlite3')
    await arm(s.sender, n)
    assert (await s._execute(body(r.e, r.approval), 'send_once')).status == 'unknown'
    other = WindowsManualDeliveryService(s.settings, r.store, lambda:r.context, native=n)
    try:
        assert other.journal is None and other.consumed(r.e) and other.view().delivery_uncertain
        assert other.recent()[0].result == 'unknown' and other.journal is None
        r.store.approve(r.e.suggestion_id, ApprovalInput(expected_suggestion_revision=r.e.suggestion_revision), r.context)
        assert other.consumed(r.e)
    finally: await other.aclose()


async def test_owned_pending_verification_is_not_presented_as_unknown(service):
    s, n, r = service
    await arm(s.sender, n)
    def verified():
        assert not s.view().delivery_uncertain  # still-owned reservation, no unknown result
        s.observe_comment(echo(s.sender), r.context.live_session_id, 'own')
    n.on_enter = verified
    assert (await s._execute(body(r.e, r.approval), 'send_once')).status == 'confirmed'
    assert not s.view().delivery_uncertain


@pytest.mark.parametrize('changed', ['none', 'head', 'studio_pid', 'studio_start_time', 'backend_pid', 'backend_start_time', 'launch_id', 'PENDING', 'FAILED', 'CANCELLED'])
def test_prepared_projection_requires_current_revision_and_both_process_instances(tmp_path, monkeypatch, changed):
    import app.windows.preparation as module
    cfg = configuration(tiktok_username='d3pp1s', enable_physical_windows_send=True)
    cfg = cfg.model_copy(update={'windows':cfg.windows.model_copy(update={'process_id':99, 'acceptance_one_shot':True})})
    launch = 'a'*32; head = ['b'*40]
    monkeypatch.setenv('ACCEPTANCE_LAUNCH_ID', launch)
    monkeypatch.setattr(module, 'repository_head', lambda root:head[0])
    monkeypatch.setattr(module, 'process_identity', lambda pid: ('100', Path('C:/fixture/python.exe')) if pid == os.getpid() else ('200', cfg.windows.executable_path))
    projection = StartupPreparation(cfg, root=tmp_path)
    artifact = {'launch_id':launch, 'backend_launch_id':launch, 'result':'READY', 'git_head':head[0], 'git_branch':'main',
        'startup_configuration_revision':projection.configuration_revision,
        'studio_pid':99, 'studio_start_time':'200', 'backend_pid':os.getpid(), 'backend_start_time':'100',
        'backend_executable':'C:/fixture/python.exe', 'account':'d3pp1s'}
    artifact.update({key:True for key in ('auto_off', 'one_shot_available', 'physical_gate', 'numeric_uid_present')})
    artifact.update({key:'ready' for key in ('lmstudio_status', 'model_status', 'backend_health', 'admin_health')})
    if changed == 'head': head[0] = 'c'*40
    elif changed in {'PENDING', 'FAILED', 'CANCELLED'}: artifact['result'] = changed
    elif changed == 'launch_id': artifact['backend_launch_id'] = 'd'*32
    elif changed != 'none': artifact[changed] = 1 if changed.endswith('pid') else '999'
    directory = tmp_path/'work/acceptance/startup'; directory.mkdir(parents=True)
    (directory/'latest.json').write_text(json.dumps(artifact), encoding='utf-8')
    view = projection.view()
    assert (view.state == 'PRE-LIVE PREPARED') is (changed == 'none')
    if changed in {'FAILED', 'CANCELLED'}: assert view.state == 'PREPARATION FAILED'


async def test_admin_status_exposes_preparation_without_any_native_or_delivery_start(windows_api):
    app, c, n = windows_api
    response = await c.get('/api/admin/status')
    assert response.status_code == 200 and response.json()['preparation']['state'] == 'PRE-LIVE NOT PREPARED'
    assert not n.watches and not n.batches and app.state.delivery.journal is None


def test_browser_unknown_banner_consumed_actions_and_distinct_focus_state():
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node: pytest.skip('Optional Node regression')
    source = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),els=new Map();
const el=()=>({children:[],listeners:{},value:'',append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x},addEventListener(k,f){this.listeners[k]=f}});
const document={getElementById(id){if(!els.has(id))els.set(id,el());return els.get(id)}};
const window={confirm:()=>{throw Error('No attestation or action requested')}};
vm.runInNewContext(fs.readFileSync('backend/app/static/admin/delivery.js','utf8'),{window,document,crypto:require('crypto'),Date});
const status={transport:'windows_live_studio',delivery_uncertain:true,physical_delivery_enabled:true,
windows_sender:{state:'ARMING',watch_ready:true,watch_seconds:15,clock_verification:'unstable',verification:'VERIFIED',last_send_status:'SEND_UNKNOWN'}};
const api=async(path)=>path.endsWith('/history')?[{result:'unknown'}]:status;
const controls=window.createDeliveryControls({api,node:(_,__,s)=>Object.assign(el(),{textContent:s}),toast(){},state:{connected:true},refreshCards(){}});
(async()=>{
 await controls.refresh();
 assert(!document.getElementById('windows-unknown').hidden);
 assert(document.getElementById('windows-instruction').textContent.includes('ARM READY — CLICK COMPOSER NOW'));
 assert(document.getElementById('windows-instruction').className==='focus-instruction');
 const values=document.getElementById('device-status').children.map(r=>r.children[1].textContent);
 assert(!values.includes('VERIFIED'));assert(values.includes('UNAVAILABLE'));
 const review={consumed:true,approval_state:'approved',approval:{approval_id:'a'.repeat(32)},text_entry_supported:true,
 envelope:{source:'tiktok',status:'ready',suggestion_id:'b'.repeat(32),suggestion_revision:'c'.repeat(64),expires_at:new Date(Date.now()+60000).toISOString()}};
 const actions=el(),send=el();controls.attach(actions,{delivery_review:review},send);
 assert(send.disabled);assert(actions.children.find(x=>x.textContent==='Guarded send once').disabled);
 assert(actions.children.some(x=>x.textContent==='CONSUMED / NO RETRY'));
 status.windows_sender.state='DISARMED'; await controls.refresh();
 assert(!document.getElementById('windows-unknown').hidden); // refresh does not dismiss STOP
 assert(document.getElementById('windows-instruction').textContent.includes('WAITING FOR COMPOSER FOCUS'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, '-e', source], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


async def test_open_sse_tracks_current_artifact_through_all_preparation_states(windows_api, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import app.admin.router as router
    import app.windows.preparation as module
    app, _, native = windows_api
    cfg = configuration(tiktok_username='d3pp1s', enable_physical_windows_send=True)
    cfg = cfg.model_copy(update={'windows':cfg.windows.model_copy(update={'process_id':99, 'acceptance_one_shot':True})})
    monkeypatch.setenv('ACCEPTANCE_LAUNCH_ID', 'a'*32)
    monkeypatch.setattr(module, 'repository_head', lambda root:'b'*40)
    monkeypatch.setattr(module, 'process_identity', lambda pid:('100', Path('C:/fixture/python.exe')) if pid==os.getpid() else ('200',cfg.windows.executable_path))
    monkeypatch.setattr(router, 'PREPARATION_POLL_SECONDS', .01)
    projection = StartupPreparation(cfg, root=tmp_path)
    app.state.admin.preparation = projection
    artifact = dict(launch_id='a'*32, backend_launch_id='a'*32, result='READY', git_head='b'*40,
        git_branch='main', backend_pid=os.getpid(), backend_start_time='100', backend_executable='C:/fixture/python.exe',
        studio_pid=99, studio_start_time='200', account='d3pp1s', startup_configuration_revision=projection.configuration_revision)
    artifact.update({key:True for key in ('auto_off','one_shot_available','physical_gate','numeric_uid_present')})
    artifact.update({key:'ready' for key in ('lmstudio_status','model_status','backend_health','admin_health')})
    directory=tmp_path/'work/acceptance/startup'; directory.mkdir(parents=True)
    def publish():
        temporary=directory/'next.tmp'
        temporary.write_text(json.dumps(artifact),encoding='utf-8')
        temporary.replace(directory/'latest.json')
    publish()
    # last-ready alone is never authority, including during a cancelled attempt.
    (directory/'last-ready.json').write_text(json.dumps(artifact),encoding='utf-8')
    request=SimpleNamespace(app=app,is_disconnected=AsyncMock(return_value=False))
    response=await router.events(request)
    stream=response.body_iterator
    try:
        assert 'PRE-LIVE PREPARED' in await anext(stream)
        for index, result in enumerate(['PENDING','CANCELLED','FAILED','READY']):
            artifact.update(launch_id=str(index+1)*32,result=result)
            publish()
            async with asyncio.timeout(1):
                while True:
                    frame=await anext(stream)
                    if 'event: preparation_status' in frame: break
            data=json.loads(frame.split('data: ',1)[1])
            assert data['result']==result and data['launch_id']==artifact['launch_id']
            assert (data['state']=='PRE-LIVE PREPARED') is (result=='READY')
        assert not native.watches and not native.batches and app.state.delivery.journal is None
    finally:
        await stream.aclose()
    assert not app.state.admin.events.subscribers


@pytest.mark.parametrize('field,value', [('broadcaster_user_id','456'), ('process_id',55), ('acceptance_one_shot',True)])
def test_startup_fingerprint_changes_with_physical_binding(field, value, monkeypatch):
    cfg=configuration()
    monkeypatch.delenv('ACCEPTANCE_LAUNCH_ID',raising=False)
    before=StartupPreparation(cfg)
    changed=cfg.model_copy(update={'windows':cfg.windows.model_copy(update={field:value})})
    assert StartupPreparation(changed).configuration_revision != before.configuration_revision


def test_same_open_browser_updates_preparation_without_reload():
    import shutil
    import subprocess
    node=shutil.which('node')
    if not node: pytest.skip('Optional Node regression')
    source=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),elements=new Map(),timers=[];
function el(){return {value:'',textContent:'',children:[],listeners:{},classList:{toggle(){}},append(...v){this.children.push(...v)},prepend(...v){this.children.unshift(...v)},replaceChildren(...v){this.children=v},addEventListener(k,f){this.listeners[k]=f},setAttribute(){},removeAttribute(){},querySelector(){return el()}}}
const document={getElementById(id){if(!elements.has(id))elements.set(id,el());return elements.get(id)},createElement:el,querySelectorAll(){return []}};
const status={preparation:{state:'PRE-LIVE PREPARED',result:'READY'},started_at:new Date().toISOString(),live:{state:'stopped',activity:'inactive',enabled:false,live_session_id:null},ai:{provider:'fake',state:'available'},metrics:{processed:0,ready:0,skipped:0,needs_operator:0},reply_policy:'focused',temperature:0};
const delivery={transport:'windows_live_studio',windows_sender:{state:'DISARMED'},physical_delivery_enabled:true};
let stream,loads=0;
class EventSource{constructor(){stream=this;this.listeners={};this.readyState=1;loads++}addEventListener(n,f){this.listeners[n]=f}close(){}}
const window={addEventListener(){},confirm(){throw Error('No actions')},createQualityLab:()=>({controls(){},active(){return false},refresh:async()=>{},apply(){}}),createOperatorControls:()=>({controls(){},render(){}})};
const context=vm.createContext({window,document,EventSource,console,location:{hash:''},crypto:require('crypto'),Date,setTimeout,clearTimeout,setInterval(f){timers.push(f)},fetch:async(path)=>{let v=path==='/api/admin/status'?status:path==='/api/admin/settings'?{provider:'fake',reply_policy:'focused'}:path==='/api/admin/session'?{csrf_token:'fixture'}:path==='/api/admin/delivery/status'?delivery:path==='/api/admin/operator/settings'?{}:[];return {ok:true,json:async()=>JSON.parse(JSON.stringify(v))}}});
for(const name of ['live-feed.js','delivery.js','app.js'])vm.runInContext(fs.readFileSync('backend/app/static/admin/'+name,'utf8'),context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 await flush();await flush();assert.equal(document.getElementById('startup-preparation').textContent,'PRE-LIVE PREPARED');
 for(const result of ['PENDING','CANCELLED','READY','FAILED','READY']){
   const prepared=result==='READY';
   stream.listeners.preparation_status({data:JSON.stringify({state:prepared?'PRE-LIVE PREPARED':result==='PENDING'?'PRE-LIVE NOT PREPARED':'PREPARATION FAILED',result})});
   const label=document.getElementById('startup-preparation').textContent;
   assert.equal(label.includes('PRE-LIVE PREPARED'),prepared);
   if(!prepared)assert(label.includes(result));
 }
 stream.onerror();assert(!document.getElementById('startup-preparation').textContent.includes('PRE-LIVE PREPARED'));
 assert.equal(loads,1); // same document and SSE connection throughout
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result=subprocess.run([node,'-e',source],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


async def api_suggestion(app, client, message_id='124'):
    runtime = app.state.operator_runtime
    response = await client.post('/api/admin/live/connect', json={'username':'d3pp1s',
        'expected_active_runtime_revision':runtime.active_runtime_revision})
    assert response.status_code == 202
    await until(lambda: app.state.admin.live_status.state == 'connected')
    previous = set(app.state.admin.suggestions.groups['live'])
    sdk_comment(runtime.live.service.listener, message_id)
    await until(lambda: bool(set(app.state.admin.suggestions.groups['live']) - previous))
    return next(reversed(app.state.admin.suggestions.groups['live'].values()))


async def api_approve(client, envelope, **extra):
    return await client.post(f'/api/admin/suggestions/{envelope.suggestion_id}/approve',
        json={'expected_suggestion_revision':envelope.suggestion_revision, **extra})


def consume_api_suggestion(app, envelope, outcome):
    """Persist the production journal result without invoking any input path."""
    approval = app.state.admin.suggestions.approvals[envelope.suggestion_id]
    request = DeliveryRequest(operation_id=uuid4().hex, idempotency_key=uuid4().hex,
        suggestion_id=envelope.suggestion_id, suggestion_revision=envelope.suggestion_revision,
        comment_identity=envelope.comment_identity, durable_delivery_key=envelope.durable_delivery_key,
        source_kind=envelope.source, source_message_id=envelope._source_message_id,
        approval_id=approval.approval_id, suggested_reply_hash=envelope.suggested_reply_hash,
        text=envelope.suggested_reply, live_session_id=envelope.live_session_id,
        target_account=envelope.target_account, active_runtime_revision=envelope.active_runtime_revision,
        expires_at=envelope.expires_at)
    journal = SQLiteDeliveryOperationStore(app.state.admin.settings.state_dir/'windows-delivery.sqlite3')
    try:
        operation, created = journal.reserve(request)
        assert created
        if outcome != 'reserved':
            journal.finish(operation.operation_id, DeliveryResult(
                status='not_sent' if outcome == 'filled_only' else outcome,
                reason='filled_only' if outcome == 'filled_only' else 'echo_verified' if outcome == 'confirmed' else 'timeout'))
        return request
    finally:
        journal.close()


def approval_spies(app, monkeypatch):
    admin = app.state.admin
    spies = [Mock(wraps=method) for method in
        (admin.suggestions.approve, admin.events.publish, app.state.delivery.approved)]
    monkeypatch.setattr(admin.suggestions, 'approve', spies[0])
    monkeypatch.setattr(admin.events, 'publish', spies[1])
    monkeypatch.setattr(app.state.delivery, 'approved', spies[2])
    return spies


async def assert_consumed_approval_rejected(app, client, native, envelope, monkeypatch):
    admin = app.state.admin
    before = (dict(admin.suggestions.approvals), admin.suggestions.sequence)
    mutate, publish, approved = approval_spies(app, monkeypatch)
    response = await api_approve(client, envelope)
    assert response.status_code == 409
    assert response.json() == {'detail':'Suggestion already consumed and cannot be approved again.'}
    assert before == (admin.suggestions.approvals, admin.suggestions.sequence)
    mutate.assert_not_called()
    assert not any(call.args[0] == 'suggestion_approval' for call in publish.call_args_list)
    approved.assert_not_called()
    assert not native.watches and not native.batches
    review = (await client.get(f'/api/admin/suggestions/{envelope.suggestion_id}')).json()
    assert review['consumed'] is True
    assert app.state.delivery.journal is None  # Guard uses the existing read-only durable source.
    return review


@pytest.mark.parametrize('outcome', ['unknown', 'filled_only', 'confirmed', 'reserved'])
async def test_api_consumed_same_sdk_id_after_reconnect_rejects_before_approval(windows_api, monkeypatch, outcome):
    app, client, native = windows_api
    first = await api_suggestion(app, client)
    assert (await api_approve(client, first)).status_code == 200
    consume_api_suggestion(app, first, outcome)
    runtime = app.state.operator_runtime
    await runtime.live.disconnect(runtime.live.session_id)
    second = await api_suggestion(app, client)
    assert second.suggestion_id != first.suggestion_id and second.live_session_id != first.live_session_id
    assert second.comment_identity != first.comment_identity and second.durable_delivery_key == first.durable_delivery_key
    review = (await client.get(f'/api/admin/suggestions/{second.suggestion_id}')).json()
    assert review['consumed'] and review['approval_state'] == 'missing'
    review = await assert_consumed_approval_rejected(app, client, native, second, monkeypatch)
    assert review['approval_state'] == 'missing' and review['approval'] is None


async def test_api_fresh_unused_approval_still_publishes_and_notifies(windows_api, monkeypatch):
    app, client, native = windows_api
    envelope = await api_suggestion(app, client)
    mutate, publish, approved = approval_spies(app, monkeypatch)
    response = await api_approve(client, envelope)
    assert response.status_code == 200
    assert response.json()['approval_state'] == 'approved' and not response.json()['consumed']
    mutate.assert_called_once()
    events = [call for call in publish.call_args_list if call.args[0] == 'suggestion_approval']
    assert len(events) == 1 and events[0].args[1].suggestion_id == envelope.suggestion_id
    approved.assert_called_once_with(envelope.suggestion_id)
    assert not native.watches and not native.batches
    assert not (app.state.admin.settings.state_dir/'windows-delivery.sqlite3').exists()


async def test_api_consumed_existing_approval_is_not_refreshed(windows_api, monkeypatch):
    app, client, native = windows_api
    envelope = await api_suggestion(app, client)
    response = await api_approve(client, envelope)
    assert response.status_code == 200
    old = app.state.admin.suggestions.approvals[envelope.suggestion_id]
    old_binding = (old.model_dump(), old._approved_monotonic, old._approved_sequence)
    consume_api_suggestion(app, envelope, 'unknown')
    review = await assert_consumed_approval_rejected(app, client, native, envelope, monkeypatch)
    current = app.state.admin.suggestions.approvals[envelope.suggestion_id]
    assert current is old and old_binding == (current.model_dump(), current._approved_monotonic, current._approved_sequence)
    assert review['approval'] == response.json()['approval']


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('outcome', ['unknown', 'reserved'])
async def test_api_backend_restart_and_migrated_legacy_sdk_identity_stay_consumed(windows_api_factory, monkeypatch, legacy, outcome):
    import sqlite3
    async with windows_api_factory() as (app, client, native):
        first = await api_suggestion(app, client)
        assert (await api_approve(client, first)).status_code == 200
        consume_api_suggestion(app, first, outcome)
        path = app.state.admin.settings.state_dir/'windows-delivery.sqlite3'
        old_runtime = app.state.operator_runtime.active_runtime_revision
    if legacy:
        connection = sqlite3.connect(path)
        try:
            connection.execute('DROP INDEX durable_delivery_key_unique')
            connection.execute('ALTER TABLE operations DROP COLUMN durable_delivery_key')
            connection.commit()
        finally:
            connection.close()
        migrated = SQLiteDeliveryOperationStore(path)
        try: assert migrated.recent()[0].durable_delivery_key is None
        finally: migrated.close()
    async with windows_api_factory() as (app, client, native):
        assert app.state.operator_runtime.active_runtime_revision != old_runtime
        second = await api_suggestion(app, client)
        assert second.durable_delivery_key == first.durable_delivery_key and second.comment_identity != first.comment_identity
        review = await assert_consumed_approval_rejected(app, client, native, second, monkeypatch)
        assert review['approval_state'] == 'missing'


async def test_api_browser_body_cannot_override_consumed_identity(windows_api, monkeypatch):
    app, client, native = windows_api
    first = await api_suggestion(app, client)
    assert (await api_approve(client, first)).status_code == 200
    consume_api_suggestion(app, first, 'unknown')
    runtime = app.state.operator_runtime
    await runtime.live.disconnect(runtime.live.session_id)
    second = await api_suggestion(app, client)
    mutate, publish, approved = approval_spies(app, monkeypatch)
    for extra in ({'consumed':False}, {'approval_state':'approved'}, {'durable_delivery_key':None}):
        assert (await api_approve(client, second, **extra)).status_code == 422
    mutate.assert_not_called()
    approved.assert_not_called()
    assert not any(call.args[0] == 'suggestion_approval' for call in publish.call_args_list)
    await assert_consumed_approval_rejected(app, client, native, second, monkeypatch)


async def test_api_consumed_journal_read_failure_rejects_without_mutation(windows_api, monkeypatch):
    app, client, native = windows_api
    envelope = await api_suggestion(app, client)
    path = app.state.admin.settings.state_dir/'windows-delivery.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'invalid sqlite journal')
    await assert_consumed_approval_rejected(app, client, native, envelope, monkeypatch)
