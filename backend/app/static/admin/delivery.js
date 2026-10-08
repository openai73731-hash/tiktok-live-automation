/* Explicit device inspection and manual identity-only delivery requests. */
window.createDeliveryControls = ({api, node, toast, state, refreshCards, refreshReviews = async () => {}}) => {
  const $ = (id) => document.getElementById(id);
  let view = null, version = 0, busy = false;
  const keys = new Map();
  function render(next) {
    view = next;
    const windows = next.transport === "windows_live_studio";
    $("device-panel-title").textContent = windows ? "Windows LIVE Studio" : "Android / TikTok UI";
    $("device-panel-note").textContent = windows
      ? "Discovery: запустите scripts/probe_live_studio.py локально. Подготовка текста и отправка пока недоступны."
      : "Явная проверка и capture не запускают LIVE/AI, не вводят текст и не отправляют сообщения.";
    $("android-settings").hidden = windows;
    $("android-capture-note").hidden = windows;
    ["inspect-device", "capture-device", "verify-account", "confirm-send"].forEach((id) => {
      $(id).hidden = windows;
      $(id).disabled = windows || busy || !state.connected;
    });
    const d = next.inspection;
    const items = windows ? [["Transport", "windows_live_studio"], ["Instance", next.instance_id],
      ["Account label", next.account_label],
      ["Stage", "PC-SEND-1"], ["Discovery", next.windows_status], ["Prepare / Submit", "DISABLED"],
      ["Delivery", next.delivery]] : [["ADB", d.adb], ["Device", d.device], ["Serial", d.serial],
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
    if (windows) { refreshCards(); return; }
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
    const token = view?.inspection?.send_confirmation_token;
    if (busy || !token || !state.connected) return;
    if (!window.confirm("Подтверждаю, что vp8/vpl в открытом composer — кнопка отправки. Привязка действует только для проверенного Xiaomi / TikTok You 46.8.3 и аккаунта. Сейчас ничего не отправляется.")) return;
    busy = true;
    try { render(await api("/api/admin/device/confirm-send", {token, confirmed: true})); }
    catch (error) { toast(error.message); }
    finally { busy = false; await refresh(); }
  });
  function attach(actions, record, send) {
    const review = record.delivery_review, e = review?.envelope;
    const approved = review?.approval_state === "approved" && review.approval;
    const live = e?.source === "tiktok";
    const allowed = Boolean(approved && live && e.status === "ready");
    send.textContent = "SEND";
    send.disabled = !allowed || !review?.physical_send_allowed || !view?.physical_ready || view?.busy || busy || !state.connected || Date.now() >= Date.parse(e?.expires_at);
    if (!send.disabled) setTimeout(() => { send.disabled = true; send.title = "Истёк срок предложения. Обновите проверку."; }, Math.max(0, Math.min(Date.parse(e.expires_at) - Date.now(), 90000)));
    send.title = !allowed ? "Нужен актуальный approval реального LIVE" : !review?.text_entry_supported ? "unicode_entry_unavailable · доступен COPY" : !review?.eligibility?.eligible ? review?.eligibility?.reason || "Обновите проверку" : view?.reason || "Устройство не проверено";
    const dry = node("button", "button secondary-button", "DRY RUN SEND");
    dry.type = "button"; dry.disabled = !allowed || busy || !state.connected || view?.transport === "windows_live_studio";
    actions.append(dry);
    if (!keys.has(e?.suggestion_id) && e) {
      if (keys.size >= 150) keys.delete(keys.keys().next().value);
      keys.set(e.suggestion_id, crypto.randomUUID().replaceAll("-", ""));
    }
    const perform = async (physical) => {
      if ((physical ? send : dry).disabled) return;
      busy = true; dry.disabled = send.disabled = true;
      try {
        const result = await api(`/api/admin/delivery/${physical ? "send" : "dry-run"}`, {
          suggestion_id: e.suggestion_id, expected_suggestion_revision: e.suggestion_revision,
          approval_id: review.approval.approval_id, idempotency_key: keys.get(e.suggestion_id)});
        toast(result.status === "unknown" ? "DELIVERY RESULT UNKNOWN. Проверьте TikTok вручную. Повтор запрещён."
          : physical ? `${result.status}: ${result.reason}` : `DRY RUN: eligible=${result.eligible}, device_ready=${result.device_ready}, would_send=${result.would_send}; ${result.reason}. Ничего не отправлено.`);
      } catch (error) { toast(error.message); }
      finally {
        busy = false;
        try {
          record.delivery_review = await api(`/api/admin/suggestions/${e.suggestion_id}`);
          await refresh();
        }
        catch (error) { view = null; refreshCards(); toast(error.message); }
      }
    };
    dry.addEventListener("click", () => perform(false)); send.addEventListener("click", () => perform(true));
    actions.append(node("span", "subtle", send.disabled ? `SEND заблокирован: ${send.title}` : "MANUAL · один запрос"));
  }
  return {refresh, attach};
};
