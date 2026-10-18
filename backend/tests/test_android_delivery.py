"""Stage 4B: synthetic devices only. No real ADB, AI, LIVE or physical action."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import xml.etree.ElementTree as ET

import pytest

from app.android.adb import AdbTransport, AdbCommand, AdbError, DUMP_PATH, parse_devices
from app.android.controller import ADBAndroidController
from app.android.models import DeviceInspection, ScreenState
from app.android.tiktok_ui import ScreenProfile, inspect_hierarchy
from app.delivery.coordinator import DeliveryCoordinator
from app.delivery.journal import SQLiteDeliveryOperationStore, JournalConflict
from app.delivery.models import (DeliveryRequest, DeliveryResult, AndroidSubmission, AndroidTarget,
    ManualDeliveryInput, ApprovalInput, SuggestionEnvelope)
from app.delivery.sender import AndroidTikTokReplySender
from app.delivery.service import ManualDeliveryService as ProductionDeliveryService
from app.delivery.suggestions import DeliveryEligibility, SuggestionConflict
from app.live.models import Comment
from app.admin.service import record_for
from app.admin.operator_config import OperatorOverlay, LocalConfigError
from test_predelivery_hardening import suggestion, panel, coordinator, execute

PACKAGE = "com.zhiliaoapp.musically"
PROFILE = ScreenProfile(PACKAGE, "fixture-1", "fixture:input", "fixture:send", "fixture:account", "fixture:live", True)
evaluate = DeliveryEligibility.evaluate


def ManualDeliveryService(settings, suggestions, context, **kwargs):
    """Inject an already armed server AUTO state for low-level delivery regression tests.

    Explicit opt-in/CSRF/default-gate behavior is covered separately in Stage4B4.
    """
    service = ProductionDeliveryService(settings, suggestions, context, **kwargs)
    if settings.enable_physical_tiktok_send:
        ctx = context()
        service.mode = "AUTOMATIC"
        service.armed_binding = (ctx.live_session_id, ctx.target_account, ctx.active_runtime_revision)
        for e in suggestions.groups["live"].values():
            suggestions.authorize_auto(e.suggestion_id, service.auto_context())
    return service


@pytest.fixture(autouse=True)
def no_real_adb(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=AssertionError("Real subprocess forbidden")))


def hierarchy(*, account="@own", live="LIVE", text="", extra=None, missing=None):
    root = ET.Element("hierarchy")
    for identity, content, klass in [(PROFILE.account_id, account, "android.widget.TextView"),
        (PROFILE.live_id, live, "android.widget.TextView"), (PROFILE.input_id, text, "android.widget.EditText"),
        (PROFILE.send_id, "", "android.widget.Button")]:
        if identity == missing: continue
        ET.SubElement(root, "node", {"package": PACKAGE, "resource-id": identity, "text": content,
            "class": klass, "enabled": "true", "clickable": "true", "content-desc": "Send" if identity == PROFILE.send_id else ""})
    if extra is not None: ET.SubElement(root, "node", extra)
    return ET.tostring(root, encoding="unicode")


class Transport:
    executable = Path("fixture/adb.exe")
    def __init__(self, devices="USB1 device", xml=None):
        self.devices, self.xml = devices, xml or hierarchy()
        self.calls, self.closed, self.after = [], False, None
    async def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.after: await self.after(command)
        return {AdbCommand.DEVICES: "List of devices attached\n" + self.devices + "\n",
            AdbCommand.PACKAGES: "package:" + PACKAGE, AdbCommand.FOREGROUND: f"mResumedActivity: ActivityRecord{{abc u0 {PACKAGE}/.LiveActivity t1}}",
            AdbCommand.DUMP: "UI hierarchy dumped to: " + DUMP_PATH,
            AdbCommand.HIERARCHY: self.xml, AdbCommand.PACKAGE_INFO: "versionName=fixture-1"}[command]
    async def aclose(self): self.closed = True


class Actions:
    def __init__(self, transport): self.transport, self.entries, self.clicks = transport, [], 0
    def supports(self, text): return True
    async def prepare_exact(self, serial, resource_id, text):
        self.entries.append((serial, resource_id, text)); self.transport.xml = hierarchy(text=text); return text
    async def click_send(self, serial, resource_id, guard):
        await asyncio.sleep(0)
        if not guard(): return False
        self.clicks += 1; return True
    async def aclose(self): pass


def device(*, enabled=True, xml=None, devices="USB1 device", profiles=(PROFILE,), actions=True):
    t = Transport(devices, xml)
    a = Actions(t) if actions else None
    c = ADBAndroidController(t, physical_enabled=enabled, profiles=profiles, actions=a)
    return c, t, a


def submission(text="Exact reply"):
    return AndroidSubmission(operation_id=uuid4().hex, target=AndroidTarget(live_session_id="a" * 32,
        account="own", device_serial="USB1"), text=text, expires_at=datetime.now(UTC) + timedelta(minutes=5))


def body(rig, key="manual_test"):
    return ManualDeliveryInput(suggestion_id=rig.e.suggestion_id, expected_suggestion_revision=rig.e.suggestion_revision,
        approval_id=rig.store.auto_authorizations.get(rig.e.suggestion_id, rig.approval).approval_id, idempotency_key=key)


def request(rig):
    e = rig.e
    return DeliveryRequest(operation_id=uuid4().hex, idempotency_key="manual_test", suggestion_id=e.suggestion_id,
        suggestion_revision=e.suggestion_revision, comment_identity=e.comment_identity, approval_id=rig.approval.approval_id,
        suggested_reply_hash=e.suggested_reply_hash, text=e.suggested_reply, live_session_id=e.live_session_id,
        target_account=e.target_account, active_runtime_revision=e.active_runtime_revision, expires_at=e.expires_at,
        device_serial="USB1")


async def test_monotonic_lifetime_never_resurrects_on_wall_clock_rollback(suggestion, monkeypatch):
    r = suggestion; deadline = r.e._monotonic_deadline
    assert evaluate(r.e, r.approval, r.context, monotonic_now=deadline - 1).eligible
    for wall in [datetime.now(UTC), datetime(2000, 1, 1, tzinfo=UTC)]:
        assert evaluate(r.e, r.approval, r.context, now=wall, monotonic_now=deadline + 1).reason == "expired"
    monkeypatch.setattr("app.delivery.suggestions.monotonic", lambda: deadline + 1)
    assert r.store.view(r.e.suggestion_id, r.context).approval_state == "stale"
    with pytest.raises(SuggestionConflict):
        r.store.approve(r.e.suggestion_id, ApprovalInput(expected_suggestion_revision=r.e.suggestion_revision), r.context)


async def test_receipt_deadline_includes_queue_and_inference_delay(suggestion):
    r = suggestion; comment = Comment(username="viewer", text="Is it automatic?")
    comment._received_monotonic -= 301
    result = await r.processor.process(comment)
    e = r.store.register(record_for(comment, result, "tiktok", 1, uuid4().hex), comment, r.processor,
        r.e.active_runtime_revision, session_id=r.e.live_session_id, target_account="own")
    assert evaluate(e, None, r.context).reason == "expired"
    assert "received_monotonic" not in comment.model_dump_json()
    assert "monotonic" not in e.model_dump_json()
    restored = SuggestionEnvelope.model_validate(r.e.model_dump())
    assert evaluate(restored, r.approval, r.context).reason == "expired"


@pytest.mark.parametrize("change", ["approval", "envelope", "expiry", "runtime", "session", "account", "connected", "policy", "knowledge"])
async def test_authoritative_change_after_reservation_never_calls_sender(suggestion, tmp_path, change):
    r = suggestion; journal = SQLiteDeliveryOperationStore(tmp_path / "op.db"); reserve = journal.reserve
    def reserve_and_revoke(req):
        reserved = reserve(req)
        if change == "approval": r.store.approvals.clear()
        elif change == "envelope": r.store.groups["live"].clear()
        elif change == "expiry": r.e._monotonic_deadline = 0
        else:
            field = {"runtime": "active_runtime_revision", "session": "live_session_id", "account": "target_account",
                "connected": "connected", "policy": "policy_revision", "knowledge": "knowledge_revision"}[change]
            r.context = r.context.model_copy(update={field: False if field == "connected" else "other"})
        return reserved
    journal.reserve = reserve_and_revoke
    sender = SimpleNamespace(send=AsyncMock())
    result = await execute(r, coordinator(r, journal, sender))
    assert result.status == "not_sent"; sender.send.assert_not_awaited()
    journal.close()


async def test_cancellation_and_finish_failure_settle_sender_before_persistence(suggestion, tmp_path):
    r = suggestion; journal = SQLiteDeliveryOperationStore(tmp_path / "op.db")
    started, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def sending(req):
        try: started.set(); await asyncio.Event().wait()
        finally: cleanup.set(); await release.wait()
    def broken(*args):
        assert release.is_set()
        raise sqlite3.OperationalError("fixture secret must not escape")
    journal.finish = broken
    c = coordinator(r, journal, SimpleNamespace(send=AsyncMock(side_effect=sending)))
    task = asyncio.create_task(c.execute(r.e.suggestion_id, r.approval.approval_id, r.e.suggestion_revision, "manual_test", timeout=1))
    await started.wait(); task.cancel(); await cleanup.wait(); task.cancel(); await asyncio.sleep(0)
    assert not task.done(); release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert not c.tasks and c.failed and journal.lookup("manual_test").status == "reserved"
    result = await c.execute(r.e.suggestion_id, r.approval.approval_id, r.e.suggestion_revision, "other")
    assert result.reason == "delivery_paused"; journal.close()


@pytest.mark.parametrize("failure", ["build", "retire", "cancel"])
async def test_apply_final_sse_snapshot_is_unlocked(panel, monkeypatch, failure):
    app, _ = panel; runtime = app.state.operator_runtime; sub = app.state.admin.events.subscribe()
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.state.quality.aclose
    async def retire():
        if failure == "retire": raise RuntimeError("fixture")
        entered.set(); await release.wait(); await original()
    if failure == "build": monkeypatch.setattr("app.admin.runtime.create_processor", lambda *a, **k: (_ for _ in ()).throw(ValueError("fixture")))
    else: app.state.quality.aclose = retire
    task = asyncio.create_task(runtime.apply(runtime.saved_config_revision, runtime.active_runtime_revision))
    if failure == "cancel":
        await entered.wait(); task.cancel(); await asyncio.sleep(0); task.cancel(); release.set()
    with pytest.raises((ValueError, RuntimeError, asyncio.CancelledError)): await task
    locks = []
    while not sub.queue.empty():
        event = sub.queue.get_nowait()
        if event.name == "operator_settings": locks.append(json.loads(event.data)["locked"])
    assert locks and locks[-1] is False and not runtime.lock.locked()
    app.state.admin.events.unsubscribe(sub)


@pytest.mark.parametrize("mode", ["fetch", "sse", "reconnect"])
async def test_quality_old_epoch_never_replaces_new(panel, mode):
    from app.quality.service import QualityService
    node = shutil.which("node")
    if not node: pytest.skip("Optional Node regression")
    app, _ = panel
    old = app.state.quality.snapshot().model_dump(mode="json"); old["run"]["revision"] = 155
    new = QualityService(app.state.admin.playground).snapshot().model_dump(mode="json"); new["run"]["revision"] = 1
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const [a,b,mode]=JSON.parse(fs.readFileSync(0,'utf8')),els=new Map();
const el=()=>({children:[],append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x},setAttribute(){},addEventListener(){}});
const document={getElementById(id){if(!els.has(id))els.set(id,el());return els.get(id)}};const window={};
vm.runInNewContext(fs.readFileSync('backend/app/static/admin/quality.js','utf8'),{window,document});
let resolve;const lab=window.createQualityLab({api:()=>new Promise(r=>resolve=r),node:(_,__,v)=>Object.assign(el(),{textContent:v}),replyCard:el,toast(){},state:{connected:true},updateControls(){}});
(async()=>{lab.apply(a);const late=lab.refresh();lab.apply(b);const rows=els.get('quality-cases').children;
resolve(a);await late;assert.strictEqual(rows,els.get('quality-cases').children);
if(mode==='sse')lab.apply(a);else {const again=lab.refresh();resolve(a);await again;}
assert.strictEqual(rows,els.get('quality-cases').children);})().catch(e=>{console.error(e);process.exitCode=1});
'''
    done = subprocess.run([node, "-e", script], input=json.dumps([old, new, mode]), text=True, capture_output=True, timeout=10)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("devices,reason", [("", "device_missing"), ("USB1 device", "ready"),
    ("USB1 device\nUSB2 device", "multiple_devices"), ("USB1 unauthorized", "unauthorized"), ("USB1 offline", "offline")])
async def test_discovery_status_and_no_interaction(devices, reason):
    c, t, a = device(devices=devices)
    result = await c.inspect(submission().target)
    assert result.reason == reason and not a.entries and a.clicks == 0
    if reason != "ready": assert len(t.calls) == 1


async def test_selected_serial_is_exact_and_missing_never_falls_back():
    c, t, _ = device(devices="USB1 device\nUSB2 device"); c.serial = "USB2"
    assert (await c.inspect(submission().target)).serial == "USB2"
    assert all(args.get("serial", "USB2") == "USB2" for _, args in t.calls)
    c.serial = "absent"; t.calls.clear()
    assert (await c.inspect()).reason == "device_missing" and len(t.calls) == 1


@pytest.mark.parametrize("line", ["bad;serial device", "USB1 unknown", "USB1 device\nUSB1 device", "-s device", "USB1", "USB1\x00 device"])
def test_device_output_rejects_ambiguous_or_unsafe_lines(line):
    with pytest.raises(AdbError): parse_devices("List of devices attached\n" + line)


@pytest.mark.parametrize("serial", ["; echo x", "-s", "a b", "a\nother", "a&b", "x" * 101])
def test_adb_argv_rejects_injection(tmp_path, serial):
    path = tmp_path / "adb.exe"; path.touch(); adb = AdbTransport(path)
    with pytest.raises(AdbError): adb.argv(AdbCommand.FOREGROUND, serial=serial)


def test_adb_fixed_allowlist_and_pinned_server(tmp_path):
    path = tmp_path / "adb.exe"; path.touch(); adb = AdbTransport(path)
    assert adb.argv(AdbCommand.DEVICES) == [str(path.resolve()), "-H", "127.0.0.1", "-P", "5037", "devices", "-l"]
    for command in AdbCommand:
        kwargs = {} if command == AdbCommand.DEVICES else {"serial": "USB1"}
        if command == AdbCommand.PACKAGE_INFO: kwargs["package"] = PACKAGE
        if command == AdbCommand.TEXT: kwargs["text"] = "Safe text."
        if command in {AdbCommand.TAB, AdbCommand.ACTIVATE}:
            with pytest.raises(AdbError): adb.argv(command, **kwargs)
            continue
        if command in {AdbCommand.TAP_SEND, AdbCommand.TAP_COMPOSER}: kwargs["point"] = (10, 20)
        args = adb.argv(command, **kwargs)
        assert all(word not in args for word in ["install", "kill-server", "KEYCODE_TAB", "KEYCODE_DPAD_CENTER"])
        if "input" in args:
            assert command in {AdbCommand.TEXT, AdbCommand.SELECT_ALL, AdbCommand.DELETE, AdbCommand.TAP_SEND, AdbCommand.TAP_COMPOSER}
    with pytest.raises(AdbError): adb.argv("shell", serial="USB1")
    with pytest.raises(AdbError): adb.argv(AdbCommand.PACKAGE_INFO, serial="USB1", package="browser.app")


class Process:
    def __init__(self, output=b"List of devices attached\n", hang=False):
        self.returncode, self.killed = None, False
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.exit = asyncio.Event()
        if not hang: self.stdout.feed_eof(); self.stderr.feed_eof(); self.returncode = 0; self.exit.set()
    def kill(self):
        self.killed = True; self.returncode = -9; self.exit.set(); self.stdout.feed_eof(); self.stderr.feed_eof()
    async def wait(self): await self.exit.wait(); return self.returncode


async def test_subprocess_argv_no_shell_and_no_secrets(tmp_path, monkeypatch):
    path = tmp_path / "adb.exe"; path.touch(); process = Process()
    spawn = AsyncMock(return_value=process); monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture secret"); monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:remote:1")
    adb = AdbTransport(path)
    assert "List of devices" in await adb.run(AdbCommand.DEVICES)
    args, kwargs = spawn.call_args
    assert args == tuple(adb.argv(AdbCommand.DEVICES)) and "shell" not in kwargs
    assert "OPENAI_API_KEY" not in kwargs["env"] and "ADB_SERVER_SOCKET" not in kwargs["env"]
    assert not adb.processes


@pytest.mark.parametrize("phase", ["spawn", "read", "timeout", "overflow", "decode"])
async def test_subprocess_cancel_timeout_and_limits_release_owned_process(tmp_path, monkeypatch, phase):
    path = tmp_path / "adb.exe"; path.touch()
    process = Process(output=b"x" * 524289 if phase == "overflow" else b"\xff" if phase == "decode" else b"", hang=phase in {"spawn", "read", "timeout"})
    entered, release = asyncio.Event(), asyncio.Event()
    async def spawn(*a, **k):
        entered.set()
        if phase == "spawn": await release.wait()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    adb = AdbTransport(path, timeout=0.02 if phase == "timeout" else 1)
    task = asyncio.create_task(adb.run(AdbCommand.DEVICES)); await entered.wait()
    if phase in {"spawn", "read"}:
        await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0); task.cancel(); release.set()
    with pytest.raises(asyncio.CancelledError if phase in {"spawn", "read"} else AdbError): await task
    assert not adb.processes
    if phase in {"spawn", "read", "timeout"}: assert process.killed


@pytest.mark.parametrize("text,state", [("CAPTCHA", ScreenState.CAPTCHA), ("Verification required", ScreenState.VERIFY),
    ("Log in", ScreenState.LOGIN_REQUIRED), ("Security check", ScreenState.SECURITY_CHALLENGE)])
async def test_security_screen_blocks_every_action(text, state):
    c, _, a = device(xml=hierarchy(extra={"text": text, "package": PACKAGE}))
    result = await c.submit(submission(), guard=lambda: True)
    assert c.last.screen == state and result.reason == "security_ui" and not a.entries and not a.clicks


@pytest.mark.parametrize("xml,reason", [("<bad", "hierarchy_unavailable"), ("<!DOCTYPE hierarchy><hierarchy/>", "hierarchy_unavailable"),
    (hierarchy(account="@other"), "account_unverified"), (hierarchy(live="FEED"), "live_unverified"),
    (hierarchy(missing=PROFILE.input_id), "input_unavailable"), (hierarchy(missing=PROFILE.send_id), "input_unavailable"),
    (hierarchy(extra={"class": "android.app.Dialog", "package": PACKAGE}), "unknown_screen"),
    (hierarchy(extra={"package": "com.foreign"}), "unknown_screen"),
    ("<hierarchy>" + "<node>" * 34 + "</node>" * 34 + "</hierarchy>", "hierarchy_unavailable")])
async def test_unknown_account_live_modal_and_missing_controls_fail_closed(xml, reason):
    c, _, a = device(xml=xml)
    assert (await c.inspect(submission().target)).reason == reason
    assert (await c.submit(submission(), guard=lambda: True)).status == "needs_operator"
    assert not a.entries and not a.clicks


async def test_rc_profile_is_explicit_and_unicode_remains_unavailable():
    c, _, a = device(profiles=())
    assert (await c.submit(submission(), guard=lambda: True)).reason == "profile_unverified"
    assert not a.entries
    c, _, _ = device(actions=False)
    assert not (await c.inspect(submission().target)).ready
    from app.android.tiktok_ui import TIKTOK_YOU_4683
    rc = ADBAndroidController()
    assert rc.profiles == (TIKTOK_YOU_4683,)
    assert rc.actions.supports("Hello!") and not rc.actions.supports("Привет")


@pytest.mark.parametrize("text", ["Hello, exact reply.", "Привет, точный ответ.", "Sí, está automatizado.",
    "Grüße, der Handel läuft automatisch.", "Sim, a negociação é automática.", "Evet, işlem otomatik çalışır."])
async def test_semantic_unicode_contract_preserves_exact_text_and_never_confirms_by_exit(text):
    c, _, a = device()
    result = await c.submit(submission(text), guard=lambda: True)
    assert a.entries == [("USB1", PROFILE.input_id, text)] and a.clicks == 1
    assert result.status == "unknown"


async def test_unsupported_unicode_never_uses_adb_input_text():
    c, t, a = device(); a.supports = lambda _: False
    assert (await c.submit(submission("Привет"), guard=lambda: True)).reason == "unicode_entry_unavailable"
    assert not a.entries and not a.clicks


async def test_missing_action_result_is_ambiguous_never_not_sent():
    c, _, a = device()
    async def ambiguous(*args): a.clicks += 1  # Lost result after possible native action.
    a.click_send = ambiguous
    assert (await c.submit(submission(), guard=lambda: True)).status == "unknown"
    assert a.clicks == 1


@pytest.mark.parametrize("phase", ["inspection", "prepare", "final_inspection", "action_wait", "readback"])
async def test_guard_after_android_await_and_exact_readback(phase):
    c, t, a = device(); valid = True; inspections = 0
    async def after(command):
        nonlocal valid, inspections
        if command == AdbCommand.HIERARCHY:
            inspections += 1
            if phase == "inspection" or phase == "final_inspection" and inspections == 2: valid = False
    t.after = after
    prepare = a.prepare_exact
    async def entry(*args):
        nonlocal valid
        value = await prepare(*args)
        if phase == "prepare": valid = False
        if phase == "readback": t.xml = hierarchy(text="different text")
        return value
    a.prepare_exact = entry
    click = a.click_send
    async def clicking(*args):
        nonlocal valid
        await asyncio.sleep(0)
        if phase == "action_wait": valid = False
        return await click(*args)
    a.click_send = clicking
    result = await c.submit(submission(), guard=lambda: valid)
    assert result.status in {"not_sent", "needs_operator"} and not a.clicks


async def test_device_exclusive_lock_serializes_inspection_and_sends():
    c, t, a = device(); entered, release = asyncio.Event(), asyncio.Event(); calls = 0
    async def after(command):
        nonlocal calls
        calls += 1
        if calls == 1: entered.set(); await release.wait()
    t.after = after
    one = asyncio.create_task(c.submit(submission(), guard=lambda: True)); await entered.wait()
    two = asyncio.create_task(c.inspect(submission().target)); await asyncio.sleep(0)
    assert calls == 1 and c.lock.locked(); release.set()
    await asyncio.gather(one, two); assert a.clicks == 1


async def test_disabled_sender_never_inspects_or_reserves(suggestion, settings):
    c, t, a = device(enabled=False)
    assert (await AndroidTikTokReplySender(c).send(request(suggestion))).reason == "physical_delivery_disabled"
    service = ManualDeliveryService(settings, suggestion.store, lambda: suggestion.context, controller=c,
        journal_factory=lambda: pytest.fail("disabled feature created DB"))
    assert (await service.send(body(suggestion))).reason == "physical_delivery_disabled"
    service.prepare(); service.view(); assert service.recent() == () and not t.calls and not a.entries
    await service.aclose()


async def test_two_browser_calls_share_one_reservation_and_device_action(suggestion, settings, tmp_path):
    c, _, a = device(); await c.inspect(submission().target)
    service = ManualDeliveryService(settings.model_copy(update={"enable_physical_tiktok_send": True}), suggestion.store,
        lambda: suggestion.context, controller=c, journal_factory=lambda: SQLiteDeliveryOperationStore(tmp_path / "op.db"))
    one, two = await asyncio.gather(service.send(body(suggestion)), service.send(body(suggestion)))
    assert one == two and one.status == "unknown" and a.clicks == 1
    assert len(service.journal.recent()) == 1; await service.aclose()


async def test_cooldown_blocks_another_attempt_but_same_key_returns_prior(suggestion, settings, tmp_path):
    c, _, a = device(); await c.inspect(submission().target)
    a.click_send = AsyncMock(return_value=False)  # Proven no action.
    service = ManualDeliveryService(settings.model_copy(update={"enable_physical_tiktok_send": True}), suggestion.store,
        lambda: suggestion.context, controller=c, journal_factory=lambda: SQLiteDeliveryOperationStore(tmp_path / "op.db"))
    first = await service.send(body(suggestion))
    assert first.status == "not_sent" and not service.paused
    assert await service.send(body(suggestion)) == first
    assert (await service.send(body(suggestion, "another"))).reason == "cooldown"
    a.click_send.assert_awaited_once(); await service.aclose()


async def test_real_store_eviction_during_android_await_prevents_semantic_send(suggestion, tmp_path):
    r = suggestion; c, t, a = device(); await c.inspect(submission().target)
    async def after(command):
        if command == AdbCommand.HIERARCHY: r.store.groups["live"].clear(); r.store.approvals.clear()
    t.after = after
    journal = SQLiteDeliveryOperationStore(tmp_path / "op.db")
    sender = AndroidTikTokReplySender(c, physical_enabled=True)
    co = coordinator(r, journal, sender); sender.preflight = co.preflight
    outcome = await co.execute(r.e.suggestion_id, r.approval.approval_id, r.e.suggestion_revision, "k", device_serial="USB1")
    assert outcome.status == "not_sent" and not a.entries and not a.clicks
    await co.aclose(); await c.aclose(); journal.close()


async def test_journal_write_failure_after_possible_action_is_unknown_and_pauses(suggestion, settings, tmp_path):
    r = suggestion; c, _, a = device(); await c.inspect(submission().target)
    journal = SQLiteDeliveryOperationStore(tmp_path / "op.db")
    journal.finish = lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("fixture secret"))
    service = ManualDeliveryService(settings.model_copy(update={"enable_physical_tiktok_send": True}), r.store,
        lambda: r.context, controller=c, journal_factory=lambda: journal)
    outcome = await service.send(body(r))
    assert outcome.status == "unknown" and outcome.reason == "journal_unavailable" and service.paused
    assert a.clicks == 1 and journal.lookup("manual_test").status == "reserved"
    assert (await service.send(body(r, "other"))).reason == "delivery_paused" and a.clicks == 1
    assert not service.tasks; await service.aclose()


async def test_restart_unresolved_operation_pauses_before_device_action(suggestion, settings, tmp_path):
    path = tmp_path / "op.db"; journal = SQLiteDeliveryOperationStore(path)
    journal.reserve(request(suggestion)); journal.close()
    c, _, a = device(); await c.inspect(submission().target)
    service = ManualDeliveryService(settings.model_copy(update={"enable_physical_tiktok_send": True}), suggestion.store,
        lambda: suggestion.context, controller=c, journal_factory=lambda: SQLiteDeliveryOperationStore(path))
    assert service.prepare().paused
    assert service.recent()[0].result == "unknown"
    result = await service.send(body(suggestion))
    assert result == DeliveryResult(status="needs_operator", reason="delivery_paused")
    assert not a.entries and not a.clicks; await service.aclose()


async def test_serialized_unknown_action_error_can_never_claim_not_sent(suggestion, settings, tmp_path):
    c, _, a = device(); await c.inspect(submission().target)
    async def possible_click(*args): a.clicks += 1; raise RuntimeError("fixture private UI payload")
    a.click_send = possible_click
    service = ManualDeliveryService(settings.model_copy(update={"enable_physical_tiktok_send": True}), suggestion.store,
        lambda: suggestion.context, controller=c, journal_factory=lambda: SQLiteDeliveryOperationStore(tmp_path / "op.db"))
    result = await service.send(body(suggestion))
    assert result.status == "unknown" and result.reason == "sender_failed" and service.paused
    assert a.clicks == 1; await service.aclose()


async def test_shutdown_repeated_cancellation_finishes_owned_android_close(suggestion, settings):
    c, t, _ = device(); started, release = asyncio.Event(), asyncio.Event()
    async def closing(): started.set(); await release.wait(); t.closed = True
    t.aclose = closing
    service = ManualDeliveryService(settings, suggestion.store, lambda: suggestion.context, controller=c)
    task = asyncio.create_task(service.aclose()); await started.wait()
    task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
    assert not task.done(); release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert t.closed and service.closed


async def test_dry_run_uses_ephemeral_reservation_and_no_interaction(suggestion, settings):
    c, _, a = device()
    service = ManualDeliveryService(settings, suggestion.store, lambda: suggestion.context, controller=c,
        journal_factory=lambda: pytest.fail("dry run touched production DB"))
    for _ in range(2):
        result = await service.dry_run(body(suggestion))
        assert result.eligible and result.device_ready and result.would_send and not result.physical_action
    assert service.journal is None and not a.entries and not a.clicks
    await service.aclose()


async def test_production_sender_uses_only_authoritative_text_and_unknown_no_retry(suggestion, settings, tmp_path):
    r = suggestion; c, _, a = device(); await c.inspect(submission().target)
    service = ManualDeliveryService(settings.model_copy(update={"enable_physical_tiktok_send": True}), r.store,
        lambda: r.context, controller=c, journal_factory=lambda: SQLiteDeliveryOperationStore(tmp_path / "op.db"))
    first = await service.send(body(r))
    assert first.status == "unknown" and service.paused and a.entries[0][2] == r.e.suggested_reply
    assert (await service.send(body(r))).status == "unknown"
    assert (await service.send(body(r, "new_key"))).reason == "delivery_paused" and a.clicks == 1
    assert len(service.recent()) == 1
    assert not {"text", "original_text"} & set(service.journal.recent()[0].model_dump())
    await service.aclose()


@pytest.mark.parametrize("failure", ["locked", "full", "corrupt"])
async def test_database_failure_before_action_never_calls_sender(suggestion, tmp_path, failure):
    path = tmp_path / "op.db"
    if failure == "corrupt":
        path.write_bytes(b"not sqlite")
        with pytest.raises(sqlite3.DatabaseError): SQLiteDeliveryOperationStore(path)
        return
    journal = SQLiteDeliveryOperationStore(path, capacity=1); r = suggestion
    blocker = None
    if failure == "locked":
        blocker = sqlite3.connect(path, isolation_level=None); blocker.execute("BEGIN IMMEDIATE")
    else:
        req = request(r).model_copy(update={"suggestion_id": uuid4().hex, "comment_identity": "b" * 64})
        journal.reserve(req)
    sender = SimpleNamespace(send=AsyncMock()); c = coordinator(r, journal, sender)
    try:
        if failure == "full":
            with pytest.raises(JournalConflict): await execute(r, c)
        else: assert (await execute(r, c)).reason == "journal_unavailable"
        sender.send.assert_not_awaited()
    finally:
        if blocker: blocker.execute("ROLLBACK"); blocker.close()
        journal.close()


async def test_shutdown_cancels_only_owned_delivery_and_persists_unknown(suggestion, settings, tmp_path):
    c, _, a = device(); await c.inspect(submission().target)
    started, closed = asyncio.Event(), asyncio.Event()
    async def click(*args):
        try: started.set(); await asyncio.Event().wait()
        finally: closed.set()
    a.click_send = click
    service = ManualDeliveryService(settings.model_copy(update={"enable_physical_tiktok_send": True}), suggestion.store,
        lambda: suggestion.context, controller=c, journal_factory=lambda: SQLiteDeliveryOperationStore(tmp_path / "op.db"))
    foreign = asyncio.create_task(asyncio.Event().wait()); task = asyncio.create_task(service.send(body(suggestion)))
    await started.wait(); await service.aclose()
    with pytest.raises(asyncio.CancelledError): await task
    assert closed.is_set() and not service.tasks and not foreign.done() and c.closed
    journal = SQLiteDeliveryOperationStore(tmp_path / "op.db")
    assert journal.lookup("manual_test").status == "unknown"; journal.close()
    foreign.cancel(); await asyncio.gather(foreign, return_exceptions=True)


@pytest.mark.parametrize("field,value", [("text", "arbitrary"), ("target_account", "other"), ("device_serial", "USB1"),
    ("physical_delivery_enabled", True), ("live_session_id", "a" * 32), ("eligibility", True)])
async def test_browser_cannot_supply_delivery_authority(panel, field, value):
    _, client = panel
    payload = dict(suggestion_id="a" * 32, expected_suggestion_revision="b" * 64,
        approval_id="c" * 32, idempotency_key="test", **{field: value})
    for route in ["send", "dry-run"]:
        assert (await client.post("/api/admin/delivery/" + route, json=payload)).status_code == 422


async def test_read_only_startup_gets_and_disabled_send_do_not_spawn_or_create_db(panel, monkeypatch):
    app, client = panel
    monkeypatch.setattr("app.delivery.service.SQLiteDeliveryOperationStore", lambda *a, **k: pytest.fail("unexpected DB"))
    for route in ["delivery/status", "delivery/history", "status", "settings"]:
        assert (await client.get("/api/admin/" + route)).status_code == 200
    payload = dict(suggestion_id="a" * 32, expected_suggestion_revision="b" * 64, approval_id="c" * 32, idempotency_key="test")
    assert (await client.post("/api/admin/delivery/send", json=payload)).json()["reason"] == "physical_delivery_disabled"
    assert app.state.delivery.journal is None and app.state.operator_runtime.live.service is None
    asyncio.create_subprocess_exec.assert_not_awaited()


def test_delivery_browser_identity_only_dry_run_and_disabled_send():
    node = shutil.which("node")
    if not node: pytest.skip("Optional Node regression")
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),crypto=require('crypto');
const els=new Map();
const el=()=>({children:[],listeners:{},value:'',append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x},addEventListener(k,f){this.listeners[k]=f}});
const document={getElementById(id){if(!els.has(id))els.set(id,el());return els.get(id)}};const window={};
vm.runInNewContext(fs.readFileSync('backend/app/static/admin/delivery.js','utf8'),{window,document,crypto});
const calls=[],status={inspection:{adb:'AVAILABLE',device:'CONNECTED',devices:[{serial:'USB1',state:'CONNECTED'}]},physical_delivery_enabled:false,physical_ready:false,delivery:'NOT READY',reason:'physical_delivery_disabled'};
const api=async(path,body)=>{calls.push([path,body]);return path.endsWith('/status')?status:path.endsWith('/history')?[]:{eligible:true,device_ready:false,would_send:false,reason:'profile_unverified'}};
const delivery=window.createDeliveryControls({api,node:(_,__,s)=>Object.assign(el(),{textContent:s}),toast(){},state:{connected:true,savedDevice:'USB1'},refreshCards(){}});
(async()=>{document.getElementById('operator-device').value='USB1';await delivery.refresh();document.getElementById('operator-device').value='';await delivery.refresh();assert.strictEqual(document.getElementById('operator-device').value,'');
const actions=el(),send=el(),record={delivery_review:{approval_state:'approved',approval:{approval_id:'a'.repeat(32)},envelope:{suggestion_id:'b'.repeat(32),suggestion_revision:'c'.repeat(64),source:'tiktok',status:'ready',suggested_reply:'PRIVATE EXACT TEXT'}}};
delivery.attach(actions,record,send);assert.strictEqual(send.disabled,true);assert.strictEqual(send.hidden,true);assert(!send.listeners.click);assert(!calls.some(([p,b])=>p.endsWith('/send')));
const dry=actions.children.find(e=>e.textContent==='Проверить без отправки');assert.strictEqual(dry.disabled,false);await dry.listeners.click();
const posted=calls.find(([p,b])=>p.endsWith('/dry-run'))[1];assert.deepStrictEqual(Object.keys(posted).sort(),['approval_id','expected_suggestion_revision','idempotency_key','suggestion_id']);assert(!JSON.stringify(posted).includes('PRIVATE EXACT TEXT'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("headers,status", [({"X-Admin-CSRF": "wrong"}, 403), ({"Origin": "https://remote.example"}, 403),
    ({"Host": "remote.example"}, 403), ({"Content-Type": "text/plain"}, 415)])
async def test_device_and_delivery_mutations_preserve_local_guards(panel, headers, status):
    _, client = panel
    for route in ["device/inspect", "delivery/prepare"]:
        assert (await client.post("/api/admin/" + route, json={}, headers=headers)).status_code == status


async def test_serial_setting_requires_backend_discovery(panel):
    app, client = panel; rt = app.state.operator_runtime
    async def save(serial):
        return await client.post("/api/admin/operator/settings", json={"expected_saved_revision": rt.saved_config_revision,
            "settings": {"android": {"device_serial": serial}}})
    assert (await save("unseen")).status_code == 409
    c, _, _ = device(); app.state.delivery.controller = c
    await c.inspect(submission().target)
    assert (await save("USB1")).status_code == 200
    assert rt.android_serial is None
    await rt.apply(rt.saved_config_revision, rt.active_runtime_revision)
    assert rt.android_serial == "USB1" and c.serial == "USB1" and c.last.reason == "not_checked"


@pytest.mark.parametrize("position", ["file", "parent"])
@pytest.mark.parametrize("kind", ["reparse", "symlink"])
def test_local_overlay_reparse_guard_rejects_file_and_parent(tmp_path, monkeypatch, settings, position, kind):
    # Windows reparse attributes are checked without requiring symlink privileges.
    from app.admin.operator_config import OperatorStore
    from app.ai.knowledge import ProjectConfig, load_yaml_model
    store = OperatorStore(load_yaml_model(settings.project_config_path, ProjectConfig), tmp_path / "operator.local.yaml")
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        if path == (store.path if position == "file" else tmp_path):
            return SimpleNamespace(st_mode=stat.S_IFLNK if kind == "symlink" else 0,
                st_file_attributes=0x400 if kind == "reparse" else 0)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", lstat)
    for action in [store.load, lambda: store.save(OperatorOverlay()), store.reset]:
        with pytest.raises(LocalConfigError): action()
