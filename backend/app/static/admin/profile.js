/* One generic profile. All authority, validation and source storage are backend-owned. */
window.createProfileControls = ({api, state, toast}) => {
  const $ = id => document.getElementById(id);
  let view = null, dirty = false, busy = false, revision = null;
  const value = name => $("profile-" + name).value;
  const number = name => Number(value(name));
  const fields = Array.from($("profile-form").querySelectorAll("input,textarea,select"));
  function controls() {
    const locked = busy || !state.connected || view?.locked || state.status?.live?.connection_owned;
    fields.forEach(field => { field.disabled = Boolean(locked); });
    $("profile-save").disabled = locked || !view || !dirty;
    $("profile-apply").disabled = locked || dirty || !view?.pending;
    $("profile-refresh-models").disabled = busy || !state.connected;
    $("kb-text-form").querySelectorAll("input,textarea,button").forEach(n => { n.disabled = Boolean(locked); });
    $("kb-file").disabled = $("kb-upload").disabled = Boolean(locked);
    $("kb-sources").querySelectorAll("button").forEach(n => { n.disabled = Boolean(locked); });
  }
  function render(next, force = false) {
    view = next;
    if (!dirty || force) {
      const p = next.saved.ai_profile || next.desired.ai_profile || next.profile_defaults;
      if (!p) return;
      const m = p.model, c = p.periodic_comments;
      const values = {name:p.name,topic:p.topic,language:p.language,tone:p.tone,system:p.system_prompt,
        instruction:p.reply_instruction,provider:m.provider,url:m.base_url,model:m.model,temperature:m.temperature,tokens:m.max_tokens,
        "top-k":p.knowledge.top_k,interval:c.interval_seconds/60,jitter:c.jitter_seconds,"periodic-mode":c.mode,
        "max-hour":c.max_per_hour,"periodic-instruction":c.instruction,templates:c.templates.join("\n")};
      Object.entries(values).forEach(([k,v]) => { $("profile-"+k).value = v; });
      $("profile-kb-enabled").checked = p.knowledge.enabled;
      $("profile-periodic-enabled").checked = c.enabled;
      revision = next.saved_config_revision; dirty = false;
    }
    $("profile-state").textContent = dirty ? "Есть несохранённые изменения" : next.pending ? "Сохранено; требуется Apply" : next.active.ai_profile ? "Активный generic profile: " + next.active.ai_profile.name : "Legacy profile активен. Save / Apply включает generic profile.";
    controls();
  }
  function draft() {
    return {name:value("name"),topic:value("topic"),language:value("language"),tone:value("tone"),system_prompt:value("system"),reply_instruction:value("instruction"),
      model:{provider:value("provider"),base_url:value("url"),model:value("model"),temperature:number("temperature"),max_tokens:number("tokens")},
      knowledge:{enabled:$("profile-kb-enabled").checked,top_k:number("top-k")},
      periodic_comments:{enabled:$("profile-periodic-enabled").checked,interval_seconds:number("interval")*60,jitter_seconds:number("jitter"),
        mode:value("periodic-mode"),max_per_hour:number("max-hour"),instruction:value("periodic-instruction"),templates:value("templates").split("\n").map(s=>s.trim()).filter(Boolean)}};
  }
  async function action(path, body) {
    if (busy) return;
    busy = true; controls(); $("profile-error").hidden = true;
    try {
      await api(path,body);
      render(await api("/api/admin/operator/settings"),path.startsWith('/api/admin/operator/')); await knowledge(); toast("Сохранено. Apply меняет активный профиль и KB.");
    } catch (error) { $("profile-error").textContent = error.message; $("profile-error").hidden = false; }
    finally { busy = false; controls(); }
  }
  fields.forEach(n=>n.addEventListener("input",()=>{dirty=true;$("profile-state").textContent="Есть несохранённые изменения";controls();}));
  $("profile-form").addEventListener("submit",e=>{ e.preventDefault(); if (!$("profile-save").disabled) action("/api/admin/operator/settings",{expected_saved_revision:revision,settings:{...view.saved,ai_profile:draft()}}); });
  $("profile-apply").addEventListener("click",()=>action("/api/admin/operator/apply",{expected_saved_revision:view.saved_config_revision,expected_active_runtime_revision:view.active_runtime_revision}));
  $("profile-refresh-models").addEventListener("click",async()=>{
    try { const response=await api("/api/admin/ai-profile/models",{}); const options=response.models.map(id=>{const n=document.createElement("option");n.value=id;return n;});$("profile-models").replaceChildren(...options);toast("Модели обновлены. Выбранное значение не изменено."); }
    catch(error){toast(error.message);}
  });
  async function knowledge() {
    const k=await api("/api/admin/knowledge");
    $("kb-state").textContent = `${k.sources.length} sources · ${k.total_bytes} bytes · ${k.sources.reduce((n,s)=>n+s.chunks,0)} indexed chunks`;
    const rows=k.sources.map(s=>{
      const row=document.createElement("li"), label=document.createElement("span");label.textContent=`${s.name} · ${s.chunks} chunks · ${s.enabled?"enabled":"disabled"} `;row.append(label);
      for(const [text,path,body] of [[s.enabled?"Disable":"Enable","enabled",{enabled:!s.enabled}],["Delete","delete",{}]]) {
        const b=document.createElement("button");b.type="button";b.className="text-button";b.textContent=text;b.addEventListener("click",()=>action(`/api/admin/knowledge/${s.id}/${path}`,body));row.append(b);
      } return row;
    });$("kb-sources").replaceChildren(...rows);controls();
  }
  $("kb-text-form").addEventListener("submit",e=>{e.preventDefault();action("/api/admin/knowledge/text",{title:$("kb-title").value,text:$("kb-text").value});});
  $("kb-upload").addEventListener("click",async()=>{
    const f=$("kb-file").files[0];if(!f)return;
    if(f.size>131072||!/[.](txt|md)$/i.test(f.name)){toast("Только UTF-8 TXT/MD до 128 KiB.");return;}
    const bytes=new Uint8Array(await f.arrayBuffer());let binary="";for(const b of bytes)binary+=String.fromCharCode(b);
    action("/api/admin/knowledge/upload",{filename:f.name,content_base64:btoa(binary)});
  });
  async function status() {
    try {const p=await api("/api/admin/periodic/status");const band=p.next_due_seconds_range;$("periodic-state").textContent=`${p.state} · last: ${p.last_result||"—"} · last send: ${p.last_send?new Date(p.last_send).toLocaleTimeString():"—"} · next: ${band?`примерно через ${band[0]}–${band[1]} сек.`:"—"}`;}catch{}
  }
  window.setInterval(()=>{if(state.connected)status();},5000);
  controls();
  return {render,controls,refresh:async()=>{await knowledge();await status();}};
};
