import base64
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.ai.profile import AIProfile, PeriodicSettings, ProfileModel
from app.ai.local_knowledge import KnowledgeStore, KnowledgeError, MAX_FILE
from app.ai.generic import GenericReplyEngine, GenericDraft, violations
from app.ai.models import ReplyRequest
from app.ai.factory import create_processor
from app.admin.operator_config import OperatorOverlay, effective
from app.ai.knowledge import ProjectConfig, load_yaml_model
from app.live.models import Comment
from app.main import create_app


@pytest.mark.parametrize('values',[{'name':''},{'system_prompt':'x'*3001},{'language':'English'},
    {'periodic_comments':{'interval_seconds':59}},{'periodic_comments':{'jitter_seconds':151}},
    {'periodic_comments':{'max_per_hour':61}},{'model':{'temperature':float('nan')}},
    {'model':{'base_url':'http://example.com/v1'}},{'model':{'base_url':'http://127.0.0.1:1234/v1/other'}},
    {'model':{'max_tokens':2049}},{'knowledge':{'top_k':9}},{'periodic_comments':{'templates':['']}}])
def test_profile_bounds_fail_closed(values):
    with pytest.raises(ValidationError): AIProfile.model_validate(values)


def test_profile_defaults_and_legacy_are_separate():
    base=load_yaml_model(Path('config/project.yaml'),ProjectConfig)
    assert base.ai_profile is None
    p=AIProfile()
    assert not p.periodic_comments.enabled
    assert (p.periodic_comments.interval_seconds,p.periodic_comments.jitter_seconds,p.periodic_comments.max_per_hour)==(300,30,12)
    applied=effective(base,OperatorOverlay(ai_profile=p))
    assert applied.ai.model==p.model.model and applied.ai.provider=='lmstudio'
    assert applied.product.name=='LIVE Assistant' and base.product.name!=applied.product.name


def test_local_fts_snapshot_apply_delete_and_missing_context(tmp_path):
    store=KnowledgeStore(tmp_path)
    assert not store.path.exists() and store.view()[0]['sources']==[]
    first=store.add('hours.md',b'Our showroom opens at nine. The showroom closes at six.')
    second=store.add('games.txt','We discuss cooperative gaming.'.encode())
    snapshot=store.snapshot()
    result=snapshot.retrieve('When does the showroom close?',3)
    assert result and all('showroom' in r['text'] for r in result)
    assert snapshot.retrieve('nonexistentxyzw',3)==()
    store.update(first,delete=True)
    assert snapshot.retrieve('showroom',3)  # Draft deletion cannot mutate active inference.
    applied=store.snapshot()
    assert applied.retrieve('showroom',3)==() and len(store.view()[0]['sources'])==1
    store.update(second,enabled=False)
    disabled=store.snapshot(); assert disabled.retrieve('gaming',3)==()
    assert snapshot.revision!=applied.revision!=disabled.revision
    for value in (snapshot,applied,disabled): value.close()


@pytest.mark.parametrize('name,raw',[('../private.txt',b'a'),('C:\\private.txt',b'a'),('/private.txt',b'a'),
    ('a.pdf',b'not pdf'),('a.docx',b'not docx'),('a.txt',b'\xff'),('a.txt',b'\x00'),('a.txt',b'x'*(MAX_FILE+1))],
    ids=['traversal','drive','absolute','pdf','docx','encoding','nul','oversized'])
def test_knowledge_rejects_paths_and_unsupported_or_oversized_data(tmp_path,name,raw):
    store=KnowledgeStore(tmp_path)
    with pytest.raises(KnowledgeError):store.add(name,raw)
    assert store.view()[0]['sources']==[]


@pytest.fixture
async def profile_api(settings,tmp_path):
    cfg=settings.model_copy(update={'state_dir':tmp_path/'state','log_dir':tmp_path/'logs'})
    app=create_app(cfg)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
            client.headers['X-Admin-CSRF']=app.state.admin.csrf_token
            yield app,client,cfg


async def save_apply(app,client,profile):
    v=(await client.get('/api/admin/operator/settings')).json()
    saved=await client.post('/api/admin/operator/settings',json={'expected_saved_revision':v['saved_config_revision'],
        'settings':{**v['saved'],'ai_profile':profile.model_dump(mode='json')}})
    assert saved.status_code==200,saved.text
    v=saved.json()
    response=await client.post('/api/admin/operator/apply',json={'expected_saved_revision':v['saved_config_revision'],
        'expected_active_runtime_revision':v['active_runtime_revision']})
    assert response.status_code==200,response.text
    return response.json()


async def test_profile_save_is_not_apply_and_kb_mutations_are_drafts(profile_api):
    app,http,cfg=profile_api
    before=app.state.playground_service.processor
    k=await http.post('/api/admin/knowledge/text',json={'title':'Hours','text':'The workshop opens at nine.'})
    assert k.status_code==200 and k.json()['sources'][0]['chunks']==1
    assert 'The workshop' not in k.text
    v=(await http.get('/api/admin/operator/settings')).json()
    response=await http.post('/api/admin/operator/settings',json={'expected_saved_revision':v['saved_config_revision'],
        'settings':{**v['saved'],'ai_profile':AIProfile().model_dump(mode='json')}})
    assert response.status_code==200 and response.json()['pending']
    assert app.state.playground_service.processor is before and before.project.ai_profile is None
    v=response.json()
    response=await http.post('/api/admin/operator/apply',json={'expected_saved_revision':v['saved_config_revision'],'expected_active_runtime_revision':v['active_runtime_revision']})
    assert response.status_code==200 and not response.json()['pending']
    processor=app.state.playground_service.processor
    assert processor.kb.retrieve('workshop',3)
    source=k.json()['sources'][0]['id']
    assert (await http.post('/api/admin/knowledge/'+source+'/delete',json={})).status_code==200
    assert processor.kb.retrieve('workshop',3) and (await http.get('/api/admin/operator/settings')).json()['pending']


async def test_models_query_only_current_endpoint_and_preserve_model_on_failure(profile_api,monkeypatch):
    app,http,_=profile_api
    await save_apply(app,http,AIProfile())
    engine=app.state.playground_service.processor.engine
    seen=[]
    def server(request):
        seen.append(str(request.url));return httpx.Response(200,json={'data':[{'id':'another-model'}]})
    engine._transport=httpx.MockTransport(server)
    result=await http.post('/api/admin/ai-profile/models',json={})
    assert result.json()['models']==['another-model'] and engine.model=='qwen/qwen3-8b'
    assert seen==['http://127.0.0.1:1234/v1/models']
    engine._client._transport=httpx.MockTransport(lambda request:httpx.Response(302,headers={'location':'http://elsewhere/v1/models'}))
    result=await http.post('/api/admin/ai-profile/models',json={})
    assert result.status_code==409 and engine.model=='qwen/qwen3-8b'
    assert (await http.post('/api/admin/ai-profile/models',json={'url':'http://elsewhere'})).status_code==422


async def test_upload_size_allowlist_and_csrf(profile_api):
    app,http,_=profile_api
    raw=b'Bounded uploaded knowledge. '*2500
    response=await http.post('/api/admin/knowledge/upload',json={'filename':'notes.md','content_base64':base64.b64encode(raw).decode()})
    assert response.status_code==200,response.text
    assert response.json()['sources'][0]['bytes']==len(raw)
    response=await http.post('/api/admin/knowledge/upload',json={'filename':'../notes.md','content_base64':'eA=='})
    assert response.status_code==422
    http.headers.pop('X-Admin-CSRF')
    assert (await http.post('/api/admin/knowledge/text',json={'title':'x','text':'x'})).status_code==403


async def test_generic_prompt_context_purpose_and_content_checks(settings,tmp_path):
    p=AIProfile()
    project=effective(load_yaml_model(settings.project_config_path,ProjectConfig),OperatorOverlay(ai_profile=p))
    cfg=settings.model_copy(update={'state_dir':tmp_path})
    store=KnowledgeStore(tmp_path);store.add('hours.txt',b'The workshop closes at six.')
    processor=create_processor(cfg,project=project)
    calls=[]
    def server(request):
        if request.method=='GET':return httpx.Response(200,json={'data':[{'id':p.model.model}]})
        body=json.loads(request.content);calls.append(body)
        data=json.loads(body['messages'][1]['content']);context=data['KNOWLEDGE_CONTEXT']
        draft=dict(detected_language='en',operator_translation='Когда закрывается мастерская?',should_reply=True,
            reply='The workshop closes at six.',reply_translation='Мастерская закрывается в шесть.',
            basis='knowledge',context_ids=[context[0]['id']])
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(draft)}}]})
    processor.engine._transport=httpx.MockTransport(server)
    result=await processor.process(Comment(username='viewer',text='When does the workshop close?'))
    assert result.status=='ready' and result.intent=='generic' and result.knowledge_ids
    from app.delivery.automatic import verified_content
    assert verified_content(result,processor)
    req=ReplyRequest(original_text='missing',is_candidate=True,project=project,knowledge=processor.knowledge)
    bad=GenericDraft(detected_language='en',operator_translation='Вопрос',should_reply=True,reply='Guaranteed profit.',
        reply_translation='Гарантированная прибыль.',basis='knowledge',context_ids=['invented'])
    assert {'unknown_context','content_rejected'}<=set(violations(req,bad))
    user=json.loads(calls[0]['messages'][1]['content'])
    assert user['purpose']=='VIEWER_REPLY' and user['USER_COMMENT']=='When does the workshop close?'
    assert 'Trading Bot' not in calls[0]['messages'][0]['content']
    await processor.aclose()
