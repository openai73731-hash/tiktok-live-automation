/* Render server evaluation only. No prompts, classification or grading in JS. */
"use strict";
window.createQualityLab = ({api, node, replyCard, toast, state, updateControls}) => {
  const $ = (id) => document.getElementById(id);
  let dataset = null, run = null, results = new Map(), rows = new Map(), pending = false;
  let generation = 0, requestNumber = 0;
  const retiredEpochs = new Set();
  const active = () => pending || ["running", "stopping"].includes(run?.state);
  const pct = (value) => value === null ? "—" : `${value}%`;
  const ms = (value) => value === null ? "—" : `${value} мс`;
  function controls() {
    $("quality-start").disabled = pending || active() || !state.connected || state.busy || !run?.enabled;
    $("quality-stability").disabled = $("quality-start").disabled;
    $("quality-stop").disabled = pending || !state.connected || run?.state !== "running";
    $("quality-export").disabled = !run?.run_id || active();
  }
  function metric(label, value) {
    const card = node("div", "stat-card");
    card.append(node("span", "stat-label", label), node("strong", "quality-value", value)); return card;
  }
  function renderSummary() {
    const s = run.summary;
    $("quality-summary").replaceChildren(
      metric("ЗАВЕРШЕНО / КОРПУС", `${s.total} / ${s.dataset_total}`), metric("PASS / FAIL", `${s.passed} / ${s.failed}`),
      metric("CRITICAL FAIL", s.critical_failed), metric("SHOULD REPLY", pct(s.should_reply_accuracy)),
      metric("INTENT", pct(s.intent_accuracy)), metric("FACT SET MATCH", pct(s.fact_ids_accuracy)),
      metric("SAFE REPLY / READY", `${s.safe_reply_count} / ${s.ready_count}`),
      metric("NEEDS OPERATOR / SKIPPED", `${s.needs_operator_count} / ${s.skipped_count}`),
      metric("INVALID OUTPUT / TRANSLATION REJECTED", `${s.invalid_output_count} / ${s.translation_rejected_count}`),
      metric("AVG / P50 / P95", `${ms(s.average_duration_ms)} / ${ms(s.p50_duration_ms)} / ${ms(s.p95_duration_ms)}`));
    const groups = ["languages", "categories"].map((key) => {
      const box = node("div"); box.append(node("h3", "", key === "languages" ? "Языки" : "Категории"));
      Object.entries(s[key]).forEach(([name, v]) => box.append(node("p", "mono", `${name}: ${v.passed} PASS · ${v.failed} FAIL / ${v.total}`)));
      return box;
    });
    $("quality-breakdown").replaceChildren(...groups);
    const labels = {idle: "Не запускался", running: "RUNNING", stopping: "STOPPING — завершаем текущий кейс", completed: "COMPLETED", stopped: "STOPPED", failed: "FAILED"};
    $("quality-progress").textContent = `${labels[run.state]} · прогон ${run.run_number}/${run.runs_requested} · ${s.total}/${s.dataset_total}${run.current_case_id ? ` · ${run.current_case_id}` : ""}`;
    $("quality-provider").textContent = `${run.provider} / ${run.model} · T=${run.temperature}`;
    $("quality-dataset").textContent = `${run.dataset_name} · v${run.dataset_version} · ${s.dataset_total} cases`;
    $("quality-error").hidden = !run.disabled_reason && !run.error_category;
    $("quality-error").textContent = run.disabled_reason || (run.error_category ? "Прогон не завершён. Требуется оператор." : "");
    renderStability();
  }
  function renderStability() {
    const s = run.stability, range = s.pass_min === null ? "—" : `${s.pass_min}–${s.pass_max}`;
    $("quality-stability-summary").replaceChildren(
      metric("ЗАВЕРШЕНО ПРОГОНОВ", `${s.runs_completed} / ${s.runs_requested}`),
      metric("КОММЕНТАРИИ / MODEL POST", `${s.total_generations} / ${s.model_generation_calls ?? "—"}`),
      metric("PASS MIN–MAX / MEAN", `${range} / ${pct(s.mean_pass_rate)}`),
      metric("CRITICAL FAIL ПО ПРОГОНАМ", s.critical_fail_counts.join(" / ") || "—"),
      metric("SHOULD REPLY MEAN / MIN", `${pct(s.should_reply_mean)} / ${pct(s.should_reply_min)}`),
      metric("INTENT MEAN / MIN", `${pct(s.intent_mean)} / ${pct(s.intent_min)}`),
      metric("FACT SET MEAN / MIN", `${pct(s.fact_ids_mean)} / ${pct(s.fact_ids_min)}`),
      metric("INVALID / TRANSLATION REJECTED", `${s.invalid_output_count} / ${s.translation_rejected_count}`),
      metric("PASS CONSISTENCY", Object.entries(s.pass_consistency).map(([key, count]) => `${key}: ${count}`).join(" · ") || "—"));
    const table = node("table"), head = node("thead"), header = node("tr"), body = node("tbody");
    ["Прогон", "PASS / FAIL", "Critical", "Should reply", "Intent", "Fact SET", "Invalid / translation", "Время AVG / P95"].forEach((title) => header.append(node("th", "", title)));
    head.append(header);
    (run.completed_summaries || []).forEach((trial) => {
      const v = trial.summary, row = node("tr");
      [trial.run_number, `${v.passed} / ${v.failed}`, v.critical_failed, pct(v.should_reply_accuracy), pct(v.intent_accuracy), pct(v.fact_ids_accuracy), `${v.invalid_output_count} / ${v.translation_rejected_count}`, `${ms(v.average_duration_ms)} / ${ms(v.p95_duration_ms)}`].forEach((value) => row.append(node("td", "", value)));
      body.append(row);
    });
    table.append(head, body); $("quality-trials").replaceChildren(table);
    $("quality-consistency").replaceChildren(...s.cases.map((c) => node("p", c.flapping ? "warning-text mono" : "mono", `${c.case_id}: ${c.passes}/${c.trials} PASS${c.flapping ? " · FLAPPING" : ""}${c.selection_consistent === false ? " · selection varies" : ""}`)));
    const trials = run.completed_summaries.some((trial) => trial.run_number === run.run_number) ? run.completed_summaries : [...run.completed_summaries, run];
    $("quality-fingerprints").replaceChildren(...trials.map((trial) => node("p", "", `Прогон ${trial.run_number}: ${trial.provider}/${trial.model}, T=${trial.temperature}; dataset=${trial.dataset_hash}; knowledge=${trial.knowledge_hash}; AI=${trial.ai_config_hash}; analysis=${trial.analysis_prompt_hash || "—"}; translation=${trial.translation_prompt_hash || "—"}`)));
  }
  function rowFor(c) {
    const row = node("tr"), cells = Array.from({length: 6}, () => node("td"));
    cells[0].append(node("strong", "mono", c.id), node("div", "subtle", c.category), node("div", "subtle", c.language));
    cells[1].append(node("div", "cell-text", c.viewer_text));
    cells[2].append(node("div", "subtle", `Expected: ${c.expected.intent} · reply=${c.expected.should_reply}`));
    cells[3].append(node("div", "subtle", `Expected: ${c.expected.fact_ids.join(", ") || "—"}`));
    const intent = node("div", "mono", "Actual: —"), facts = node("div", "mono", "Actual: —"), status = node("div", "mono", "Не запускался");
    cells[2].append(intent); cells[3].append(facts); cells[4].append(status);
    const badge = node("span", "badge neutral", "NOT RUN"), details = node("details", "quality-details");
    details.append(node("summary", "", "Открыть"));
    const content = node("div", "quality-detail-content");
    if (c.notes) content.append(node("p", "notice", c.notes));
    details.append(content); cells[5].append(badge, details); row.append(...cells);
    rows.set(c.id, {row, intent, facts, status, badge, details, content}); return row;
  }
  function renderReview(container, c, result) {
    const owner = run.run_id, runNumber = run.run_number;
    const form = node("form", "quality-review"), grades = {};
    const names = {comment_translation_quality: "COMMENT TRANSLATION", reply_naturalness: "REPLY NATURALNESS", reply_translation_quality: "REPLY TRANSLATION"};
    Object.entries(names).forEach(([key, label]) => {
      const wrapper = node("label", "field-label", label), select = node("select");
      select.setAttribute("aria-label", `${c.id} ${label}`);
      ["unreviewed", "good", "bad"].forEach((value) => { const option = node("option", "", value.toUpperCase()); option.value = value; select.append(option); });
      select.value = result.review[key]; wrapper.append(select); form.append(wrapper); grades[key] = select;
    });
    const label = node("label", "field-label", "Заметка оператора"), note = node("textarea"); note.rows = 3; note.maxLength = 1000; note.value = result.review.note;
    note.setAttribute("aria-label", `${c.id} заметка`); label.append(note); form.append(label);
    const save = node("button", "button secondary-button", "Сохранить оценку"), message = node("span", "subtle"); save.type = "submit"; form.append(save, message);
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); save.disabled = true;
      try {
        const review = Object.fromEntries(Object.entries(grades).map(([key, select]) => [key, select.value])); review.note = note.value;
        await api("/api/admin/quality/review", {run_id: owner, run_number: runNumber, case_id: c.id, review});
        // Re-read the bounded canonical state; SSE may already have delivered it.
        await refresh(); toast("Ручная оценка сохранена в памяти.");
      } catch (error) { message.textContent = error.message; }
      finally { save.disabled = false; }
    });
    container.append(node("h3", "", "Ручная оценка · не меняет PASS/FAIL"), form);
  }
  function renderResult(c, result) {
    const refs = rows.get(c.id), item = result.record.suggestion;
    refs.intent.textContent = `Actual: ${item.intent} · reply=${item.should_reply}`;
    refs.facts.textContent = `Actual: ${item.fact_ids.join(", ") || "—"}`;
    refs.status.textContent = `${item.status.toUpperCase()} · ${result.record.duration_ms} мс`;
    refs.badge.textContent = result.passed ? "PASS" : "FAIL"; refs.badge.className = `badge ${result.passed ? "good" : "warning"}`;
    refs.content.replaceChildren();
    if (c.notes) refs.content.append(node("p", "notice", c.notes));
    refs.content.append(node("p", result.critical_failures.length ? "error-message" : "subtle", `CRITICAL: ${result.critical_failures.join(", ") || "нет"}`),
      node("p", "subtle", `Прочие проверки: ${result.other_failures.join(", ") || "нет ошибок"}`));
    const checks = node("dl", "quality-checks");
    Object.entries(result.checks).forEach(([name, pass]) => { const pair = node("div"); pair.append(node("dt", "mono", name), node("dd", pass ? "good-text" : "warning-text", pass ? "PASS" : "FAIL")); checks.append(pair); });
    refs.content.append(checks, replyCard(result.record)); renderReview(refs.content, c, result);
  }
  function apply(value) {
    if (retiredEpochs.has(value.run.service_epoch)) return;
    if (run && value.run.service_epoch === run.service_epoch && value.run.revision < run.revision) return;
    const epochChanged = value.run.service_epoch !== run?.service_epoch;
    if (epochChanged) {
      if (run) retiredEpochs.add(run.service_epoch);
      generation += 1;
    }
    const replaced = epochChanged || value.run.run_id !== run?.run_id || value.run.run_number !== run?.run_number;
    if (epochChanged) { dataset = null; results.clear(); rows.clear(); $("quality-cases").replaceChildren(); }
    run = value.run;
    if (value.dataset) dataset = value.dataset;
    if (!dataset) { refresh().catch((error) => toast(error.message)); return; }
    if (replaced || !rows.size) { results.clear(); rows.clear(); $("quality-cases").replaceChildren(...dataset.cases.map(rowFor)); }
    const changed = value.results || (value.result ? [value.result] : []);
    changed.forEach((result) => { results.set(result.case_id, result); const c = dataset.cases.find((entry) => entry.id === result.case_id); if (c) renderResult(c, result); });
    renderSummary(); controls(); updateControls();
  }
  async function refresh() {
    const started = generation, request = ++requestNumber;
    const value = await api("/api/admin/quality");
    if (started !== generation || request !== requestNumber) return;
    apply(value);
  }
  async function start(runs) {
    pending = true; controls(); updateControls();
    try { apply({run: await api("/api/admin/quality/start", {runs})}); }
    catch (error) { toast(error.message); }
    finally { pending = false; controls(); updateControls(); }
  }
  $("quality-start").addEventListener("click", () => start(1));
  $("quality-stability").addEventListener("click", () => start(3));
  $("quality-stop").addEventListener("click", async () => {
    pending = true; controls();
    try { apply({run: await api("/api/admin/quality/stop", {run_id: run.run_id})}); }
    catch (error) { toast(error.message); }
    finally { pending = false; controls(); }
  });
  $("quality-export").addEventListener("click", async () => {
    let report;
    try { report = await api("/api/admin/quality"); }
    catch (error) { toast(error.message); return; }
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type: "application/json"}));
    const a = node("a"); a.href = url; a.download = `quality-${report.run.run_id}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  function tab(quality) {
    $("quality-lab").hidden = !quality; $("playground-standard").hidden = quality;
    $("quality-tab").setAttribute("aria-pressed", String(quality)); $("standard-tab").setAttribute("aria-pressed", String(!quality));
  }
  $("quality-tab").addEventListener("click", () => tab(true)); $("standard-tab").addEventListener("click", () => tab(false));
  return {refresh, apply, controls, active};
};
