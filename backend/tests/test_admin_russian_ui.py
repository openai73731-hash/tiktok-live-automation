"""Russian presentation with real wire values, explicit writes and canonical AUTO."""
from pathlib import Path
import shutil
import subprocess
from html.parser import HTMLParser

import httpx

from app.main import create_app
from app.ai.profile import AIProfile
from test_generic_profile import profile_api, save_apply


DOM = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),crypto=require('crypto');
class Element {
 constructor(tag='input'){this.tagName=tag.toUpperCase();this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.children=[];this.listeners={};this.options=[];this.attributes={};this.textContent='';}
 append(...xs){this.children.push(...xs);} replaceChildren(...xs){this.children=xs;}
 addEventListener(name,fn){this.listeners[name]=fn;} setAttribute(k,v){this.attributes[k]=v;}
 querySelectorAll(selector){return this.children.flatMap(c=>[...(selector.split(',').some(t=>t.toUpperCase()===c.tagName)?[c]:[]),...c.querySelectorAll(selector)]);}
 querySelector(s){return this.querySelectorAll(s)[0];} get lastElementChild(){return this.children.at(-1);} focus(){}
}
const els=new Map(),get=id=>{if(!els.has(id))els.set(id,new Element());return els.get(id);};
const document={getElementById:get,createElement:tag=>new Element(tag),querySelectorAll:()=>[]};
let confirm=true;const confirmations=[],window={setInterval(){},confirm:s=>{confirmations.push(s);return confirm;}};
const context={window,document,crypto,setTimeout(){}};
const load=f=>vm.runInNewContext(fs.readFileSync('backend/app/static/admin/'+f,'utf8'),context);
load('i18n.js');
const tick=async()=>{for(let i=0;i<8;i++)await new Promise(setImmediate);};
const event={preventDefault(){}};
"""


def run_js(script):
    node = shutil.which('node')
    assert node, 'Node is required for the release UI checks'
    result = subprocess.run([node, '-e', DOM + script], text=True, capture_output=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr


def test_periodic_form_explicit_save_apply_bounds_and_reload():
    run_js(r"""
load('profile.js');
const p={name:'Own topic',topic:'Workshop',language:'en',tone:'Calm',system_prompt:'Be honest',reply_instruction:'Be concise',model:{provider:'openai_compatible',base_url:'http://127.0.0.1:1234/v1',model:'qwen/qwen3-8b',temperature:0,max_tokens:512},knowledge:{enabled:true,top_k:3},periodic_comments:{enabled:false,interval_seconds:300,jitter_seconds:30,mode:'ai',instruction:'Ask a neutral question',templates:['A short phrase.'],max_per_hour:12}};
let view={saved:{tiktok:{username:'owner'},ai_profile:p},active:{ai_profile:p},desired:{ai_profile:p},saved_config_revision:'a'.repeat(32),active_runtime_revision:'b'.repeat(32),locked:false,pending:false};
const state={connected:true,status:{live:{connection_owned:false}},delivery:{mode:'MANUAL',physical_ready:false,mobile_one_shot:'SPENT'}};
const writes=[];
const api=async(path,body)=>{
 if(body){writes.push([path,JSON.parse(JSON.stringify(body))]);
  if(path.endsWith('/settings'))view={...view,saved:body.settings,pending:true,saved_config_revision:'c'.repeat(32)};
  if(path.endsWith('/apply'))view={...view,active:{ai_profile:view.saved.ai_profile},pending:false};
 }
 if(path.endsWith('/settings')||path.endsWith('/apply'))return view;
 if(path.endsWith('/knowledge'))return {sources:[],total_bytes:0};
 if(path.endsWith('/periodic/status'))return {state:'blocked',last_result:'skipped_unavailable',last_send:null,next_due_seconds_range:[270,300]};
 throw Error(path);
};
(async()=>{
 const controls=window.createProfileControls({api,state,toast(){}});controls.render(view);await controls.refresh();
 assert.strictEqual(writes.length,0);assert.strictEqual(get('profile-periodic-enabled').checked,false);
 get('profile-periodic-enabled').checked=true;get('profile-periodic-enabled').listeners.input();
 get('profile-interval-preset').value='3';get('profile-interval-preset').listeners.change();
 assert.strictEqual(writes.length,0);assert(!get('periodic-save').disabled);assert(get('periodic-apply').disabled);
 for(let i=1;i<12;i++)get('template-add').listeners.click();
 assert.strictEqual(get('template-list').children.length,12);assert(get('template-add').disabled);
 get('template-add').listeners.click();assert.strictEqual(get('template-list').children.length,12);
 get('periodic-form').listeners.submit(event);await tick();assert.strictEqual(writes.length,0);assert(!get('periodic-error').hidden);
 get('template-list').querySelectorAll('textarea').forEach((n,i)=>{n.value='Safe phrase '+i;n.listeners.input();});
 get('profile-interval').value='0.5';get('periodic-form').listeners.submit(event);await tick();assert.strictEqual(writes.length,0);
 get('profile-interval').value='3';get('periodic-form').listeners.submit(event);await tick();
 assert.strictEqual(writes.length,1);const body=writes[0][1];assert.strictEqual(body.settings.ai_profile.periodic_comments.interval_seconds,180);
 assert.strictEqual(body.settings.ai_profile.periodic_comments.mode,'ai');assert.strictEqual(body.settings.ai_profile.periodic_comments.templates.length,12);
 assert.strictEqual(body.settings.tiktok.username,'owner');assert.strictEqual(view.active.ai_profile.periodic_comments.enabled,false);
 assert(!get('periodic-apply').disabled);
 get('profile-topic').value='Unsaved';get('profile-topic').listeners.input();controls.render(view);assert.strictEqual(get('profile-topic').value,'Unsaved');
 controls.render(view,true);get('periodic-apply').listeners.click();await tick();
 assert.strictEqual(writes.length,2);assert.strictEqual(writes[1][0],'/api/admin/operator/apply');
 assert.strictEqual(writes[1][1].expected_active_runtime_revision,'b'.repeat(32));
 assert.strictEqual(state.delivery.mode,'MANUAL');assert(get('periodic-readiness').textContent.includes('отправка сейчас недоступна'));
 els.clear();const reloaded=window.createProfileControls({api,state,toast(){}});reloaded.render(view);
 assert.strictEqual(get('profile-periodic-enabled').checked,true);assert.strictEqual(Number(get('profile-interval').value),3);assert.strictEqual(writes.length,2);
 state.status.live.connection_owned=true;reloaded.controls();assert(get('periodic-apply').disabled);assert(get('profile-periodic-enabled').disabled);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_auto_switch_uses_canonical_backend_and_never_silently_enables():
    run_js(r"""
load('delivery.js');
let status={transport:'android',mode:'MANUAL',busy:false,paused:false,mobile_one_shot:'SPENT',auto_state:'AUTO OFF',auto_reason:'automatic_mode_required',physical_delivery_enabled:true,physical_ready:false,workflow:'READY',live_session_id:'1'.repeat(32),active_runtime_revision:'2'.repeat(32),inspection:{devices:[],account_verified:true,profile_verified:true,live_verified:true}};
const state={connected:true,status:{live:{state:'connected'}}},writes=[];
const api=async(path,body)=>{if(body){writes.push([path,body]);if(status.mobile_one_shot==='SPENT')throw Error('Отправка недоступна');status={...status,mode:body.mode,auto_state:body.mode==='AUTOMATIC'?'AUTO ARMED':'AUTO OFF',physical_ready:body.mode==='AUTOMATIC',auto_reason:'eligible'};}return path.endsWith('/history')?[]:status;};
(async()=>{
 const c=window.createDeliveryControls({api,node:(tag,cls,text)=>Object.assign(new Element(tag),{textContent:text}),state,toast(){},refreshCards(){}});
 await c.refresh();assert.strictEqual(writes.length,0);assert.strictEqual(get('auto-replies').checked,false);
 assert(get('auto-limit').textContent.includes('уже использована'));
 get('auto-replies').checked=true;await get('auto-replies').listeners.change();await tick();
 assert.strictEqual(writes.length,1);assert.strictEqual(writes[0][1].mode,'AUTOMATIC');assert.strictEqual(writes[0][1].expected_live_session_id,'1'.repeat(32));
 assert.strictEqual(writes[0][1].expected_active_runtime_revision,'2'.repeat(32));assert.strictEqual(get('auto-replies').checked,false);
 assert(!get('auto-replies-error').hidden);assert(get('auto-replies-error').textContent.includes('единственная отправка'));
 confirm=false;get('auto-replies').checked=true;get('auto-replies').listeners.change();await tick();assert.strictEqual(writes.length,1);assert.strictEqual(get('auto-replies').checked,false);
 confirm=true;status={...status,mobile_one_shot:'AVAILABLE'};c.render(status);
 c.render({...status,inspection:{...status.inspection,reason:'ready'}});
 assert.strictEqual(get('auto-replies-reason').textContent,'Автоматический режим выключен');
 get('auto-replies').checked=true;get('auto-replies').listeners.change();await tick();assert.strictEqual(get('auto-replies').checked,true);
 assert(confirmations.every(s=>s.includes('автоматический режим')));
 c.render({...status,mode:'MANUAL',auto_state:'AUTO OFF',mobile_one_shot:'SPENT'});assert.strictEqual(get('auto-replies').checked,false);assert.strictEqual(writes.length,2);
 state.connected=false;c.controls();assert(get('auto-replies').disabled);
 assert.strictEqual(window.AdminI18n.text('UNKNOWN'),'Отправка не подтверждена');
 assert.notStrictEqual(window.AdminI18n.text('REPLY READY'),window.AdminI18n.text('ARM READY — CLICK COMPOSER NOW'));
 assert.strictEqual(window.AdminI18n.text('qwen/qwen3-8b'),'qwen/qwen3-8b');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_russian_navigation_and_wire_values_in_html():
    class Page(HTMLParser):
        def __init__(self):
            super().__init__(); self.ids=[]; self.pages=[]; self.values=[]
        def handle_starttag(self, tag, attrs):
            a=dict(attrs)
            if 'id' in a: self.ids.append(a['id'])
            if 'data-page' in a: self.pages.append(a['data-page'])
            if tag=='option': self.values.append(a.get('value'))
    html=Path('backend/app/static/admin/index.html').read_text(encoding='utf-8')
    page=Page();page.feed(html)
    assert len(page.ids)==len(set(page.ids))
    assert page.pages==['dashboard','live','periodic','profile','knowledge','settings']
    assert {'MANUAL','AUTOMATIC','ai','templates','mixed'}<=set(page.values)
    assert 'ограниченный ASCII-текст' in html
    assert html.index('i18n.js') < html.index('app.js')


async def test_periodic_settings_survive_restart_without_auto(profile_api):
    app,http,cfg=profile_api
    p=AIProfile.model_validate({'name':'Локальный профиль','periodic_comments':{'enabled':True,'mode':'mixed','interval_seconds':180,'jitter_seconds':20,'max_per_hour':7,'templates':['First phrase.','Second phrase.']}})
    await save_apply(app,http,p)
    restarted=create_app(cfg)
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted),base_url='http://127.0.0.1:8000') as client:
            view=(await client.get('/api/admin/operator/settings')).json()
            assert view['active']['ai_profile']['periodic_comments']==p.periodic_comments.model_dump(mode='json')
            d=(await client.get('/api/admin/delivery/status')).json()
            assert d['mode']=='MANUAL' and d['auto_state']=='AUTO OFF' and not d['physical_delivery_enabled']
