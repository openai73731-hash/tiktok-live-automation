/* Explicit manual fill; separately armed backend AUTO. Identity-only requests. */
window.createDeliveryControls = ({api, node, toast, state, refreshCards, refreshReviews = async () => {}}) => {
  const $ = (id) => document.getElementById(id);
  let view = null, version = 0, busy = false;
  const keys = new Map();
  function render(next) {
    view = next;
    const d = next.inspection;
    const items = [["ADB", d.adb], ["Device", d.device], ["Serial", d.serial],
      ["Client", ({TIKTOK_YOU: "TikTok You · observed hint", OFFICIAL: "Official profile · локально reviewed", UNKNOWN: "UNKNOWN"}[d.client_variant] || "UNKNOWN")],
      ["TikTok package", d.package], ["Version", d.version], ["Version code", d.version_code],
      ["Foreground", d.foreground_activity], ["Screen", d.screen], ["Profile", d.profile_verified ? "VERIFIED" : "UNVERIFIED"],
      ["Comment input / Send", `${d.comment_input_found} / ${d.send_button_found}`],
      ["Send control", d.send_control_state],
      ["Account / LIVE verified", `${d.account_verified} / ${d.live_verified}`],
      ["Security UI", d.security_state], ["Delivery", next.delivery],
      ["Physical SEND", next.physical_delivery_enabled ? "GATE ENABLED · проверка обязательна" : "DISABLED"],
      ["Причина", next.reason === "device_unavailable" ? d.reason : next.reason]];
    $("device-status").replaceChildren(...items.map(([label, value]) => {
      const row = node("div"); row.append(node("dt", "", label), node("dd", "mono", String(value ?? "—"))); return row;
    }));
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
    $("confirm-send").disabled = busy || !state.connected || !d.send_confirmation_token;
    $("delivery-mode").value = next.mode || "MANUAL";
    $("delivery-mode").disabled = busy || !state.connected;
    $("pause-auto").disabled = !state.connected || next.mode !== "AUTOMATIC";
    $("verify-fill").disabled = busy || !state.connected || next.workflow !== "FILLED — SEND ON PHONE";
    $("delivery-mode-label").textContent = next.mode || "MANUAL";
    $("delivery-workflow").textContent = `${next.workflow || "READY"} · ${next.auto_state || "AUTO OFF"} · ${next.auto_reason || ""}`;
    refreshCards();
  }
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
    finally { busy = false; $("capture-device").disabled = $("inspect-device").disabled = !state.connected; }
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
  $("confirm-send").addEventListener("click", async () => {
    const token = view?.inspection.send_confirmation_token;
    if (busy || !token || !state.connected) return;
    if (!window.confirm("Подтверждаю, что vp8/vpl в открытом composer — кнопка отправки. Привязка действует только для проверенного Xiaomi / TikTok You 46.8.3 и аккаунта. Сейчас ничего не отправляется.")) return;
    busy = true;
    try { render(await api("/api/admin/device/confirm-send", {token, confirmed: true})); }
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
    const allowed = Boolean(approved && e?.source === "tiktok" && e.status === "ready");
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
