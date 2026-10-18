/* Explicit manual fill; separately armed backend AUTO. Identity-only requests. */
window.createDeliveryControls = ({api, node, toast, state, refreshCards, refreshReviews = async () => {}}) => {
  const $ = (id) => document.getElementById(id);
  let view = null, version = 0, busy = false, challengeTimer = null;
  const keys = new Map();
  function render(next) {
    ++version; // Invalidate older GETs when SSE publishes a transport switch.
    view = next;
    if (challengeTimer !== null) { window.clearTimeout(challengeTimer); challengeTimer = null; }
    if (next.transport !== "windows_live_studio" && next.account_challenge?.token && window.setTimeout) {
      challengeTimer = window.setTimeout(() => { challengeTimer = null; refresh().catch((error) => toast(error.message)); },
        (next.account_challenge.expires_in_seconds + 1) * 1000);
    }
    const windows = next.transport === "windows_live_studio";
    $("active-transport").textContent = windows ? "PC / LIVE STUDIO" : "MOBILE / ANDROID";
    $("stream-mode").textContent = windows ? "PC" : "MOBILE";
    const stopped = next.paused || next.delivery_uncertain || next.workflow === "UNKNOWN" || next.inspection?.security_state;
    $("delivery-summary").textContent = stopped ? "STOP" : next.physical_ready || (!windows && next.fill_ready) ? "READY" : "NEED OPERATOR";
    if (!windows && next.paused) $("delivery-summary").textContent = next.unknown_ack_count ? "DELIVERY PAUSED — ACK UNKNOWN REQUIRED" : "DELIVERY PAUSED — RE-PREPARE REQUIRED";
    $("auto-summary").textContent = (windows ? next.windows_sender?.auto_enabled : next.mode === "AUTOMATIC") ? "ON" : "OFF";
    $("device-panel-title").textContent = windows ? "Windows LIVE Studio" : "Android / TikTok UI";
    $("device-panel-note").textContent = windows
      ? "PC-SEND-2B · оператор сам фокусирует пустой composer. AUTO принимает только новые receipts и approvals после включения epoch."
      : "Явная проверка и capture не запускают LIVE/AI, не вводят текст и не отправляют сообщения.";
    $("android-settings").hidden = windows;
    $("windows-panel").hidden = !windows;
    $("manual-mode-panel").hidden = windows;
    $("legacy-delivery-settings").hidden = windows;
    $("dash-delivery-mode").textContent = windows ? next.windows_sender?.auto_enabled ? "AUTO · operator approved" : "MANUAL" : next.mode;
    $("android-capture-note").hidden = windows;
    ["inspect-device", "capture-device", "verify-account", "confirm-send", "prepare-mobile", "live-account-challenge", "mobile-challenge-note"].forEach((id) => {
      $(id).hidden = windows;
      $(id).disabled = windows || busy || !state.connected;
    });
    const d = next.inspection, w = next.windows_sender || {};
    const items = windows ? [["Transport", "windows_live_studio"], ["Instance", next.instance_id],
      ["Account label", next.account_label],
      ["Stage", w.stage], ["State", w.state], ["Target PID", w.target_process_id],
      ["Target instance", w.target_instance],
      ["Armed age (s)", w.armed_age_seconds], ["Foreground guard", w.foreground_guard ? "OK" : "FAIL"],
      ["Composer", w.composer], ["Last send", w.last_send_status], ["Verification", w.clock_verification === "unstable" ? "UNAVAILABLE" : w.verification],
      ["Clock verification", w.clock_verification], ["AUTO epoch state", w.auto_epoch_state],
      ["AUTO epoch", w.auto_epoch_id], ["Approval watermark", w.auto_watermark], ["One-shot acceptance", w.acceptance_status],
      ["Physical Enter gate", next.physical_delivery_enabled ? "ON" : "OFF"], ["Reason", w.reason],
      ["Delivery", next.delivery]] : [["ADB", d.adb], ["Device", d.device], ["Serial", d.serial],
      ["Client", ({TIKTOK_YOU: "TikTok You · observed hint", OFFICIAL: "Official profile · локально reviewed", UNKNOWN: "UNKNOWN"}[d.client_variant] || "UNKNOWN")],
      ["TikTok package", d.package], ["Version", d.version], ["Version code", d.version_code],
      ["Foreground", d.foreground_activity], ["Screen", d.screen], ["Profile", d.profile_verified ? "VERIFIED" : "UNVERIFIED"],
      ["Comment input / Send", `${d.comment_input_found} / ${d.send_button_found}`],
      ["Send control", d.send_control_state],
      ["Account / LIVE verified", `${d.account_verified} / ${d.live_verified}`],
      ["Device/install prepared", Boolean(next.mobile_prepared)],
      ["LIVE account challenge", next.account_challenge?.state || "REQUIRED"],
      ["Challenge rejected checks", next.account_challenge?.last_checks
        ? Object.entries(next.account_challenge.last_checks).filter(([,pass]) => !pass).map(([name]) => name).join(", ") || "none" : "—"],
      ["Security UI", d.security_state], ["Delivery", next.delivery],
      ["Physical SEND", next.physical_delivery_enabled ? "GATE ENABLED · проверка обязательна" : "DISABLED"],
      ["Причина", next.reason === "device_unavailable" ? d.reason : next.reason]];
    $("device-status").replaceChildren(...items.map(([label, value]) => {
      const row = node("div"); row.append(node("dt", "", label), node("dd", "mono", String(value ?? "—"))); return row;
    }));
    if (windows) {
      $("windows-unknown").hidden = !next.delivery_uncertain;
      $("windows-instruction").textContent = w.state === "ARMING"
        ? w.watch_ready ? `ARM READY — CLICK COMPOSER NOW · ${w.watch_seconds} seconds · ensure it is empty.` : "WAITING FOR COMPOSER FOCUS · preparing watch…"
        : w.state === "ARMED" ? "COMPOSER ARMED · focus must remain in Studio."
        : "WAITING FOR COMPOSER FOCUS · confirm empty, ARM, then click the Studio composer.";
      $("windows-instruction").className = "focus-instruction";
      $("windows-arm").disabled = busy || next.busy || !state.connected;
      $("windows-disarm").disabled = !state.connected;
      $("windows-accept-empty").disabled = !state.connected || w.last_send_status !== "SENT_CONFIRMED" || w.clock_verification === "unstable";
      $("windows-auto").checked = Boolean(w.auto_enabled);
      $("windows-auto").disabled = !state.connected || !next.physical_delivery_enabled || !w.empty_after_verified_confirmed || w.acceptance_status === "SPENT";
      refreshCards(); return;
    }
    const select = $("operator-device"), selected = select.value;
    const wanted = selected;
    const devices = d.devices.map((device) => {
      const option = node("option", "", `${device.serial} · ${device.state}`); option.value = device.serial; return option;
    });
    // Retain an existing saved choice for display; the backend validates new choices.
    if (wanted && !d.devices.some((device) => device.serial === wanted)) {
      const missing = node("option", "", `${wanted} · MISSING`); missing.value = wanted; devices.push(missing);
    }
    const automatic = node("option", "", "Единственное устройство (если подключено одно)"); automatic.value = "";
    select.replaceChildren(automatic, ...devices); select.value = wanted;
    $("inspect-device").disabled = busy || !state.connected;
    $("capture-device").disabled = busy || !state.connected;
    $("verify-account").disabled = busy || !state.connected;
    $("live-account-challenge").disabled = busy || next.busy || !state.connected || !next.account_challenge?.available;
    const challenge = next.account_challenge;
    $("mobile-challenge-note").textContent = challenge?.token
      ? `TYPE THIS EXACT COMMENT FROM THE HOST PHONE: ${challenge.token} · expires in ${challenge.expires_in_seconds}s. Введите и отправьте вручную с host телефона.`
      : challenge?.state === "VERIFIED" ? "LIVE ACCOUNT VERIFIED · новая внешняя test comment → AI → APPROVE → host LIVE inspect → FILL PHONE → human Send → VERIFY."
      : challenge?.state === "CHECKING" ? "Challenge получен. Свежая read-only проверка host LIVE/device/install…"
      : "PREPARE MOBILE проверяет только device/install. Затем вручную START LIVE → CONNECT → LIVE ACCOUNT CHALLENGE. Profile proof остаётся отдельной строгой диагностикой.";
    $("prepare-mobile").disabled = busy || next.busy || !state.connected || next.mode !== "MANUAL" || Boolean(next.live_session_id);
    $("confirm-send").disabled = busy || !state.connected || !d.send_confirmation_token;
    $("delivery-mode").value = next.mode || "MANUAL";
    $("delivery-mode").disabled = busy || !state.connected;
    $("pause-auto").disabled = !state.connected || next.mode !== "AUTOMATIC";
    $("verify-fill").disabled = busy || !state.connected || next.workflow !== "FILLED — SEND ON PHONE";
    const ack = $("acknowledge-unknown");
    if (ack) {
      ack.hidden = windows || !next.unknown_ack_count;
      ack.disabled = busy || next.busy || !state.connected || next.mode !== "MANUAL" || !next.live_session_id || !next.unknown_ack_revision;
    }
    $("delivery-mode-label").textContent = next.mode || "MANUAL";
    $("delivery-workflow").textContent = `${next.workflow || "READY"} · ${next.auto_state || "AUTO OFF"} · ${next.auto_reason || ""}`;
    refreshCards();
  }
  async function windowsAction(path, payload) {
    ++version;
    try { render(await api(`/api/admin/windows/${path}`, payload)); await refresh(); }
    catch (error) { toast(error.message); }
  }
  $("windows-arm").addEventListener("click", () => {
    if (window.confirm("Ensure the comment field is empty before arming. Подтверждаю пустое поле; после READY вручную кликну composer.")) windowsAction("arm", {empty_confirmed: true});
  });
  $("windows-disarm").addEventListener("click", () => windowsAction("disarm", {}));
  $("windows-accept-empty").addEventListener("click", () => {
    if (window.confirm("После VERIFIED отправки поле действительно очистилось. Подтверждаю это для текущего Studio/LIVE. Новый ARM остаётся ручным.")) windowsAction("accept-empty", {empty_confirmed: true});
  });
  $("windows-auto").addEventListener("change", () => windowsAction("auto", {enabled: $("windows-auto").checked}));
  async function refresh() {
    const token = ++version;
    const [next, history] = await Promise.all([api("/api/admin/delivery/status"), api("/api/admin/delivery/history")]);
    if (token !== version) return;
    await refreshReviews();
    if (token !== version) return;
    render(next);
    $("delivery-history").replaceChildren(...history.map((entry) => {
      const row = node("tr");
      [entry.time, entry.username, entry.suggestion_id, entry.result, entry.reason, entry.device, entry.mode, entry.operation_id]
        .forEach((value) => row.append(node("td", "mono cell-text", String(value ?? "—"))));
      return row;
    }));
    $("delivery-history-empty").hidden = history.length > 0;
  }
  $("inspect-device").addEventListener("click", async () => {
    if (busy || !state.connected) return;
    busy = true; $("capture-device").disabled = $("inspect-device").disabled = true;
    try { render(await api("/api/admin/device/inspect", {})); await refresh(); }
    catch (error) { toast(error.message); }
    finally { busy = false; $("capture-device").disabled = $("inspect-device").disabled = !state.connected; if (view) render(view); }
  });
  $("capture-device").addEventListener("click", async () => {
    if (busy || !state.connected) return;
    busy = true; $("capture-device").disabled = $("inspect-device").disabled = true;
    try {
      const result = await api("/api/admin/device/capture", {});
      $("capture-result").textContent = `Сохранено локально: diagnostics/android/${result.capture_id}/ (hierarchy.xml, metadata.json). Ничего не отправлено.`;
      toast("UI capture сохранён только локально. Проверьте файл перед передачей.");
      await refresh();
    } catch (error) { toast(error.message); }
    finally { busy = false; $("capture-device").disabled = $("inspect-device").disabled = !state.connected; }
  });
  $("verify-account").addEventListener("click", async () => {
    if (busy || !state.connected) return;
    busy = true;
    try { render(await api("/api/admin/device/verify-account", {})); toast("Аккаунт проверен. Откройте host LIVE и проверьте устройство, затем откройте composer вручную."); }
    catch (error) { toast(error.message); }
    finally { busy = false; await refresh(); }
  });
  $("prepare-mobile").addEventListener("click", async () => {
    if (busy || !state.connected || view?.transport === "windows_live_studio") return;
    busy = true;
    try {
      render(await api("/api/admin/delivery/prepare", {}));
      toast("Device/install prepared. Аккаунт ещё не подтверждён: START LIVE → CONNECT → LIVE ACCOUNT CHALLENGE. AUTO OFF.");
    } catch (error) { toast(error.message); }
    finally { busy = false; await refresh(); }
  });
  $("live-account-challenge").addEventListener("click", async () => {
    if (busy || !state.connected || view?.transport === "windows_live_studio") return;
    busy = true;
    try { render(await api("/api/admin/device/live-account-challenge", {})); }
    catch (error) { toast(error.message); }
    finally { busy = false; await refresh(); }
  });
  $("confirm-send").addEventListener("click", async () => {
    const token = view?.inspection?.send_confirmation_token;
    if (busy || !token || !state.connected) return;
    if (!window.confirm("Подтверждаю, что vp8/vpl в открытом composer — кнопка отправки. Привязка действует только для проверенного Xiaomi / TikTok You 46.8.3 и аккаунта. Сейчас ничего не отправляется.")) return;
    busy = true;
    try { render(await api("/api/admin/device/confirm-send", {token, confirmed: true})); }
    catch (error) { toast(error.message); }
    finally { busy = false; await refresh(); }
  });
  $("acknowledge-unknown")?.addEventListener("click", async () => {
    if (busy || !state.connected || !view?.unknown_ack_revision || view.transport === "windows_live_studio") return;
    if (!window.confirm("ACK UNKNOWN: прежние операции навсегда остаются UNKNOWN и consumed. Разрешаются только новые SDK message_id после этой границы; AUTO остаётся OFF.")) return;
    busy = true;
    try { render(await api("/api/admin/delivery/acknowledge-unknown", {confirmed: true,
      expected_unknown_revision: view.unknown_ack_revision, expected_live_session_id: view.live_session_id,
      expected_active_runtime_revision: view.active_runtime_revision})); }
    catch (error) { toast(error.message); }
    finally { busy = false; await refresh(); }
  });
  async function setMode(mode) {
    if (!view || !state.connected) return;
    if (mode === "AUTOMATIC" && !window.confirm("Включить AUTOMATIC только для текущей собственной LIVE-сессии? Безопасные ответы могут отправляться без отдельного APPROVE. Одна попытка; UNKNOWN ставит AUTO на паузу.")) { render(view); return; }
    try {
      render(await api("/api/admin/delivery/mode", {mode, expected_live_session_id: view.live_session_id,
        expected_active_runtime_revision: view.active_runtime_revision}));
      await refresh();
    } catch (error) { toast(error.message); render(view); }
  }
  $("delivery-mode").addEventListener("change", () => setMode($("delivery-mode").value));
  $("pause-auto").addEventListener("click", () => setMode("MANUAL"));
  $("verify-fill").addEventListener("click", async () => {
    if (busy || !state.connected) return;
    busy = true;
    try {
      const result = await api("/api/admin/delivery/verify-fill", {});
      toast(result.status === "confirmed" ? "SENT CONFIRMED: точная строка появилась в LIVE." : "UNKNOWN. Проверьте TikTok вручную. Повтор запрещён.");
    } catch (error) { toast(error.message); }
    finally { busy = false; await refresh(); }
  });
  function attach(actions, record, send) {
    send.hidden = true; send.disabled = true;
    const review = record.delivery_review, e = review?.envelope;
    const approved = review?.approval_state === "approved" && review.approval;
    const live = e?.source === "tiktok";
    const allowed = Boolean(approved && live && e.status === "ready" && !review?.consumed);
    if (view?.transport === "windows_live_studio") {
      const auto = review?.auto_delivery;
      if (review?.consumed) actions.append(node("span", "badge danger", "CONSUMED / NO RETRY"));
      actions.append(node("span", "badge neutral", `Server AUTO: ${auto?.eligible ? "ELIGIBLE" : "BLOCKED"} · ${auto?.reason || "unavailable"}`));
      send.hidden = false;
      send.textContent = "Fill approved reply";
      send.disabled = !allowed || !review?.text_entry_supported || view.busy || busy || !state.connected || Date.now() >= Date.parse(e?.expires_at);
      const once = node("button", "button secondary-button", "Guarded send once");
      once.disabled = send.disabled || !view.physical_delivery_enabled;
      actions.append(once);
      if (e && !keys.has(e.suggestion_id)) {
        if (keys.size >= 150) keys.delete(keys.keys().next().value);
        keys.set(e.suggestion_id, crypto.randomUUID().replaceAll("-", ""));
      }
      const begin = async (path, control) => {
        if (control.disabled || !window.confirm("Ensure the comment field is empty before arming. Подтверждаю пустое поле и вручную кликну composer после Focus watch READY.")) return;
        send.disabled = once.disabled = true;
        await windowsAction(path, {empty_confirmed: true, delivery: {
          suggestion_id: e.suggestion_id, expected_suggestion_revision: e.suggestion_revision,
          approval_id: review.approval.approval_id, idempotency_key: keys.get(e.suggestion_id)}});
      };
      send.addEventListener("click", () => begin("fill", send));
      once.addEventListener("click", () => begin("send-once", once));
      actions.append(node("span", "subtle", "Ручной ARM + клик для каждого действия из Admin. Fill расходует dedup; Enter после Fill нажимает оператор."));
      return;
    }
    const fill = node("button", "button secondary-button", "ВСТАВИТЬ В ТЕЛЕФОН · FILL PHONE");
    fill.type = "button"; fill.hidden = view?.mode === "AUTOMATIC";
    fill.disabled = !allowed || !review?.fill_allowed || busy || view?.busy || !state.connected || Date.now() >= Date.parse(e?.expires_at);
    fill.title = !review?.text_entry_supported ? "unicode_entry_unavailable · доступен COPY" : "Откройте и сфокусируйте поле комментария на телефоне. Отправляете вы.";
    if (!fill.disabled) setTimeout(() => { fill.disabled = true; }, Math.max(0, Math.min(Date.parse(e.expires_at) - Date.now(), 90000)));
    const dry = node("button", "button secondary-button", "DRY RUN SEND");
    dry.type = "button"; dry.disabled = !allowed || busy || !state.connected;
    actions.append(fill, dry);
    actions.append(node("span", "subtle", `AUTO: ${review?.auto_eligibility?.eligible ? "ELIGIBLE" : "BLOCKED"} · ${review?.auto_eligibility?.reason || "automatic_mode_required"}`));
    if (!keys.has(e?.suggestion_id) && e) {
      if (keys.size >= 150) keys.delete(keys.keys().next().value);
      keys.set(e.suggestion_id, crypto.randomUUID().replaceAll("-", ""));
    }
    const perform = async (input) => {
      if ((input ? fill : dry).disabled) return;
      busy = true; dry.disabled = fill.disabled = true;
      try {
        const result = await api(`/api/admin/delivery/${input ? "fill" : "dry-run"}`, {
          suggestion_id: e.suggestion_id, expected_suggestion_revision: e.suggestion_revision,
          approval_id: review.approval.approval_id, idempotency_key: keys.get(e.suggestion_id)});
        const messages = {filled_waiting_user_send: "Ответ введён в телефон. Нажмите отправить на телефоне.",
          open_phone_composer: "Откройте поле комментария на телефоне и повторите FILL PHONE.",
          unicode_entry_unavailable: "Unicode-ввод недоступен. Используйте COPY REPLY.",
          transient_overlay: "Дождитесь обычного LIVE на телефоне. Overlay закрывать автоматически нельзя."};
        toast(result.status === "unknown" ? "UNKNOWN. Проверьте TikTok вручную. Повтор запрещён."
          : input ? messages[result.reason] || `${result.status}: ${result.reason}`
          : `DRY RUN: eligible=${result.eligible}, device_ready=${result.device_ready}, would_send=${result.would_send}; ${result.reason}. Ничего не отправлено.`);
      } catch (error) { toast(error.message); }
      finally { busy = false; await refresh(); }
    };
    fill.addEventListener("click", () => perform(true));
    dry.addEventListener("click", () => perform(false));
  }
  return {refresh, attach, render};
};
