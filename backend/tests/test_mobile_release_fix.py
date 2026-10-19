"""Release regressions: production pipeline/SQLite, fake native boundary only."""
import json
import logging
from pathlib import Path
import shlex
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
import xml.etree.ElementTree as ET

import pytest

from app.ai.generic import GenericDraft, violations
from app.ai.periodic_policy import RELEASE_TEMPLATES, periodic_statement
from app.ai.profile import AIProfile
from app.android.adb import AdbCommand, AdbTransport
from app.android.delivery_evidence import unresolved, proven_no_send, evidence_db, digest
from app.android.text_entry import ASCIITextEntryBackend
from app.android.tiktok_ui import rid
from app.delivery.journal import SQLiteDeliveryOperationStore
from test_periodic_profile import periodic_api, manual_accept, due
from test_android_outgoing_auto import arm_auto, captured, unknown, phone_api, reply


@pytest.mark.parametrize('text', ['Risk management is an essential part of trading.',
    'A clear strategy helps maintain consistency.', 'Careful planning helps the workshop.', 'The next level rewards careful timing.'])
def test_periodic_neutral_declarations(text):
    assert periodic_statement(text)


@pytest.mark.parametrize('text', RELEASE_TEMPLATES)
def test_release_templates_pass_real_content_policy_without_normalization(text):
    profile=AIProfile()
    assert profile.periodic_comments.templates==RELEASE_TEMPLATES
    request=SimpleNamespace(project=SimpleNamespace(ai_profile=profile,
        ai=SimpleNamespace(max_reply_chars=240,max_reply_sentences=2)),context=[],purpose='PERIODIC')
    draft=GenericDraft(detected_language='en',operator_translation='Шаблон ведущего',should_reply=True,
        reply=text,reply_translation='Шаблон оператора; перевод не выполнялся.',basis='general',context_ids=[])
    assert not violations(request,draft) and periodic_statement(text)
    assert not periodic_statement(text+'?')


@pytest.mark.parametrize('text', ['What would you like to discuss?', 'What would you like to discuss.',
    'Do you agree.', 'Tell us your opinion.', 'Feel free to ask.', 'One statement. Another statement.',
    'Welcome everyone!', 'Привет всем.', 'A neutral statement. ', 'A fake guarantee of income?', ''])
def test_periodic_questions_and_non_declarations_fail_closed(text):
    assert not periodic_statement(text)


@pytest.mark.parametrize('text',["I've earned money here.", 'Everyone here is winning.', 'The host just opened a trade.',
    'Viewers say the software is profitable.', 'Join us for guaranteed results.'])
def test_periodic_cannot_impersonate_viewers_or_invent_current_events(text):
    assert not periodic_statement(text)


def test_viewer_reply_policy_is_unchanged_and_defaults_are_neutral():
    profile = AIProfile()
    request = SimpleNamespace(project=SimpleNamespace(ai_profile=profile,
        ai=SimpleNamespace(max_reply_chars=240,max_reply_sentences=2)),context=[],purpose='VIEWER_REPLY')
    draft = GenericDraft(detected_language='en',operator_translation='Вопрос зрителя',should_reply=True,
        reply='What would you like to discuss?',reply_translation='Что хотите обсудить?',basis='general',context_ids=[])
    assert violations(request,draft)==()
    request.purpose='PERIODIC'
    assert 'periodic_statement_required' in violations(request,draft)
    assert all(periodic_statement(t) for t in profile.periodic_comments.templates)
    assert (profile.periodic_comments.interval_seconds,profile.periodic_comments.jitter_seconds,
        profile.periodic_comments.max_per_hour)==(300,30,12)


@pytest.mark.parametrize('expected,actual,first,count,kind', [
    ('A clear plan.', 'A.clear plan.', 1,1,'space->punctuation'),
    ('A clear plan.', 'B clear plan.', 0,1,'letter->letter'),
    ('A clear plan.', 'A clear plan!',12,1,'punctuation->punctuation'),
    ('A clear plan.', 'A clear plan.',None,0,None),
    ('A clear plan.', 'A\u00a0clear plan.',1,1,'space->space'),
    ("It's a plan.", 'It\u2019s a plan.',2,1,'punctuation->punctuation')])
def test_safe_readback_character_diagnostics(expected,actual,first,count,kind):
    backend=ASCIITextEntryBackend(SimpleNamespace())
    backend.require_send=True
    backend.diagnostic={}
    backend.record_readback(expected,{'text':actual})
    data=backend.diagnostic
    assert data['readback_first_mismatch_index']==first and data['readback_mismatch_count']==count
    assert data['readback_expected_length']==data['readback_actual_length']
    if kind: assert kind in data['readback_changed_categories']
    assert expected not in json.dumps(data) and actual not in json.dumps(data)


def test_reported_55_character_mismatch_is_not_mistaken_for_exact_text():
    expected='A clear explanation helps everyone understand the topic'
    actual='B'+expected[1:]
    assert len(expected)==len(actual)==55
    backend=ASCIITextEntryBackend(SimpleNamespace());backend.diagnostic={}
    backend.record_readback(expected,{'text':actual})
    assert not backend.diagnostic['readback_exact_match'] and not backend.diagnostic['readback_trim_match']
    assert not backend.diagnostic['readback_casefold_match']
    assert backend.diagnostic['readback_first_mismatch_index']==0 and backend.diagnostic['readback_mismatch_count']==1


def test_swiftkey_word_substitution_and_suffix_are_not_an_exact_readback():
    expected,actual="We're discussing this LIVE.",'Where discussing this LIVE. '
    backend=ASCIITextEntryBackend(SimpleNamespace());backend.diagnostic={}
    backend.record_readback(expected,{'text':actual})
    data=backend.diagnostic
    assert not data['readback_exact_match'] and not data['readback_trim_match'] and not data['readback_casefold_match']
    assert data['readback_actual_length']==data['readback_expected_length']+1
    assert data['readback_first_mismatch_index']==1 and data['readback_mismatch_count']==3
    assert data['readback_changed_categories']==['letter->letter','missing->space','punctuation->letter']
    assert data['readback_trailing_whitespace']==1
    assert expected not in json.dumps(data) and actual not in json.dumps(data)


@pytest.mark.parametrize('corrupt',[False,True])
async def test_final_inspection_rejection_keeps_readback_diagnostics_and_consumed_identity(phone_api,monkeypatch,caplog,corrupt):
    from app.logging_config import LOGGER_NAME
    m=phone_api
    envelope,body=await reply(m)
    controller=m.service.controller
    logger=logging.getLogger(LOGGER_NAME+'.android')
    monkeypatch.setattr(logger,'handlers',[caplog.handler])
    caplog.set_level(logging.INFO)
    original=m.phone.spawn
    async def spawn(*args,**kwargs):
        result=await original(*args,**kwargs)
        if 'input' in args and args[args.index('input')+1]=='text':
            evidence=controller._live_evidence
            controller._live_evidence=(*evidence[:3],evidence[3]-91)
            if corrupt:
                root=ET.fromstring(m.phone.current)
                entry=next(n for n in root.iter() if n.get('resource-id')==rid('h4k'))
                entry.set('text','X'+entry.get('text')[1:])
                m.phone.current=ET.tostring(root,encoding='unicode')
        return result
    monkeypatch.setattr('asyncio.create_subprocess_exec',AsyncMock(side_effect=spawn))
    m.phone.current=captured('composer')
    result=await m.http.post('/api/admin/delivery/fill',json=body)
    assert result.json()=={'status':'needs_operator','reason':'rejected'}
    assert m.phone.sent==0 and m.service.paused and m.service.mode=='MANUAL' and not m.service.pending_fill
    assert m.service.consumed(envelope)
    operation=m.service.journal.lookup(body['idempotency_key'])
    assert operation.status=='needs_operator' and operation.reason=='rejected'
    inputs=[args[args.index('input')+1] for args in m.phone.calls if 'input' in args]
    assert inputs==['text']  # No clear, second entry or Send after authority expired.
    records=[r.fields for r in caplog.records if r.getMessage()=='android_manual_text_entry']
    evidence=next(r for r in records if r['rejection_reason']=='fresh_evidence_rejected')
    assert evidence['post_readback_result']=='observed_without_authority'
    assert evidence['classified_screen']=='COMMENT_INPUT_READY' and evidence['inspection_reason']=='live_unverified'
    assert evidence['account_verified'] and evidence['profile_verified'] and not evidence['live_verified']
    assert evidence['readback_target_focused'] and evidence['prior_host_evidence_age_seconds']>=90
    assert not evidence['current_host_evidence_present']
    assert evidence['readback_exact_match'] is (not corrupt)
    assert evidence['readback_mismatch_count']==int(corrupt)
    assert all(envelope.suggested_reply not in json.dumps(record) for record in records)
    before=tuple(m.service.journal.connection.execute('SELECT * FROM operations WHERE operation_id=?',(operation.operation_id,)).fetchone())
    assert (await m.http.post('/api/admin/suggestions/'+envelope.suggestion_id+'/approve',
        json={'expected_suggestion_revision':envelope.suggestion_revision})).status_code==409
    assert tuple(m.service.journal.connection.execute('SELECT * FROM operations WHERE operation_id=?',(operation.operation_id,)).fetchone())==before


@pytest.mark.parametrize('text', ["It's a clear plan.", 'A clear plan (one step); no rush.', 'A, B: one step.', 'A  clear plan.'])
def test_adb_percent_space_and_remote_shell_quote_preserve_supported_text(tmp_path,text):
    executable=tmp_path/'adb.exe';executable.touch()
    args=AdbTransport(executable).argv(AdbCommand.TEXT,serial='fixture',text=text)
    remote_word=shlex.split(args[-1])
    assert len(remote_word)==1 and remote_word[0].replace('%s',' ')==text


@pytest.mark.parametrize('receipt_store',['current.sqlite3','delivery.sqlite3'])
def test_no_send_history_proof_is_bound_to_its_journal(tmp_path,receipt_store):
    stores=[SQLiteDeliveryOperationStore(tmp_path/name) for name in ('current.sqlite3','delivery.sqlite3')]
    try:
        service=SimpleNamespace(journal=stores[0],settings=SimpleNamespace(state_dir=tmp_path))
        operation='a'*32
        for store in stores:
            values=dict(operation_id=operation,idempotency_key='one',suggestion_id='b'*32,
                suggestion_revision='c'*64,comment_identity='d'*64,approval_id='e'*32,
                reply_hash='f'*64,live_session_id='0'*32,target_account='own',runtime_revision='1'*32,
                created_at='2026-10-19T00:00:00+00:00',mode='AUTOMATIC',
                status='needs_operator' if store.path.name==receipt_store else 'unknown',reason='rejected')
            columns=','.join(values)
            store.connection.execute(f"INSERT INTO operations ({columns}) VALUES ({','.join('?' for _ in values)})",tuple(values.values()))
            if store.path.name==receipt_store:
                row=dict(store.connection.execute('SELECT * FROM operations').fetchone())
                with evidence_db(tmp_path,write=True) as db:
                    db.execute('INSERT INTO no_send_receipts VALUES (?,?,?)',(store.path.name,operation,digest(row)))
        assert proven_no_send(service)==({operation} if receipt_store=='current.sqlite3' else set())
        assert unresolved(service)  # The other store's UNKNOWN always blocks recovery.
    finally:
        for store in stores: store.connection.close()


@pytest.mark.parametrize('periodic_api',[{'mode':'templates','continuous':True}],indirect=True)
async def test_auto_rejected_without_send_recovers_only_new_periodic_slot(periodic_api,monkeypatch):
    m=periodic_api
    await manual_accept(m)
    await arm_auto(m)
    original=m.phone.spawn
    inserted=[]
    async def corrupt_once(*args,**kwargs):
        result=await original(*args,**kwargs)
        if 'input' in args and args[args.index('input')+1]=='text':
            root=ET.fromstring(m.phone.current)
            entry=next(n for n in root.iter() if n.get('resource-id')==rid('h4k'))
            text=entry.get('text'); inserted.append(text)
            if len(inserted)==1:
                index=text.index(' ')
                entry.set('text',text[:index]+'.'+text[index+1:])
                m.phone.current=ET.tostring(root,encoding='unicode')
        return result
    monkeypatch.setattr('asyncio.create_subprocess_exec',AsyncMock(side_effect=corrupt_once))
    await due(m)
    old=m.app.state.admin.live_history[0]
    envelope=m.app.state.admin.suggestions.get(old.id)
    operation=m.service.journal.lookup('auto_'+old.id)
    assert operation.status=='needs_operator' and operation.reason=='rejected'
    assert m.phone.sent==0 and len(inserted)==1
    assert m.service.mode=='AUTOMATIC' and not m.service.paused and not m.service.acceptance_spent
    assert operation.operation_id in proven_no_send(m.service)
    assert not unresolved(m.service,allow_rejected=True) and m.service.consumed(envelope)
    row=tuple(m.service.journal.connection.execute('SELECT * FROM operations WHERE operation_id=?',(operation.operation_id,)).fetchone())
    history=(await m.http.get('/api/admin/delivery/history')).json()
    assert next(r for r in history if r['operation_id']==operation.operation_id)['send_invoked'] is False
    assert (await m.http.post('/api/admin/suggestions/'+old.id+'/approve',json={'expected_suggestion_revision':envelope.suggestion_revision})).status_code==409
    m.phone.current=captured('host')
    m.phone.after_send=lambda text:m.clients[-1].comment(text,identity='3999')
    # Advance only the sender's test cooldown; keep every durable slot/history row.
    m.service.last_attempt-=m.service.settings.automatic_delivery_min_interval_seconds
    m.now[0]=m.runtime.periodic.due
    await m.runtime.periodic.tick()
    assert m.phone.sent==1 and len(inserted)==2
    latest=m.service.journal.lookup('auto_'+m.app.state.admin.live_history[0].id)
    assert latest.status=='confirmed' and latest.reason=='sdk_echo_confirmed'
    assert m.service.mode=='AUTOMATIC' and not m.service.paused
    assert tuple(m.service.journal.connection.execute('SELECT * FROM operations WHERE operation_id=?',(operation.operation_id,)).fetchone())==row
    assert m.service.consumed(envelope)


async def test_readiness_is_read_only_and_does_not_enable_auto(periodic_api):
    m=periodic_api
    await manual_accept(m)
    before=sum('input' in args for args in m.phone.calls)
    result=await m.http.post('/api/admin/mobile/readiness',json={})
    assert result.status_code==200,result.text
    data=result.json()
    assert len(data['checks'])>=10 and not data['ready']
    assert [c['name'] for c in data['checks'] if not c['ok']]==['Допуск отправки']
    assert data['auto_enabled'] is False and m.service.mode=='MANUAL' and m.service.armed_binding is None
    assert m.app.state.admin.ai_status.available and m.app.state.admin.ai_status.model_found
    assert before==sum('input' in args for args in m.phone.calls)
    assert (await m.http.post('/api/admin/mobile/readiness',json={'mode':'AUTOMATIC'})).status_code==422


async def test_readiness_never_acknowledges_unknown(periodic_api):
    m=periodic_api
    _,_,old,before=await unknown(m)
    result=await m.http.post('/api/admin/mobile/readiness',json={})
    assert result.status_code==200 and not result.json()['ready']
    assert m.service.paused and m.service.mode=='MANUAL'
    assert any(not c['ok'] and c['name']=='Журнал отправок' for c in result.json()['checks'])
    assert not proven_no_send(m.service)
    assert tuple(m.service.journal.connection.execute('SELECT * FROM operations WHERE operation_id=?',(old.operation_id,)).fetchone())==before
    # Even a matching sidecar entry cannot acknowledge an UNKNOWN status.
    row=dict(m.service.journal.connection.execute('SELECT * FROM operations WHERE operation_id=?',(old.operation_id,)).fetchone())
    with evidence_db(m.config.state_dir,write=True) as db:
        db.execute('INSERT INTO no_send_receipts VALUES (?,?,?)',(m.service.journal.path.name,old.operation_id,digest(row)))
    assert unresolved(m.service,allow_rejected=True) and not proven_no_send(m.service)


@pytest.mark.parametrize('periodic_api',[{'mode':'ai','question':True}],indirect=True)
async def test_model_question_is_rejected_before_any_periodic_input(periodic_api):
    m=periodic_api
    await manual_accept(m); await arm_auto(m)
    before=sum('input' in args for args in m.phone.calls)
    await due(m)
    assert m.runtime.periodic.last_result=='content_rejected' and m.phone.sent==0
    assert before==sum('input' in args for args in m.phone.calls)
    assert m.service.mode=='AUTOMATIC' and not m.service.paused


@pytest.mark.parametrize('periodic_api',[{'mode':'templates','continuous':True}],indirect=True)
async def test_two_new_periodic_slots_confirm_and_keep_auto_enabled(periodic_api):
    m=periodic_api
    await manual_accept(m);await arm_auto(m)
    receipts=iter(['4901','4902'])
    m.phone.after_send=lambda text:m.clients[-1].comment(text,identity=next(receipts))
    await due(m)
    first=m.app.state.admin.live_history[0].id
    assert m.phone.sent==1 and m.service.mode=='AUTOMATIC' and not m.service.paused
    m.service.last_attempt-=m.service.settings.automatic_delivery_min_interval_seconds
    m.now[0]=m.runtime.periodic.due
    await m.runtime.periodic.tick()
    second=m.app.state.admin.live_history[0].id
    assert first!=second and m.phone.sent==2 and m.service.mode=='AUTOMATIC' and not m.service.paused
    assert all(m.service.journal.lookup('auto_'+identity).reason=='sdk_echo_confirmed' for identity in (first,second))


async def test_rejected_no_send_never_rearms_one_shot(periodic_api,monkeypatch):
    m=periodic_api
    await manual_accept(m);await arm_auto(m)
    original=m.phone.spawn
    async def mismatch(*args,**kwargs):
        result=await original(*args,**kwargs)
        if 'input' in args and args[args.index('input')+1]=='text':
            root=ET.fromstring(m.phone.current);entry=next(n for n in root.iter() if n.get('resource-id')==rid('h4k'))
            entry.set('text','Z'+entry.get('text')[1:]);m.phone.current=ET.tostring(root,encoding='unicode')
        return result
    monkeypatch.setattr('asyncio.create_subprocess_exec',AsyncMock(side_effect=mismatch))
    await due(m)
    assert m.phone.sent==0 and m.service.mode=='MANUAL' and m.service.paused and m.service.acceptance_spent
    assert proven_no_send(m.service) and m.service._pause_reason=='no_send_rejected'


@pytest.mark.parametrize('periodic_api',[{'mode':'templates','continuous':True}],indirect=True)
@pytest.mark.parametrize('failure',['scope_changed','trust_revoked','receipt_failure','after_send','row_changed'])
async def test_technical_recovery_never_clears_unsafe_pause(periodic_api,monkeypatch,failure):
    m=periodic_api
    await manual_accept(m); await arm_auto(m)
    original=m.phone.spawn
    async def corrupt(*args,**kwargs):
        result=await original(*args,**kwargs)
        if failure!='after_send' and 'input' in args and args[args.index('input')+1]=='text':
            root=ET.fromstring(m.phone.current)
            entry=next(n for n in root.iter() if n.get('resource-id')==rid('h4k'))
            text=entry.get('text'); entry.set('text',('Z' if text[0]!='Z' else 'X')+text[1:])
            m.phone.current=ET.tostring(root,encoding='unicode')
        return result
    monkeypatch.setattr('asyncio.create_subprocess_exec',AsyncMock(side_effect=corrupt))
    if failure=='receipt_failure':
        def broken(*args,**kwargs): raise OSError('private write error')
        monkeypatch.setattr('app.android.technical_recovery.record_no_send',broken)
    inspect=m.service.controller.inspect
    async def change_after_result(target=None):
        state=await inspect(target)
        coordinator=m.service.coordinator
        operation=next((o for o in m.service.journal.recent() if o.operation_id==coordinator.operation_id),None) if coordinator and m.service.journal else None
        if operation and operation.status=='needs_operator':
            if failure=='scope_changed': m.service.auto_generation+=1
            elif failure=='trust_revoked': m.service.controller.invalidate_trust()
            elif failure=='row_changed':
                m.service.journal.connection.execute("UPDATE operations SET target_account='foreign' WHERE operation_id=?",(operation.operation_id,))
        return state
    monkeypatch.setattr(m.service.controller,'inspect',change_after_result)
    await due(m)
    assert m.service.paused and not proven_no_send(m.service)
    assert m.phone.sent==(1 if failure=='after_send' else 0)
    assert unresolved(m.service,allow_rejected=True)
    latest=m.service.journal.lookup('auto_'+m.app.state.admin.live_history[0].id)
    assert latest.status==('unknown' if failure=='after_send' else 'needs_operator')
    before=m.phone.sent
    m.now[0]=m.runtime.periodic.due
    await m.runtime.periodic.tick()
    assert m.phone.sent==before


def test_readiness_ui_does_not_arm_auto_and_expires_old_results():
    executable=shutil.which('node')
    if not executable: pytest.skip('Node required for DOM regression')
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),elements=new Map(),calls=[];
const el=()=>({value:'',children:[],listeners:{},append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x},addEventListener(k,f){this.listeners[k]=f}});
const document={getElementById(id){if(!elements.has(id))elements.set(id,el());return elements.get(id)}};
const window={};vm.runInNewContext(fs.readFileSync('backend/app/static/admin/delivery.js','utf8'),{window,document});
let current={transport:'android',inspection:{devices:[]},mode:'MANUAL',active_runtime_revision:'a'.repeat(32),live_session_id:'b'.repeat(32)};
const api=async(path,body)=>{calls.push([path,body]);if(path.endsWith('/mobile/readiness')){current={...current,active_runtime_revision:'d'.repeat(32)};return {checks:[{name:'Устройство',ok:true,detail:'Проверено'}],next_step:'Автоотправка включается отдельно.',active_runtime_revision:current.active_runtime_revision,live_session_id:current.live_session_id};}if(path.endsWith('/status'))return current;if(path.endsWith('/history'))return [];throw Error(path)};
const controls=window.createDeliveryControls({api,node:(_,__,text)=>Object.assign(el(),{textContent:text}),toast(){},state:{connected:true},refreshCards(){}});
(async()=>{controls.render(current);await document.getElementById('check-mobile-readiness').listeners.click();
assert.strictEqual(elements.get('mobile-readiness-results').children.length,1);assert(elements.get('mobile-readiness-next').textContent.includes('отдельно'));
assert(calls.some(([p,b])=>p.endsWith('/mobile/readiness')&&Object.keys(b).length===0));assert(!calls.some(([p])=>/mode|fill|send|acknowledge|connect/.test(p)));
current={...current,active_runtime_revision:'c'.repeat(32)};controls.render(current);assert.strictEqual(elements.get('mobile-readiness-results').children.length,0);
controls.render({transport:'windows_live_studio',windows_sender:{}});assert(elements.get('mobile-readiness').hidden);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result=subprocess.run([executable,'-e',script],capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert result.returncode==0,result.stderr
