/* One shared draft. Existing Save/Apply, no AUTO writes. */
window.createProfileControls = ({api,state,toast}) => {
 const $=id=>document.getElementById(id), t=window.AdminI18n.text;
 let view=null,dirty=false,busy=false,revision=null,lastPeriodic=null;
 const names=['name','topic','language','tone','system','instruction','provider','url','model','temperature','tokens','top-k','interval','jitter','periodic-mode','max-hour','periodic-instruction','templates','kb-enabled','periodic-enabled'];
 const fields=names.map(n=>$('profile-'+n)),value=n=>$('profile-'+n).value,number=n=>Number(value(n));
 const locked=()=>Boolean(busy||!state.connected||view?.locked||state.status?.live?.connection_owned);
 function controls(){
  const lock=locked();fields.forEach(n=>n.disabled=lock);
  ['profile-save','periodic-save','kb-save'].forEach(id=>$(id).disabled=lock||!view||!dirty);
  ['profile-apply','periodic-apply','kb-apply'].forEach(id=>$(id).disabled=lock||dirty||!view?.pending);
  $('profile-refresh-models').disabled=busy||!state.connected;$('profile-interval-preset').disabled=lock;
  $('template-add').disabled=lock||$('template-list').children.length>=12;
  $('template-list').querySelectorAll('textarea,button').forEach(n=>n.disabled=lock);
  $('kb-text-form').querySelectorAll('input,textarea,button').forEach(n=>n.disabled=lock);
  $('kb-file').disabled=$('kb-upload').disabled=lock;$('kb-sources').querySelectorAll('button').forEach(n=>n.disabled=lock);
  const message=state.status?.live?.connection_owned?'Для изменения и применения настроек отключите подключение к чату. Сам эфир на телефоне автоматически не останавливается.':view?.locked?'Дождитесь завершения текущей операции.':'Сохранить — записать изменения. Применить — включить сохранённые настройки и базу знаний.';
  document.querySelectorAll('[data-profile-lock]').forEach(n=>n.textContent=message);
  const label=dirty?'Есть несохранённые изменения в настройках ИИ или автосообщений':view?.pending?'Сохранено. Нажмите «Применить», чтобы включить изменения.':'Сохранённые настройки применены';
  ['profile-state','periodic-settings-state','kb-apply-state'].forEach(id=>$(id).textContent=label);
  $('profile-form').setAttribute('aria-busy',String(busy));$('periodic-form').setAttribute('aria-busy',String(busy));presentation();
 }
 function dirtyNow(){dirty=true;controls();}
 function templateValues(){return Array.from($('template-list').querySelectorAll('textarea')).map(n=>n.value);}
 function templates(values){
  const rows=values.map((text,i)=>{
   const row=document.createElement('li'),input=document.createElement('textarea'),label=document.createElement('label'),remove=document.createElement('button');
   input.value=text;input.rows=2;input.maxLength=240;input.id='periodic-template-'+i;label.htmlFor=input.id;label.textContent='Фраза '+(i+1);row.className='template-row';
   input.addEventListener('input',()=>{$('profile-templates').value=templateValues().join('\n');dirtyNow();});
   remove.type='button';remove.className='text-button';remove.textContent='Удалить';remove.setAttribute('aria-label','Удалить фразу '+(i+1));
   remove.addEventListener('click',()=>{const next=templateValues();next.splice(i,1);templates(next);dirtyNow();});row.append(label,input,remove);return row;
  });$('template-list').replaceChildren(...rows);$('profile-templates').value=values.join('\n');$('template-count').textContent=values.length+' / 12';
 }
 function render(next,force=false){
  view=next;if(!dirty||force){
   const p=next.saved.ai_profile||next.desired.ai_profile||next.profile_defaults;if(!p)return;const m=p.model,c=p.periodic_comments;
   const values={name:p.name,topic:p.topic,language:p.language,tone:p.tone,system:p.system_prompt,instruction:p.reply_instruction,provider:m.provider,url:m.base_url,model:m.model,temperature:m.temperature,tokens:m.max_tokens,'top-k':p.knowledge.top_k,interval:c.interval_seconds/60,jitter:c.jitter_seconds,'periodic-mode':c.mode,'max-hour':c.max_per_hour,'periodic-instruction':c.instruction};
   Object.entries(values).forEach(([k,v])=>$('profile-'+k).value=v);$('profile-kb-enabled').checked=p.knowledge.enabled;$('profile-periodic-enabled').checked=c.enabled;
   templates(c.templates);$('profile-interval-preset').value=['3','5','10'].includes(String(c.interval_seconds/60))?String(c.interval_seconds/60):'custom';$('custom-interval').hidden=$('profile-interval-preset').value!=='custom';revision=next.saved_config_revision;dirty=false;
  }controls();
 }
 function draft(){
  const seconds=number('interval')*60,items=templateValues().map(s=>s.trim());
  if(!Number.isFinite(seconds)||Math.abs(seconds-Math.round(seconds))>1e-6||seconds<60||seconds>86400)throw Error('Интервал должен составлять от 1 до 1440 минут с точностью до секунды.');
  if(!Number.isInteger(number('jitter'))||number('jitter')<0||number('jitter')>300||number('jitter')>seconds/2)throw Error('Разброс — целое число от 0 до 300 секунд, не больше половины интервала.');
  if(items.length>12||items.some(s=>!s||s.length>240))throw Error('Добавьте не больше 12 непустых фраз, до 240 символов каждая.');
  if($('profile-periodic-enabled').checked&&value('periodic-mode')!=='ai'&&!items.length)throw Error('Для выбранного режима нужна хотя бы одна готовая фраза.');
  return {name:value('name'),topic:value('topic'),language:value('language'),tone:value('tone'),system_prompt:value('system'),reply_instruction:value('instruction'),model:{provider:value('provider'),base_url:value('url'),model:value('model'),temperature:number('temperature'),max_tokens:number('tokens')},knowledge:{enabled:$('profile-kb-enabled').checked,top_k:number('top-k')},periodic_comments:{enabled:$('profile-periodic-enabled').checked,interval_seconds:Math.round(seconds),jitter_seconds:number('jitter'),mode:value('periodic-mode'),max_per_hour:number('max-hour'),instruction:value('periodic-instruction'),templates:items}};
 }
 function showError(message){['profile-error','periodic-error','kb-error'].forEach(id=>{$(id).textContent=message;$(id).hidden=false;});}
 async function action(path,body){
  if(locked())return;busy=true;controls();['profile-error','periodic-error','kb-error'].forEach(id=>$(id).hidden=true);
  try{await api(path,body);render(await api('/api/admin/operator/settings'),path.startsWith('/api/admin/operator/'));await knowledge();toast(path.endsWith('/apply')?'Сохранённые настройки и база знаний применены. Автоотправка остаётся выключенной.':'Сохранено. Для включения изменений нажмите «Применить».');}
  catch(error){showError(error.message);}finally{busy=false;controls();}
 }
 function save(event){event.preventDefault();if(locked()||!dirty||!view)return;try{const p=draft();action('/api/admin/operator/settings',{expected_saved_revision:revision,settings:{...view.saved,ai_profile:p}});}catch(error){showError(error.message);}}
 fields.forEach(n=>n.addEventListener('input',dirtyNow));$('profile-form').addEventListener('submit',save);$('periodic-form').addEventListener('submit',save);
 ['profile-apply','periodic-apply','kb-apply'].forEach(id=>$(id).addEventListener('click',()=>{if($(id).disabled||dirty||!view?.pending)return;action('/api/admin/operator/apply',{expected_saved_revision:view.saved_config_revision,expected_active_runtime_revision:view.active_runtime_revision});}));
 $('profile-interval-preset').addEventListener('change',()=>{const v=$('profile-interval-preset').value;if(v!=='custom')$('profile-interval').value=v;$('custom-interval').hidden=v!=='custom';dirtyNow();});
 $('template-add').addEventListener('click',()=>{if(locked()||templateValues().length>=12)return;templates([...templateValues(),'']);dirtyNow();$('template-list').lastElementChild.querySelector('textarea').focus();});
 $('profile-refresh-models').addEventListener('click',async()=>{if(busy||!state.connected)return;busy=true;controls();try{const r=await api('/api/admin/ai-profile/models',{});$('profile-models').replaceChildren(...r.models.map(id=>{const n=document.createElement('option');n.value=id;return n;}));toast('Список моделей обновлён. Выбранная модель не изменена.');}catch(error){toast(error.message);}finally{busy=false;controls();}});
 async function knowledge(){
  const k=await api('/api/admin/knowledge');$('kb-state').textContent=`Источников: ${k.sources.length} · Объём: ${k.total_bytes} байт · Фрагментов в индексе: ${k.sources.reduce((n,s)=>n+s.chunks,0)}`;
  $('kb-sources').replaceChildren(...k.sources.map(s=>{const row=document.createElement('li'),label=document.createElement('span');label.textContent=`${s.name} · Фрагментов: ${s.chunks} · ${s.enabled?'Включён':'Выключен'}`;row.append(label);
   for(const [title,path,body] of [[s.enabled?'Выключить':'Включить','enabled',{enabled:!s.enabled}],['Удалить','delete',{}]]){const b=document.createElement('button');b.type='button';b.className='text-button';b.textContent=title;b.addEventListener('click',()=>{if(path==='delete'&&!window.confirm('Удалить источник «'+s.name+'» из базы знаний? Изменение начнёт действовать после применения.'))return;action(`/api/admin/knowledge/${s.id}/${path}`,body);});row.append(b);}return row;}));controls();
 }
 $('kb-text-form').addEventListener('submit',e=>{e.preventDefault();action('/api/admin/knowledge/text',{title:$('kb-title').value,text:$('kb-text').value});});
 $('kb-upload').addEventListener('click',async()=>{const f=$('kb-file').files[0];if(!f||locked())return;if(f.size>131072||!/[.](txt|md)$/i.test(f.name)){showError('Поддерживаются TXT/MD в кодировке UTF-8 до 128 КиБ.');return;}try{const bytes=new Uint8Array(await f.arrayBuffer());let binary='';for(const b of bytes)binary+=String.fromCharCode(b);await action('/api/admin/knowledge/upload',{filename:f.name,content_base64:btoa(binary)});}catch{showError('Не удалось прочитать выбранный файл.');}});
 function presentation(){
  const active=view?.active.ai_profile?.periodic_comments,d=state.delivery,p=lastPeriodic;$('dash-periodic').textContent=active?.enabled?'Включены':'Выключены';
  const unavailable=!state.connected||!d||d.mode!=='AUTOMATIC'||!d.physical_ready||d.paused||d.mobile_one_shot==='SPENT';
  $('periodic-readiness').textContent=active?.enabled&&unavailable?'Автосообщения настроены, но отправка сейчас недоступна':active?.enabled?'Автосообщения включены и ожидают своего интервала':'Автосообщения выключены';
  $('periodic-block-reason').textContent=!state.connected?'Нет связи с локальным сервером.':active?.enabled&&unavailable?window.AdminI18n.blockReason(d,state.status?.live):'';
  if(p){const band=p.next_due_seconds_range;$('periodic-state').textContent=t(p.state);$('periodic-last-send').textContent=p.last_send?new Date(p.last_send).toLocaleString('ru-RU'):'Пока нет';$('periodic-last-result').textContent=t(p.last_result);$('periodic-next').textContent=band?`Примерно через ${band[0]}–${band[1]} сек.`:'Не запланировано';}
 }
 async function status(){try{lastPeriodic=await api('/api/admin/periodic/status');presentation();}catch{$('periodic-state').textContent='Не удалось получить состояние планировщика';}}
 window.setInterval(()=>{if(state.connected)status();},5000);controls();return {render,controls,refresh:async()=>{await knowledge();await status();}};
};
