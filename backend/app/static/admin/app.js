/* Presentation only. Classification, facts, safety and eligibility live on the server. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = {connected: false, busy: false, suite: false, stopSuite: false, csrf: null,
    settings: null, status: null, presets: [], history: new Map(), live: new Map(), stream: null};
  let quality, operator, delivery;
  const liveFeed = createLiveFeed(); state.live = liveFeed.rows;
  let eventRevision = 0;
  const providerName = (name) => ({fake: "Fake · фикстуры", lmstudio: "LM Studio", openai: "OpenAI"}[name] || name);
  const liveName = (value) => value === "operator_required" ? "NEED_OPERATOR" : String(value).toUpperCase();
  const statusName = (value) => ({ready: "REPLY READY", skipped: "SKIPPED", needs_operator: "NEEDS OPERATOR"}[value] || value);
  const statusClass = (value) => ({ready: "good", skipped: "neutral", needs_operator: "warning"}[value] || "neutral");
  const sourceName = (value) => ({playground: "Playground · тест", fake: "Fake · демонстрация", replay: "Replay · фикстура", tiktok: "TikTok LIVE"}[value] || value);
  const time = (value) => value ? new Date(value).toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "—";
  const text = (id, value) => { $(id).textContent = value ?? "—"; };
  function node(tag, className, value) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (value !== undefined) result.textContent = value;
    return result;
  }
  function toast(message) {
    text("toast", message); $("toast").hidden = false;
    clearTimeout(toast.timer); toast.timer = setTimeout(() => { $("toast").hidden = true; }, 3000);
  }
  async function api(path, body) {
    const options = {cache: "no-store", credentials: "omit", headers: {Accept: "application/json"}};
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers["X-Admin-CSRF"] = state.csrf || "";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    let data;
    try { data = await response.json(); } catch { throw new Error("Backend вернул неожиданный ответ."); }
    if (!response.ok) {
      const safeMessages = {csrf_required: "Сессия изменилась. Обновите страницу.",
        same_origin_required: "Запрос должен выполняться с той же локальной страницы.",
        local_access_only: "Панель доступна только локально.", invalid_input: "Проверьте текст и параметры запроса."};
      const error = new Error(safeMessages[data.error] || data.detail || "Операция не выполнена. Проверьте состояние backend.");
      error.status = response.status; throw error;
    }
    return data;
  }
  function openPage(name) {
    const names = {dashboard: "Обзор", live: "Комментарии LIVE", playground: "AI Playground", settings: "Система и настройки"};
    if (!names[name]) return;
    document.querySelectorAll(".page").forEach((section) => { section.hidden = section.id !== `page-${name}`; });
    document.querySelectorAll(".nav-item").forEach((button) => {
      const active = button.dataset.page === name;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    text("breadcrumb-page", names[name]);
    location.hash = name;
  }
  function setConnected(value) {
    state.connected = value;
    if (!value) text('startup-preparation', 'PRE-LIVE NOT PREPARED · backend connection unavailable');
    $("backend-dot").className = `dot ${value ? "good" : "warning"}`;
    text("backend-label", value ? "Работает" : "Нет связи");
    text("dash-backend", value ? "RUNNING" : "Нет связи");
    text("dash-sse", state.stream?.readyState === 1 ? "CONNECTED" : "RECONNECTING");
    $("connection-warning").hidden = value;
    updateControls();
  }
  function renderMetrics(metrics) {
    ["processed", "ready", "skipped", "needs_operator"].forEach((key) => text(`metric-${key.replaceAll("_", "-")}`, metrics[key]));
    text("nav-live-count", metrics.processed);
  }
  function renderLiveStatus(live) {
    liveFeed.select(live.live_session_id); renderLive();
    if (state.status) state.status.live = live;
    text("dash-tiktok", liveName(live.state)); text("dash-live", live.activity.toUpperCase());
    text("dash-queue", live.queue_size);
    text("dash-last-comment", time(live.last_comment_at));
    text("dash-current-comments", live.current_comment_count || 0);
    text("live-mode-label", !live.enabled ? "LIVE ввод отключён" : ({fake: "Имитация LIVE · Fake", replay: "Синтетический Replay", real: "Чтение собственного LIVE"}[live.mode]));
    text("terminal-state", live.terminal_state ? `Завершение: ${liveName(live.terminal_state)}` : "Состояние эфира отдельно от backend");
    text("tiktok-label", live.mode === "real" && live.enabled ? liveName(live.state) : live.enabled ? "Демо" : "Отключён");
    $("tiktok-dot").className = `dot ${live.state === "connected" && live.enabled ? "good" : live.state === "operator_required" || live.terminal_state === "operator_required" ? "warning" : "muted"}`;
    if (live.enabled && !state.live.size) $("live-empty").querySelector("p").textContent = "Ожидаем комментарии от текущего входного потока. Карточки появятся автоматически.";
    operator?.controls();
  }
  function renderAI(ai) {
    text("dash-provider", providerName(ai.provider)); text("dash-model", ai.model);
    text("ai-panel-title", ai.provider === "lmstudio" ? "Локальный LM Studio" : providerName(ai.provider));
    text("ai-configured-model", ai.model);
    text("ai-model-found", ai.model_found === null ? "Не проверено" : ai.model_found ? "Да" : "Нет");
    text("ai-last-check", time(ai.last_check));
    let label = "Не проверен", message = "Нажмите «Проверить AI». Проверка не генерирует ответ.", color = "neutral";
    if (ai.state === "checking") { label = "Проверка"; message = "Проверяем локальный сервер и наличие модели…"; color = "info"; }
    else if (ai.model_found === false) { label = "Модель отсутствует"; message = "Сервер доступен, но настроенная модель не найдена. Загрузите её в LM Studio."; color = "warning"; }
    else if (ai.state === "available") { label = "Доступен"; message = ai.provider === "fake" ? "Детерминированные фикстуры. Сеть и API key не нужны." : "Сервер доступен, настроенная модель найдена. Качество ответа проверяется отдельно."; color = "good"; }
    else if (ai.state === "unavailable") { label = "Недоступен"; message = "Локальный AI недоступен или ответ сервера не прошёл проверку. Проверьте LM Studio и выбранную модель."; color = "warning"; }
    else if (ai.provider === "openai") { label = "Платный API"; message = "Проверка доступа — только отдельным подтверждённым анализом. Фоновых платных запросов нет."; color = "info"; }
    text("ai-status-badge", label); $("ai-status-badge").className = `badge ${color}`;
    text("ai-status-message", message); text("ai-label", label);
    $("ai-dot").className = `dot ${color === "neutral" ? "muted" : color === "info" ? "info" : color}`;
  }
  function renderPreparation(value) {
    text('startup-preparation', `${value?.state || 'PRE-LIVE NOT PREPARED'}${value?.result && value.result !== 'READY' ? ' · ' + value.result : ''}`);
  }
  function renderStatus(value) {
    renderPreparation(value.preparation);
    state.status = value; renderLiveStatus(value.live); renderAI(value.ai); renderMetrics(value.metrics);
    text("playground-count", value.playground_processed);
    text("dash-policy", value.reply_policy.toUpperCase()); text("dash-temperature", value.temperature);
    renderUptime();
  }
  function renderUptime() {
    if (!state.status) return;
    const seconds = Math.max(0, Math.floor((Date.now() - new Date(state.status.started_at).getTime()) / 1000));
    text("uptime", `${Math.floor(seconds / 3600).toString().padStart(2, "0")}:${Math.floor(seconds % 3600 / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`);
  }
  function settingsList(id, items) {
    $(id).replaceChildren(...items.map(([label, value]) => {
      const row = node("div"); row.append(node("dt", "", label), node("dd", "", String(value ?? "—"))); return row;
    }));
  }
  function renderSettings(settings) {
    state.settings = settings;
    text("dash-policy", settings.reply_policy.toUpperCase()); text("dash-temperature", settings.temperature);
    text("playground-provider", `${providerName(settings.provider)} / ${settings.model}`);
    $("comment-input").maxLength = settings.max_comment_chars;
    $("paid-notice").hidden = settings.provider !== "openai";
    $("run-test-set").title = settings.provider === "openai" ? "Для OpenAI доступны только отдельные подтверждённые запросы." : "Последовательно проверить 9 примеров";
    settingsList("product-settings", [["Продукт", settings.product_name], ["Платформа", settings.platform],
      ["Язык эфира", settings.stream_language], ["LIVE ввод", settings.live_enabled ? "Включён" : "Отключён"], ["Режим входа", settings.live_mode]]);
    settingsList("ai-settings", [["Provider", providerName(settings.provider)], ["Model", settings.model], ["Язык оператора", settings.operator_language],
      ["Temperature", settings.temperature], ["Thinking", settings.thinking_enabled ? "Включён" : "Выключен"], ["LM Studio URL", settings.lmstudio_base_url]]);
    settingsList("reply-settings", [["Символов в ответе", settings.max_reply_chars], ["Предложений", settings.max_reply_sentences],
      ["Символов в комментарии", settings.max_comment_chars], ["Output tokens", settings.max_model_output_tokens], ["Timeout", `${settings.timeout_seconds} с`]]);
    updateControls();
  }
  function updateControls() {
    operator?.controls();
    quality?.controls();
    const configured = Boolean(state.settings && state.connected && state.csrf);
    const value = $("comment-input").value;
    const limit = state.settings?.max_comment_chars || 1000;
    text("char-count", `${Array.from(value).length} / ${limit}`);
    const paidAllowed = state.settings?.provider !== "openai" || $("allow-paid").checked;
    $("analyze-button").disabled = !configured || state.busy || quality?.active() || !value.trim() || Array.from(value).length > limit || !paidAllowed;
    $("comment-input").disabled = state.busy;
    $("allow-paid").disabled = state.busy;
    $("run-test-set").disabled = !configured || state.busy || quality?.active() || state.settings?.provider === "openai";
    $("check-ai").disabled = !configured || state.checking;
    document.querySelectorAll(".preset-button").forEach((button) => { button.disabled = state.busy; });
    $("analyze-button").classList.toggle("loading", state.busy);
  }
  function field(container, label, value, className = "reply-field") {
    const block = node("div", className);
    block.append(node("div", "reply-field-label", label), node("div", "reply-field-text", value || "—"));
    container.append(block);
  }
  function replyCard(record) {
    const item = record.suggestion, card = node("div", "reply-card");
    const top = node("div", "reply-top"), author = node("div");
    author.append(node("div", "reply-author", `@${item.username}`), node("div", "reply-source", `${sourceName(record.source)} · ${time(record.received_at)}`));
    top.append(author, node("span", `badge ${statusClass(item.status)}`, statusName(item.status))); card.append(top);
    if (item.translation_requires_review) card.append(node("div", "review-banner", "TRANSLATION REVIEW REQUIRED · Машинный перевод ответа требует отдельной проверки оператором."));
    if (record.original_truncated) card.append(node("div", "review-banner", "Оригинал сокращён до 10 000 символов для отображения."));
    field(card, `ОРИГИНАЛ · ${item.detected_language}`, item.original_text);
    field(card, "ПЕРЕВОД КОММЕНТАРИЯ · RU", item.operator_translation);
    field(card, "ПРЕДЛОЖЕННЫЙ ОТВЕТ", item.suggested_reply, "reply-box");
    field(card, "ПЕРЕВОД ОТВЕТА · RU", item.reply_translation);
    const metadata = node("div", "reply-meta");
    [["INTENT", item.intent], ["SHOULD REPLY", String(item.should_reply)], ["FACT IDS", item.fact_ids.join(", ") || "—"],
      ["PROVIDER / MODEL", `${providerName(item.provider)} / ${item.model || "фикстуры"}`], ["ВРЕМЯ АНАЛИЗА", `${record.duration_ms} мс`],
      ["PROCESSING ERROR", item.processing_error || "—"], ["ERROR DETAIL", item.processing_error_detail || "—"],
      ["AI AUTO AUTHORITY", String(item.auto_send_eligible)], ["TRANSLATION REVIEW", String(item.translation_requires_review)],
      ["TRANSLATION SOURCE", item.translation_source]].forEach(([label, value]) => {
        const cell = node("div"); cell.append(node("div", "reply-field-label", label), node("div", "value mono", value)); metadata.append(cell);
      });
    card.append(metadata);
    const flags = node("div", "flag-list");
    flags.append(node("span", "badge warning", item.requires_approval ? "REQUIRES APPROVAL" : "APPROVAL STATUS UNKNOWN"),
      node("span", "badge neutral", "AI DOES NOT AUTHORIZE AUTO"));
    card.append(flags, node("p", "reply-reason", item.reason));
    const actions = node("div", "reply-actions"), copy = node("button", "button secondary-button", "COPY REPLY");
    copy.type = "button"; copy.disabled = !item.suggested_reply;
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(item.suggested_reply); copy.textContent = "Скопировано"; toast("Ответ скопирован. Ничего не отправлено."); setTimeout(() => { copy.textContent = "COPY REPLY"; }, 1600); }
      catch { toast("Браузер не разрешил копирование. Выделите текст ответа вручную."); }
    });
    const send = node("button", "button send-disabled", "SEND · не подключён"); send.disabled = true; send.title = "Delivery not configured";
    actions.append(copy, send); card.append(actions);
    if (record.delivery_review) {
      const review = record.delivery_review, envelope = review.envelope;
      card.append(node("p", "subtle mono", `ID ${envelope.suggestion_id} · runtime ${envelope.active_runtime_revision.slice(0, 8)} · session ${envelope.live_session_id?.slice(0, 8) || "Playground"}`),
        node("p", "subtle", `Approval: ${review.approval_state} · Delivery: NOT CONFIGURED · ${review.eligibility.reason}`));
      const approve = node("button", "button secondary-button", "APPROVE · без отправки");
      approve.type = "button"; approve.disabled = item.status !== "ready" || review.approval_state !== "missing" || review.consumed;
      let translationCheck = null;
      if (item.translation_requires_review) {
        const label = node("label", "field-label", "Я проверил перевод ответа");
        translationCheck = node("input"); translationCheck.type = "checkbox"; label.prepend(translationCheck); card.append(label);
      }
      approve.addEventListener("click", async () => {
        approve.disabled = true;
        try {
          const next = await api(`/api/admin/suggestions/${envelope.suggestion_id}/approve`, {
            expected_suggestion_revision: envelope.suggestion_revision,
            translation_review_confirmed: Boolean(translationCheck?.checked)});
          record.delivery_review = next; card.replaceWith(replyCard(record)); toast("Approval записан. Ничего не отправлено.");
        } catch (error) {
          toast(error.message);
          try { record.delivery_review = await api(`/api/admin/suggestions/${envelope.suggestion_id}`); card.replaceWith(replyCard(record)); }
          catch { approve.disabled = Boolean(review.consumed); }
        }
      });
      actions.prepend(approve);
    }
    delivery?.attach(actions, record, send);
    return card;
  }
  function showResult(record) {
    state.selectedSuggestion = record.id;
    $("result-empty").hidden = true; $("result-content").hidden = false;
    $("result-content").replaceChildren(replyCard(record)); text("result-duration", `${record.duration_ms} мс`);
  }
  async function refreshReviews() {
    const liveToken = liveFeed.token();
    const version = state.reviewRefreshVersion = (state.reviewRefreshVersion || 0) + 1;
    const [history, live] = await Promise.all([api("/api/admin/playground/history"), api("/api/admin/live/history")]);
    if (version !== state.reviewRefreshVersion) return;
    state.history = new Map(history.map((record) => [record.id, record]));
    liveFeed.hydrate(live, liveToken);
    live.forEach(r => { if (r.live_session_id === state.status?.live?.live_session_id && state.live.has(r.id) && r.suggestion) liveFeed.put(r); });
    renderHistory(); renderLive();
    if (state.selectedSuggestion && state.history.has(state.selectedSuggestion)) showResult(state.history.get(state.selectedSuggestion));
  }
  function trimMap(map, max) {
    const ordered = [...map.values()].sort((a, b) => new Date(b.received_at || b.timestamp) - new Date(a.received_at || a.timestamp));
    map.clear(); ordered.slice(0, max).forEach((value) => map.set(value.id, value));
  }
  function remember(record) { state.history.set(record.id, record); trimMap(state.history, 100); renderHistory(); }
  function renderHistory() {
    text("history-count", state.history.size); $("history-empty").hidden = state.history.size > 0;
    const rows = [...state.history.values()].map((record) => {
      const item = record.suggestion, row = node("tr"); row.tabIndex = 0;
      const values = [item.original_text, item.detected_language, item.intent, statusName(item.status), item.suggested_reply || "—", `${record.duration_ms} мс`];
      values.forEach((value, i) => {
        const cell = node("td"); cell.append(node(i === 3 ? "span" : "div", i === 3 ? `badge ${statusClass(item.status)}` : "cell-text", value)); row.append(cell);
      });
      const select = () => { showResult(record); openPage("playground"); $("result-panel").scrollIntoView({block: "nearest"}); };
      row.addEventListener("click", select); row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } });
      return row;
    });
    $("history-body").replaceChildren(...rows);
  }
  function renderLive() {
    $("live-empty").hidden = state.live.size > 0;
    $("live-cards").replaceChildren(...[...state.live.values()].map((record) => {
      const panel = node("article", "panel");
      if (record.suggestion) panel.append(replyCard(record));
      else { panel.append(node("span", "badge info", "Обработка AI")); field(panel, `@${record.username} · ${sourceName(record.source)} · ${time(record.timestamp)}`, record.text); }
      return panel;
    }));
  }
  function renderPresets() {
    $("preset-buttons").replaceChildren(...state.presets.map((preset) => {
      const button = node("button", "preset-button", preset.label); button.type = "button"; button.title = preset.text;
      button.addEventListener("click", () => { $("comment-input").value = preset.text; updateControls(); $("comment-input").focus(); }); return button;
    }));
  }
  async function analyze(value, paid = false) {
    const record = await api("/api/admin/playground/analyze", {text: value, allow_paid: paid});
    remember(record); showResult(record); return record;
  }
  async function refresh() {
    const revision = eventRevision, liveToken = liveFeed.token();
    const [session, status, settings, presets, history, live, operatorSettings] = await Promise.all([
      api("/api/admin/session"), api("/api/admin/status"), api("/api/admin/settings"),
      api("/api/admin/playground/presets"), api("/api/admin/playground/history"), api("/api/admin/live/history"), api("/api/admin/operator/settings")]);
    state.csrf = session.csrf_token; renderSettings(settings);
    if (revision === eventRevision) renderStatus(status);
    state.presets = presets; renderPresets();
    state.history = new Map(history.map((record) => [record.id, record])); trimMap(state.history, 100); renderHistory();
    liveFeed.hydrate(live, revision === eventRevision ? liveFeed.token() : liveToken); renderLive();
    setConnected(true);
    operator.render(operatorSettings);
    await quality.refresh();
    await delivery.refresh();
  }
  function connectEvents() {
    if (state.stream) return;
    const stream = new EventSource("/api/admin/events"); state.stream = stream;
    const receive = (name, handler) => stream.addEventListener(name, (event) => {
      try { eventRevision++; handler(JSON.parse(event.data)); } catch { toast("Не удалось обновить данные. Переподключение восстановит историю."); }
    });
    stream.onopen = () => { text("dash-sse", "CONNECTED"); refresh().catch(() => setConnected(false)); };
    stream.onerror = () => setConnected(false);
    receive("snapshot", renderStatus);
    receive('preparation_status', renderPreparation);
    receive("quality_update", quality.apply);
    receive("operator_settings", (value) => { operator.render(value); refreshReviews().catch(() => setConnected(false)); });
    receive("suggestion_approval", () => { refresh().catch(() => setConnected(false)); });
    receive("windows_sender_status", () => { delivery.refresh().catch(() => setConnected(false)); });
    receive("live_state", (value) => {
      const changed = value.state !== state.status?.live?.state || value.live_session_id !== state.status?.live?.live_session_id;
      renderLiveStatus(value);
      if (changed) refreshReviews().catch(() => setConnected(false));
    });
    receive("ai_status", renderAI);
    receive("reply_suggestion", (value) => { if (liveFeed.put(value.record)) { renderLive(); renderMetrics(value.metrics); } });
    receive("comment_received", (value) => { if (liveFeed.put(value)) renderLive(); });
    receive("playground_result", (value) => { remember(value.record); if (state.status) state.status.playground_processed = value.processed; text("playground-count", value.processed); });
    receive("resync", () => { refresh().catch(() => setConnected(false)); });
  }
  quality = window.createQualityLab({api, node, replyCard, toast, state, updateControls});
  operator = window.createOperatorControls({api, state, toast, renderSettings, renderStatus,
    refreshQuality: async () => { await quality.refresh(); await delivery.refresh(); }});
  delivery = window.createDeliveryControls({api, node, toast, state, refreshReviews, refreshCards: () => {
    renderLive();
    if (state.selectedSuggestion && state.history.has(state.selectedSuggestion)) showResult(state.history.get(state.selectedSuggestion));
  }});
  document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => openPage(button.dataset.page)));
  document.querySelectorAll("[data-open]").forEach((button) => button.addEventListener("click", () => openPage(button.dataset.open)));
  $("comment-input").addEventListener("input", updateControls); $("allow-paid").addEventListener("change", updateControls);
  $("analyze-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if ($("analyze-button").disabled) return;
    state.busy = true; $("playground-error").hidden = true; text("analysis-progress", "Анализ…"); updateControls();
    try { await analyze($("comment-input").value, $("allow-paid").checked); text("analysis-progress", "Готово"); }
    catch (error) { text("playground-error", error.message); $("playground-error").hidden = false; text("analysis-progress", "Ошибка"); }
    finally { state.busy = false; $("allow-paid").checked = false; updateControls(); }
  });
  $("check-ai").addEventListener("click", async () => {
    state.checking = true; updateControls();
    try { renderAI(await api("/api/admin/ai/check", {})); }
    catch (error) { toast(error.message); }
    finally { state.checking = false; updateControls(); }
  });
  $("run-test-set").addEventListener("click", async () => {
    if ($("run-test-set").disabled) return;
    state.busy = true; state.suite = true; state.stopSuite = false;
    $("stop-test-set").hidden = false; $("playground-error").hidden = true; updateControls();
    let completed = 0;
    try {
      for (const preset of state.presets) {
        if (state.stopSuite || !state.connected) break;
        $("comment-input").value = preset.text; updateControls();
        text("suite-progress", `Проверка ${completed + 1} / ${state.presets.length} · ${preset.label}`);
        await analyze(preset.text, false); completed += 1;
      }
      text("suite-progress", `${state.stopSuite ? "Остановлено" : "Завершено"}: ${completed} / ${state.presets.length}. Оценку смысла выполняет оператор.`);
    } catch (error) { text("playground-error", error.message); $("playground-error").hidden = false; }
    finally { state.busy = false; state.suite = false; $("stop-test-set").hidden = true; updateControls(); }
  });
  $("stop-test-set").addEventListener("click", () => { state.stopSuite = true; text("suite-progress", "Остановим набор после текущего запроса…"); });
  window.addEventListener("hashchange", () => openPage(location.hash.slice(1) || "dashboard"));
  window.addEventListener("pagehide", () => { state.stream?.close(); });
  openPage(location.hash.slice(1) || "dashboard");
  // Initial load is observation only; no generation or model health request.
  refresh().then(connectEvents).catch(() => { setConnected(false); connectEvents(); });
  setInterval(renderUptime, 1000); // Local display timer, no network polling.
})();
