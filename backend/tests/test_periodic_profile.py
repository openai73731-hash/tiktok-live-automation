import asyncio
from datetime import UTC, datetime
import json
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.ai.generic import GenericReplyEngine, process_generic
from app.ai.periodic import PeriodicScheduler, SlotStore
from app.ai.profile import AIProfile
from app.main import create_app
from app.live.models import Comment
from app.delivery.models import SuggestionEnvelope
from app.delivery.automatic import AutomaticEligibility
from test_android_outgoing_auto import Phone, captured, fill, arm_auto, unknown, acknowledge
from test_mobile_account_challenge import SDK, OWN, until
from test_generic_profile import save_apply


@pytest.fixture
async def periodic_api(settings,tmp_path,monkeypatch,request):
    options = getattr(request,'param','templates')
    mode = options.get('mode','templates') if isinstance(options,dict) else options
    one_shot = not (isinstance(options,dict) and options.get('continuous'))
    phone,clients,calls=Phone(),[],[]
    executable=tmp_path/'adb.exe';executable.touch()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',AsyncMock(side_effect=phone.spawn))
    def sdk(**kwargs):
        assert kwargs['unique_id']==OWN
        client=SDK();clients.append(client);return client
    monkeypatch.setattr('app.live.listener.create_sdk_client',sdk)
    monkeypatch.setattr('app.android.outgoing.SDK_WAIT_SECONDS',.02)
    def endpoint(request):
        if request.method=='GET':return httpx.Response(200,json={'data':[{'id':'qwen/qwen3-8b'}]})
        data=json.loads(request.content);calls.append(data)
        user=json.loads(data['messages'][1]['content']);periodic=user['purpose']=='PERIODIC'
        context=user['KNOWLEDGE_CONTEXT']
        draft=dict(detected_language='en',operator_translation='Периодический комментарий' if periodic else 'Вопрос зрителя',
            should_reply=True,reply=('What would you like to discuss?' if isinstance(options,dict) and options.get('question')
                else 'Clear explanations help keep the topic focused.') if periodic else 'The workshop closes at six.',
            reply_translation='Понятные объяснения помогают сосредоточиться на теме.' if periodic else 'Мастерская закрывается в шесть.',
            basis='general' if periodic or not context else 'knowledge',context_ids=[context[0]['id']] if context and not periodic else [])
        if periodic:draft={k:v for k,v in draft.items() if k in {'reply','reply_translation','basis','context_ids'}}
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(draft)}}]})
    def get_client(self):
        if self._client is None:self._client=httpx.AsyncClient(base_url=self._base_url+'/',transport=httpx.MockTransport(endpoint),trust_env=False,follow_redirects=False)
        return self._client
    monkeypatch.setattr(GenericReplyEngine,'_get_client',get_client)
    cfg=settings.model_copy(update=dict(tiktok_username=OWN,tiktok_transport='android',adb_executable=executable,
        state_dir=tmp_path/'state',log_dir=tmp_path/'logs',enable_physical_tiktok_send=True,
        enable_physical_windows_send=False,mobile_acceptance_one_shot=one_shot))
    app=create_app(cfg)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as http:
            http.headers['X-Admin-CSRF']=app.state.admin.csrf_token
            p=AIProfile.model_validate({'topic':'Local workshop','periodic_comments':{'enabled':True,'interval_seconds':60,'jitter_seconds':0,'mode':mode}})
            await http.post('/api/admin/knowledge/text',json={'title':'Hours','text':'The workshop closes at six.'})
            await save_apply(app,http,p)
            r=app.state.operator_runtime;s=app.state.delivery
            assert (await http.post('/api/admin/delivery/prepare',json={})).status_code==200
            assert (await http.post('/api/admin/device/verify-account',json={})).status_code==200
            phone.current=captured('host')
            assert (await http.post('/api/admin/live/connect',json={'username':OWN})).status_code==202
            await until(lambda:s.account_proof.valid())
            s.controller.confirmation_interval=0
            # Disable only the timer; each test advances the same production tick explicitly.
            await r.periodic.aclose()
            now=[100.]
            r.periodic.clock=lambda:now[0]
            m=SimpleNamespace(app=app,http=http,phone=phone,clients=clients,service=s,runtime=r,config=cfg,calls=calls,now=now)
            yield m


async def manual_accept(m):
    e,_=await fill(m)
    m.clients[-1].comment(e.suggested_reply,identity='3001')
    result=await m.http.post('/api/admin/delivery/verify-fill',json={})
    assert result.json()=={'status':'confirmed','reason':'sdk_echo_confirmed'}
    m.phone.current=captured('host')


async def due(m):
    await m.runtime.periodic.tick()
    m.now[0]=m.runtime.periodic.due
    await m.runtime.periodic.tick()


@pytest.mark.parametrize('periodic_api',['templates','ai','mixed'],indirect=True)
async def test_periodic_uses_existing_auto_and_sdk_echo_without_viewer_ai_loop(periodic_api):
    m=periodic_api
    await manual_accept(m);await arm_auto(m)
    before=len(m.calls)
    m.phone.after_send=lambda text:m.clients[-1].comment(text,identity='3002')
    await due(m)
    p=m.runtime.periodic
    assert m.phone.opened==2 and m.phone.sent==1
    assert p.last_result=='confirmed' and p.last_send
    assert m.service.workflow=='SENT CONFIRMED' and m.service.mode=='MANUAL' and m.service.acceptance_spent
    mode=m.runtime.project.ai_profile.periodic_comments.mode
    assert len(m.calls)-before in ({0,1} if mode=='mixed' else {1} if mode=='ai' else {0})
    record=m.app.state.admin.live_history[0]
    assert record.source=='periodic' and record.suggestion.message_purpose=='PERIODIC'
    e=m.app.state.admin.suggestions.get(record.id)
    assert e.comment_identity_kind=='periodic_slot' and e._source_message_id is None
    assert m.service.journal.lookup('auto_'+record.id).reason=='sdk_echo_confirmed'
    from app.delivery.registry import UnifiedDeliveryOperationStore
    restarted=UnifiedDeliveryOperationStore(m.config.state_dir)
    assert restarted.has_envelope_operation(e) and restarted.lookup('auto_'+record.id).status=='confirmed'
    restarted.close()
    forged=SuggestionEnvelope.model_validate(e.model_dump(mode='json'))
    assert not AutomaticEligibility.evaluate(forged,m.service.auto_context()).eligible


async def test_ack_old_unknown_allows_only_a_fresh_periodic_slot(periodic_api):
    m=periodic_api
    _,_,old,before=await unknown(m)
    # Historical settled MANUAL rejections coexist with the ACKed UNKNOWN in
    # real journals. Seed metadata, never replace the production stores/guards.
    db=m.service.journal.connection
    rejected=dict(db.execute('SELECT * FROM operations WHERE operation_id=?',(old.operation_id,)).fetchone())
    rejected.update(operation_id=uuid4().hex,idempotency_key='old_manual_rejected',suggestion_id=uuid4().hex,
        comment_identity='a'*64,durable_delivery_key='b'*64,approval_id=uuid4().hex,
        status='needs_operator',reason='rejected',mode='MANUAL')
    db.execute('INSERT INTO operations ('+','.join(rejected)+') VALUES ('+','.join('?' for _ in rejected)+')',tuple(rejected.values()))
    rejected_before=tuple(db.execute('SELECT * FROM operations WHERE operation_id=?',(rejected['operation_id'],)).fetchone())
    await acknowledge(m);await arm_auto(m)
    m.phone.after_send=lambda text:m.clients[-1].comment(text,identity='3002')
    await due(m)
    assert m.runtime.periodic.last_result=='confirmed' and m.phone.sent==1
    row=tuple(m.service.journal.connection.execute('SELECT * FROM operations WHERE operation_id=?',(old.operation_id,)).fetchone())
    assert row==before and m.service.journal.lookup('auto_'+m.app.state.admin.live_history[0].id).status=='confirmed'
    assert tuple(db.execute('SELECT * FROM operations WHERE operation_id=?',(rejected['operation_id'],)).fetchone())==rejected_before
    assert (await m.http.post(f'/api/admin/suggestions/{old.suggestion_id}/approve',json={'expected_suggestion_revision':old.suggestion_revision})).status_code==409


@pytest.mark.parametrize('periodic_api',['ai'],indirect=True)
async def test_auto_revocation_during_generation_cannot_send_or_catch_up(periodic_api,monkeypatch):
    m=periodic_api
    await manual_accept(m);await arm_auto(m)
    engine=m.runtime.live.processor.engine;original=engine.generic
    async def revoke(request):
        draft=await original(request)
        m.service.set_mode('MANUAL',m.runtime.live.session_id,m.runtime.active_runtime_revision)
        m.now[0]+=300
        return draft
    monkeypatch.setattr(engine,'generic',revoke)
    await due(m)
    assert m.phone.sent==0 and m.runtime.periodic.last_result=='context_changed'
    assert m.runtime.periodic.due>m.now[0]


@pytest.mark.parametrize('blocker',['disabled','no_live','auto_off','busy','unknown'])
async def test_periodic_blocked_slots_never_generate_or_catch_up(periodic_api,blocker):
    m=periodic_api
    if blocker=='unknown':await unknown(m)
    else:await manual_accept(m)
    p=m.runtime.periodic
    if blocker not in {'auto_off','unknown','disabled'}:await arm_auto(m)
    if blocker=='disabled':
        profile=m.runtime.project.ai_profile
        m.runtime.project=m.runtime.project.model_copy(update={'ai_profile':profile.model_copy(update={'periodic_comments':profile.periodic_comments.model_copy(update={'enabled':False})})})
    if blocker=='no_live':m.runtime.live.service.listener.connected.clear()
    if blocker=='busy':await m.service.lock.acquire()
    before=len(m.calls)
    await p.tick()
    if p.due is not None:
        m.now[0]=p.due+1000
        await p.tick()
        next_due=p.due
        await p.tick()
        assert p.due==next_due and p.due>m.now[0]
    assert len(m.calls)==before and m.phone.sent==0 and not p.store.path.exists()
    if blocker=='busy':m.service.lock.release()


async def test_ai_periodic_prompt_has_no_fake_viewer_and_knowledge_missing_is_empty(periodic_api):
    m=periodic_api
    processor=m.runtime.live.processor
    comment=Comment(username='scheduler',text='Internal periodic slot',source='periodic')
    result=await process_generic(processor,comment,purpose='PERIODIC')
    assert result.status=='ready'
    body=json.loads(m.calls[-1]['messages'][1]['content'])
    assert body['purpose']=='PERIODIC' and body['USER_COMMENT'] is None
    viewer=await processor.process(Comment(username='viewer',text='How late is the workshop open?'))
    assert viewer.status=='ready' and viewer.knowledge_ids
    body=json.loads(m.calls[-1]['messages'][1]['content'])
    assert body['purpose']=='VIEWER_REPLY' and body['USER_COMMENT']


def test_slot_store_limits_and_claim_survives_restart_without_text(tmp_path):
    store=SlotStore(tmp_path)
    assert store.claim('a'*32,'owner',1,10000)
    restarted=SlotStore(tmp_path)
    assert not restarted.claim('b'*32,'owner',1,10001)
    with pytest.raises(Exception):restarted.claim('a'*32,'owner',2,10002)
    store.finish('a'*32,'confirmed','f'*64)
    assert restarted.last_hash('owner')=='f'*64
    assert restarted.claim('c'*32,'owner',1,13601)


@pytest.mark.parametrize('offset',[-30,0,30])
async def test_interval_and_jitter_are_bounded_without_generation(periodic_api,offset):
    m=periodic_api;p=m.runtime.periodic
    cfg=m.runtime.project.ai_profile
    # Apply only scheduler config in a non-armed unit boundary, never an input action.
    m.runtime.project=m.runtime.project.model_copy(update={'ai_profile':cfg.model_copy(update={'periodic_comments':cfg.periodic_comments.model_copy(update={'interval_seconds':300,'jitter_seconds':30})})})
    p.jitter=lambda n:offset
    await p.tick()
    assert 270 <= p.due-m.now[0] <= 330
    assert m.phone.sent==0
