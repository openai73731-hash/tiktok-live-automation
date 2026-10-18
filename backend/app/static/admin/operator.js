/* Presentation of allowlisted operator settings and explicit LIVE actions. */
window.createOperatorControls = ({api, state, toast, renderSettings, renderStatus, refreshQuality}) => {
  const t = window.AdminI18n?.text || (v=>v);
  const $ = (id) => document.getElementById(id);
  const fields = ["username", "provider", "model", "temperature", "timeout", "policy", "sentences", "chars", "device"];
  let view = null, dirty = false, busy = false, editedRevision = null;
  const value = (name) => $("operator-" + name).value;
  const transport = () => document.querySelector('input[name="operator-transport"]:checked')?.value || "android";
  function controls() {
    const live = state.status?.live;
    const owned = Boolean(live?.connection_owned);
    const locked = owned || Boolean(view?.locked) || busy || state.busy;
    fields.forEach((name) => { $("operator-" + name).disabled = locked || Boolean(view?.desired.ai_profile && ["provider","model","temperature","policy"].includes(name)); });
    document.querySelectorAll('input[name="operator-transport"]').forEach(input => { input.disabled = locked; });
    $("transport-saved").textContent = view?.saved_transport === "windows_live_studio" ? "Компьютер / LIVE Studio" : "Телефон Android";
    $("transport-active").textContent = view?.active_transport === "windows_live_studio" ? "Компьютер / LIVE Studio" : "Телефон Android";
    $("operator-save").disabled = !view || !state.connected || locked || !dirty || value("provider") === "openai";
    $("operator-apply").disabled = !view?.pending || locked || dirty || !state.connected;
    $("operator-reset").disabled = !view || locked || !state.connected;
    $("operator-sentences").max = value("policy") === "friendly" ? "3" : "2";
    $("operator-paid-warning").hidden = value("provider") !== "openai";
    $("operator-lock-reason").textContent = owned ? "Отключите подключение к чату, чтобы изменить аккаунт или настройки ИИ. Сам эфир не останавливается." : view?.lock_reason || "Сохранение и применение не запускают модель или TikTok.";
    $("operator-apply-state").textContent = view?.config_error ? "Локальные настройки повреждены. Исправьте или сбросьте их." : dirty ? "Есть несохранённые изменения" : view?.pending ? "Сохранено. Требуется применить настройки." : "Настройки применены";
    const liveLabel = live?.terminal_state === "operator_required" && owned ? "Требуется оператор" : t(live?.state || "disconnected");
    $("operator-live-state").textContent = liveLabel + (live?.username && owned ? " @" + live.username : "");
    $("dash-live-account").textContent = owned ? `${liveLabel} @${live.username || ""}` : value("username") ? `Аккаунт: @${value("username").replace(/^@/, "")}` : "Укажите имя своего аккаунта в настройках";
    const connectReason = !state.connected ? "Нет связи с сервером."
      : !view ? "Загружаются настройки подключения."
      : owned ? "Сессия эфира уже создана; сначала отключите её."
      : busy || state.busy ? "Дождитесь завершения текущей операции."
      : dirty ? "Сохраните и примените изменения настроек."
      : view.config_error ? "Исправьте или сбросьте локальные настройки."
      : view.pending ? "Примените сохранённые настройки."
      : !value("username").trim() ? "Укажите и примените имя аккаунта своего эфира."
      : !/^[A-Za-z0-9._]{1,24}$/.test(value("username").trim().replace(/^@/, "")) ? "Username должен содержать 1–24 буквы, цифры, точки или подчёркивания."
      : value("provider") === "openai" ? "Для LIVE выберите и примените Fake или LM Studio."
      : view.connect_block_reason || (view.locked ? view.lock_reason || "Сервер занят." : null);
    document.querySelectorAll("[data-live-connect-reason]").forEach((element) => {
      element.textContent = connectReason ? `Подключение недоступно: ${connectReason}` : "Готово к чтению своего эфира. Готовность отправки Android не требуется.";
    });
    document.querySelectorAll('[data-live-action="connect"]').forEach((button) => {
      const connecting = owned && ["starting", "connecting"].includes(live?.state);
      button.hidden = owned && !connecting;
      button.textContent = connecting ? "Подключение…" : "Подключиться к эфиру";
      button.disabled = Boolean(connectReason);
      button.title = connectReason || "Читать комментарии своего эфира";
    });
    document.querySelectorAll('[data-live-action="disconnect"]').forEach((button) => {
      button.hidden = !owned; button.disabled = !state.connected || busy;
    });
  }
  function render(next, force = false) {
    view = next;
    if (!dirty || force) {
      const desired = next.desired;
      state.savedDevice = next.saved.android.device_serial;
      if (state.savedDevice && !Array.from($("operator-device").options).some(o => o.value === state.savedDevice)) {
        const option = document.createElement("option"); option.value = state.savedDevice; option.textContent = state.savedDevice;
        $("operator-device").append(option);
      }
      const values = {username: next.saved_username ? "@" + next.saved_username : "", provider: desired.provider,
        model: desired.model, temperature: desired.temperature, timeout: desired.timeout_seconds,
        policy: desired.reply_policy, sentences: desired.max_reply_sentences, chars: desired.max_reply_chars, device: state.savedDevice || ""};
      fields.forEach((name) => { $("operator-" + name).value = values[name]; });
      document.querySelectorAll('input[name="operator-transport"]').forEach(input => { input.checked = input.value === next.saved_transport; });
      dirty = false;
      editedRevision = next.saved_config_revision;
    }
    renderSettings(next.active);
    controls();
  }
  async function action(path, body) {
    if (busy) return;
    busy = true; $("operator-error").hidden = true; controls();
    try {
      const result = await api(path, body);
      if (result.active) render(result, true);
      renderStatus(await api("/api/admin/status"));
      await refreshQuality();
      render(await api("/api/admin/operator/settings"));
      toast(path.endsWith("connect") && !path.endsWith("disconnect") ? "Подключение запрошено. Ожидаем подключение к чату." : "Готово.");
    } catch (error) {
      if (error.status === 409) {
        render(await api("/api/admin/operator/settings"), true);
        renderStatus(await api("/api/admin/status"));
      }
      $("operator-error").textContent = error.status === 409 ? "Конфликт: состояние изменилось. Настройки перечитаны; повторно проверьте действие. " + error.message : error.message;
      $("operator-error").hidden = false; toast(error.message);
    }
    finally { busy = false; controls(); }
  }
  fields.forEach((name) => $("operator-" + name).addEventListener("input", () => { dirty = true; controls(); }));
  document.querySelectorAll('input[name="operator-transport"]').forEach(input => input.addEventListener("change", () => { dirty = true; controls(); }));
  $("operator-policy").addEventListener("change", () => {
    const friendly = value("policy") === "friendly";
    $("operator-sentences").value = friendly ? "3" : "2";
    $("operator-chars").value = friendly ? "260" : "180";
    dirty = true; controls();
  });
  $("operator-form").addEventListener("submit", (event) => {
    event.preventDefault(); if ($("operator-save").disabled) return;
    action("/api/admin/operator/settings", {expected_saved_revision: editedRevision, settings: {...view.saved, tiktok: {username: value("username"), transport: transport()},
      ai: {provider: value("provider"), model: value("model"), temperature: Number(value("temperature")), timeout_seconds: Number(value("timeout"))},
      reply: {mode: value("policy"), max_sentences: Number(value("sentences")), max_chars: Number(value("chars"))},
      android: {device_serial: value("device") || null}}});
  });
  $("operator-apply").addEventListener("click", () => action("/api/admin/operator/apply", {expected_saved_revision: view.saved_config_revision, expected_active_runtime_revision: view.active_runtime_revision}));
  $("operator-reset").addEventListener("click", () => action("/api/admin/operator/reset", {expected_saved_revision: view.saved_config_revision}));
  document.querySelectorAll("[data-live-action]").forEach((button) => button.addEventListener("click", () => {
    action("/api/admin/live/" + button.dataset.liveAction,
      button.dataset.liveAction === "connect" ? {username: value("username"), expected_active_runtime_revision: view.active_runtime_revision} : {expected_live_session_id: state.status.live.live_session_id});
  }));
  return {render, controls};
};
