"""Unified transport authority, restart dedup and UI; every physical API is fake."""
import asyncio
import ctypes
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.admin.operator_config import OperatorOverlay
from app.admin.service import record_for
from app.delivery.coordinator import DeliveryCoordinator
from app.delivery.factory import create_delivery_service
from app.delivery.journal import JournalConflict, SQLiteDeliveryOperationStore
from app.delivery.models import ApprovalInput, DeliveryRequest, DeliveryResult, ManualDeliveryInput
from app.delivery.registry import UnifiedDeliveryOperationStore, consumed
from app.delivery.suggestions import SuggestionConflict, SuggestionStore
from app.live.models import Comment
from app.main import create_app
from app.windows.native_input import NativeInput
from test_predelivery_hardening import suggestion, MockLive
from test_windows_sender import configuration, FakeNative, arm
from test_guarded_delivery import phone_factory, arm as arm_phone, confirm_after_tap
from test_guarded_delivery import capture, posted
from test_manual_send_rc import Transport, xml, ACCOUNT
from app.android.controller import ADBAndroidController
from app.android.adb import AdbCommand


@pytest.fixture(autouse=True)
def no_physical(monkeypatch):
    def forbidden(*args, **kwargs): pytest.fail("Real physical API forbidden")
    monkeypatch.setattr(ctypes, "WinDLL", forbidden)
    monkeypatch.setattr(NativeInput, "_load", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=AssertionError("Real ADB forbidden")))


@pytest.fixture
async def unified(settings, tmp_path, monkeypatch):
    cfg = settings.model_copy(update={"tiktok_username": "own", "state_dir": tmp_path / "state",
        "log_dir": tmp_path / "logs", "windows": configuration().windows,
        "enable_physical_tiktok_send": True, "enable_physical_windows_send": True})
    monkeypatch.setattr("app.main.LiveService", MockLive)
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as c:
            c.headers["X-Admin-CSRF"] = app.state.admin.csrf_token
            yield app, c


async def switch(app, transport):
    r = app.state.operator_runtime
    await r.save(OperatorOverlay(tiktok={"username": "own", "transport": transport}), r.saved_config_revision)
    return await r.apply(r.saved_config_revision, r.active_runtime_revision)


async def live_record(app, message_id="123"):
    r = app.state.operator_runtime
    await r.live.connect("own", expected_active_runtime_revision=r.active_runtime_revision)
    await r.live.service.started.wait()
    return await r.live.service.on_comment(Comment(username="viewer", text="Is it automatic?", message_id=message_id))


def request(e, approval, key="fixture", mode="MANUAL"):
    return DeliveryRequest(operation_id=uuid4().hex, idempotency_key=key, mode=mode,
        suggestion_id=e.suggestion_id, suggestion_revision=e.suggestion_revision,
        comment_identity=e.comment_identity, durable_delivery_key=e.durable_delivery_key,
        source_kind=e.source, source_message_id=e._source_message_id, approval_id=approval.approval_id,
        suggested_reply_hash=e.suggested_reply_hash, text=e.suggested_reply,
        live_session_id=e.live_session_id, target_account=e.target_account,
        active_runtime_revision=e.active_runtime_revision, expires_at=e.expires_at)


@pytest.mark.parametrize("transport", ["android", "windows_live_studio"])
def test_factory_constructs_only_selected_adapter(settings, monkeypatch, transport):
    other = "app.windows.service.WindowsManualDeliveryService.__init__" if transport == "android" else "app.delivery.service.ManualDeliveryService.__init__"
    monkeypatch.setattr(other, lambda *a, **k: pytest.fail("Inactive adapter created"))
    service = create_delivery_service(settings.model_copy(update={"tiktok_transport": transport}), SuggestionStore(), lambda: None)
    assert service.settings.tiktok_transport == transport and service.journal is None


@pytest.mark.parametrize("value", ["", "windows", "auto", "ANDROID", None])
def test_invalid_transport_fails_closed(settings, value):
    with pytest.raises(ValidationError): type(settings)(_env_file=None, tiktok_transport=value)
    with pytest.raises(ValueError): create_delivery_service(settings.model_copy(update={"tiktok_transport": value}), None, None)


async def test_save_apply_stale_revision_and_restart(unified):
    app, c = unified
    r, old = app.state.operator_runtime, app.state.delivery
    before = r.view()
    saved = await r.save(OperatorOverlay(tiktok={"transport": "windows_live_studio"}), before.saved_config_revision)
    assert saved.saved_transport == "windows_live_studio" and saved.active_transport == "android"
    assert app.state.delivery is old and saved.pending
    stale = await c.post("/api/admin/operator/apply", json={"expected_saved_revision": before.saved_config_revision,
        "expected_active_runtime_revision": before.active_runtime_revision})
    assert stale.status_code == 409 and app.state.delivery is old
    await r.apply(saved.saved_config_revision, saved.active_runtime_revision)
    assert old.closed and r.view().active_transport == "windows_live_studio"
    stale = await c.post("/api/admin/operator/apply", json={"expected_saved_revision": saved.saved_config_revision,
        "expected_active_runtime_revision": saved.active_runtime_revision})
    assert stale.status_code == 409
    restarted = create_app(r.startup_settings)
    async with restarted.router.lifespan_context(restarted):
        assert restarted.state.delivery.view().transport == "windows_live_studio"
        assert not restarted.state.delivery.sender.auto_enabled


async def test_android_approval_and_queued_work_cannot_cross_switch(unified):
    app, c = unified
    r, old = app.state.operator_runtime, app.state.delivery
    record = await live_record(app)
    e = app.state.admin.suggestions.get(record.id)
    a = app.state.admin.approve_suggestion(e.suggestion_id, ApprovalInput(expected_suggestion_revision=e.suggestion_revision)).approval
    await r.live.disconnect(r.live.session_id)
    old.mode, old.armed_binding = "AUTOMATIC", (e.live_session_id, "own", e.active_runtime_revision)
    old.auto_queue.put_nowait(e.suggestion_id)
    entered, released = asyncio.Event(), asyncio.Event()
    async def pending():
        entered.set()
        await released.wait()
        pytest.fail("Retired delivery task resumed")
    task = asyncio.create_task(pending()); old.tasks.add(task)
    await entered.wait()
    await switch(app, "windows_live_studio")
    assert task.cancelled() and old.closed and old.mode == "MANUAL" and old.armed_binding is None
    assert old.auto_queue.empty() and not app.state.delivery.sender.auto_enabled
    await live_record(app, "124")
    body = ManualDeliveryInput(suggestion_id=e.suggestion_id, expected_suggestion_revision=e.suggestion_revision,
        approval_id=a.approval_id, idempotency_key="old_android")
    with pytest.raises(SuggestionConflict): await app.state.delivery._execute(body, "fill")
    response = await c.post("/api/admin/delivery/fill", json=body.model_dump())
    assert response.status_code == 409


async def test_windows_real_fake_arm_revoked_on_switch_and_auto_off(unified):
    app, c = unified
    await switch(app, "windows_live_studio")
    await live_record(app)
    r, old = app.state.operator_runtime, app.state.delivery
    native = FakeNative(old.settings.windows); old.sender.native = native
    await arm(old.sender, native)
    assert old.sender.target is not None
    old.sender.auto_enabled = True
    await r.live.disconnect(r.live.session_id)
    await switch(app, "android")
    assert old.closed and old.sender.target is None and not old.sender.auto_enabled
    assert not old.sender.check() and native.enters == 0 and not native.batches
    assert app.state.delivery.mode == "MANUAL" and app.state.delivery.armed_binding is None
    assert (await c.post("/api/admin/windows/arm", json={"empty_confirmed": True})).status_code == 409


async def test_windows_terminal_and_preparation_not_rejuvenated_by_roundtrip(unified):
    app, _ = unified
    await switch(app, "windows_live_studio")
    sender = app.state.delivery.sender
    sender.acceptance_spent = True; sender.fail_clock()
    await switch(app, "android")
    await switch(app, "windows_live_studio")
    sender = app.state.delivery.sender
    assert sender.acceptance_spent and sender.clock_verification == "unstable" and sender.closed
    assert not sender.auto_enabled and not sender.check()
    assert app.state.admin.preparation.view().state == "PRE-LIVE NOT PREPARED"


@pytest.mark.parametrize("transport", ["android", "windows_live_studio"])
async def test_both_transports_keep_exact_account_binding(unified, transport):
    app, c = unified
    await switch(app, transport)
    record = await live_record(app)
    e = app.state.admin.suggestions.get(record.id)
    context = app.state.admin.delivery_context().model_copy(update={"target_account": "foreign"})
    with pytest.raises(SuggestionConflict): app.state.admin.suggestions.approve(e.suggestion_id,
        ApprovalInput(expected_suggestion_revision=e.suggestion_revision), context)
    if transport == "windows_live_studio":
        await app.state.operator_runtime.live.disconnect(app.state.operator_runtime.live.session_id)
        result = await c.post("/api/admin/live/connect", json={"username": "foreign",
            "expected_active_runtime_revision": app.state.operator_runtime.active_runtime_revision})
        assert result.status_code == 409


@pytest.mark.parametrize("legacy", [None, "delivery.sqlite3", "windows-delivery.sqlite3"])
@pytest.mark.parametrize("status", ["reserved", "filled", "unknown", "confirmed", "not_sent"])
async def test_stable_identity_consumed_across_transport_session_restart(suggestion, tmp_path, legacy, status):
    r = suggestion
    journal = SQLiteDeliveryOperationStore(tmp_path / legacy) if legacy else UnifiedDeliveryOperationStore(tmp_path)
    first = request(r.e, r.approval, mode="AUTOMATIC" if legacy == "delivery.sqlite3" else "AUTO")
    if legacy == "delivery.sqlite3": first = first.model_copy(update={"durable_delivery_key": None})
    operation, _ = journal.reserve(first)
    if status != "reserved": journal.finish(operation.operation_id, DeliveryResult(status=status, reason="rejected"))
    journal.close()
    comment = Comment(username="viewer", text="Is it automatic?", message_id="123")
    e = r.store.register(record_for(comment, await r.processor.process(comment), "tiktok", 1, uuid4().hex), comment,
        r.processor, uuid4().hex, session_id=uuid4().hex, target_account="own")
    assert e.comment_identity != r.e.comment_identity and e.durable_delivery_key == r.e.durable_delivery_key
    assert consumed(tmp_path, e)
    restarted = UnifiedDeliveryOperationStore(tmp_path)
    assert restarted.has_envelope_operation(e)
    with pytest.raises(JournalConflict): restarted.reserve(request(e, r.approval, key="second_transport"))
    restarted.close()


async def test_api_reapproval_is_blocked_across_transport(unified):
    app, c = unified
    record = await live_record(app)
    e = app.state.admin.suggestions.get(record.id)
    a = app.state.admin.approve_suggestion(e.suggestion_id, ApprovalInput(expected_suggestion_revision=e.suggestion_revision)).approval
    journal = UnifiedDeliveryOperationStore(app.state.operator_runtime.settings.state_dir)
    journal.reserve(request(e, a)); journal.close()
    await app.state.operator_runtime.live.disconnect(app.state.operator_runtime.live.session_id)
    await switch(app, "windows_live_studio")
    second = await live_record(app)
    assert second.delivery_review.consumed
    response = await c.post(f"/api/admin/suggestions/{second.id}/approve", json={
        "expected_suggestion_revision": second.delivery_review.envelope.suggestion_revision})
    assert response.status_code == 409 and second.id not in app.state.admin.suggestions.approvals


async def test_status_never_creates_journals_or_io(unified):
    app, c = unified
    for transport in ("windows_live_studio", "android"):
        await switch(app, transport)
        assert (await c.get("/api/admin/delivery/status")).status_code == 200
        assert (await c.get("/api/admin/status")).json()["active_transport"] == transport
    assert not app.state.operator_runtime.settings.state_dir.exists()


def test_ui_renders_only_active_transport_controls():
    node = shutil.which("node")
    if not node: pytest.skip("Node required for DOM regression")
    source = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),els=new Map();
const el=()=>({value:'',children:[],append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x},addEventListener(){}});
const document={getElementById(id){if(!els.has(id))els.set(id,el());return els.get(id)}};
const window={};vm.runInNewContext(fs.readFileSync('backend/app/static/admin/delivery.js','utf8'),{window,document});
const controls=window.createDeliveryControls({api(){throw Error('No IO on render')},node:()=>el(),toast(){},state:{connected:true},refreshCards(){}});
controls.render({transport:'windows_live_studio',windows_sender:{},physical_delivery_enabled:false});
assert(!els.get('windows-panel').hidden);assert(els.get('manual-mode-panel').hidden);assert(els.get('inspect-device').hidden);
assert(els.get('prepare-mobile').hidden);
assert(els.get('active-transport').textContent==='PC / LIVE STUDIO');assert(els.get('auto-summary').textContent==='OFF');
controls.render({transport:'android',inspection:{devices:[]},mode:'MANUAL',physical_delivery_enabled:false});
assert(els.get('windows-panel').hidden);assert(!els.get('manual-mode-panel').hidden);assert(!els.get('inspect-device').hidden);
assert(els.get('active-transport').textContent==='MOBILE / ANDROID');
controls.render({transport:'android',paused:true,inspection:{devices:[]},mode:'MANUAL',physical_delivery_enabled:false});
assert(els.get('delivery-summary').textContent==='DELIVERY PAUSED — RE-PREPARE REQUIRED');
assert(!els.get('prepare-mobile').hidden && !els.get('prepare-mobile').disabled);
'''
    result = subprocess.run([node, "-e", source], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    html = Path("backend/app/static/admin/index.html").read_text(encoding="utf-8")
    from html.parser import HTMLParser
    class Elements(HTMLParser):
        ids = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs: self.ids.append(attrs["id"])
    parser = Elements(); parser.feed(html)
    assert len(parser.ids) == len(set(parser.ids))
    assert 'name="operator-transport" value="android"' in html and 'value="windows_live_studio"' in html


def test_sse_switch_invalidates_older_delivery_get():
    node = shutil.which("node")
    if not node: pytest.skip("Node required for DOM regression")
    source = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),els=new Map();
const el=()=>({value:'',append(){},replaceChildren(){},addEventListener(){}});
const document={getElementById(id){if(!els.has(id))els.set(id,el());return els.get(id)}};
const window={};vm.runInNewContext(fs.readFileSync('backend/app/static/admin/delivery.js','utf8'),{window,document});
let resolveStatus;const pending=new Promise(r=>resolveStatus=r);
const controls=window.createDeliveryControls({api:p=>p.endsWith('/history')?Promise.resolve([]):pending,node:()=>el(),toast(){},state:{connected:true},refreshCards(){}});
(async()=>{const oldGet=controls.refresh();controls.render({transport:'android',inspection:{devices:[]},mode:'MANUAL'});
resolveStatus({transport:'windows_live_studio',windows_sender:{}});await oldGet;
assert(els.get('active-transport').textContent==='MOBILE / ANDROID');assert(els.get('windows-panel').hidden);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", source], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr


async def test_optional_mobile_one_shot_auto_returns_off_and_cannot_rearm(phone_factory):
    p = await phone_factory(gate=True)
    p.service.settings = p.service.settings.model_copy(update={"mobile_acceptance_one_shot": True})
    arm_phone(p); confirm_after_tap(p)
    result = await p.service.send(p.body)
    assert result.status == "confirmed" and p.t.activations == 1
    assert p.service.mode == "MANUAL" and p.service.acceptance_spent and p.service.auto_queue.empty()
    with pytest.raises(SuggestionConflict):
        p.service.set_mode("AUTOMATIC", p.e.live_session_id, p.e.active_runtime_revision)
    assert p.t.activations == 1


async def test_android_first_ingestion_cannot_be_rejuvenated_in_windows_epoch(unified):
    app, _ = unified
    r = app.state.operator_runtime
    await r.live.connect("own")
    await r.live.service.started.wait()
    first = Comment(username="viewer", text="Is it automatic?", message_id="123")
    r.live.service.on_receipt(first)
    receipt = first._ingestion_receipt
    assert receipt is not None
    await r.live.disconnect(r.live.session_id)
    await switch(app, "windows_live_studio")
    await r.live.connect("own", expected_active_runtime_revision=r.active_runtime_revision)
    await r.live.service.started.wait()
    replay = Comment(username="viewer", text="Is it automatic?", message_id="123")
    r.live.service.on_receipt(replay)
    record = await r.live.service.on_comment(replay)
    assert replay._ingestion_receipt is receipt
    assert app.state.admin.suggestions.get(record.id)._ingestion_receipt is None
    assert not app.state.delivery.auto_eligibility(record.id).eligible


async def test_mobile_full_admin_pipeline_uses_unified_registry_and_positive_verification(unified):
    app, client = unified
    runtime = app.state.operator_runtime
    await runtime.save(OperatorOverlay(tiktok={"username": ACCOUNT, "transport": "android"}), runtime.saved_config_revision)
    await runtime.apply(runtime.saved_config_revision, runtime.active_runtime_revision)
    service = app.state.delivery
    transport = Transport()
    service.controller = ADBAndroidController(transport, physical_enabled=False, confirmation_interval=0)
    assert (await client.post("/api/admin/device/verify-account", json={})).status_code == 200
    assert (await client.post("/api/admin/live/connect", json={"username": ACCOUNT})).status_code == 202
    await runtime.live.service.started.wait()
    comment = Comment(username="viewer", text="Is it automatic?", message_id="321")
    runtime.live.service.on_receipt(comment)
    record = await runtime.live.service.on_comment(comment)
    e = app.state.admin.suggestions.get(record.id)
    review = await client.post(f"/api/admin/suggestions/{record.id}/approve", json={"expected_suggestion_revision": e.suggestion_revision})
    assert review.status_code == 200
    approval = review.json()["approval"]
    transport.xml = capture("live_viewer")
    assert (await client.post("/api/admin/device/inspect", json={})).status_code == 200
    transport.xml = xml("composer")
    assert (await client.post("/api/admin/device/inspect", json={})).status_code == 200
    body = {"suggestion_id": record.id, "expected_suggestion_revision": e.suggestion_revision,
        "approval_id": approval["approval_id"], "idempotency_key": "full_mobile_pipeline"}
    result = await client.post("/api/admin/delivery/fill", json=body)
    assert result.status_code == 200 and result.json()["status"] == "filled"
    assert service.journal.path.name == "unified-delivery.sqlite3"
    assert not {AdbCommand.TAP_SEND, AdbCommand.TAB, AdbCommand.ACTIVATE} & {cmd for cmd, _ in transport.calls}
    # Simulate the operator's Send using a synthetic post-send feed snapshot.
    transport.xml = posted(e.suggested_reply)
    verified = await client.post("/api/admin/delivery/verify-fill", json={})
    assert verified.status_code == 200 and verified.json()["status"] == "confirmed"
    assert service.journal.lookup(body["idempotency_key"]).status == "confirmed"
    assert transport.activations == 0 and service.mode == "MANUAL"


@pytest.fixture
async def manual_fill_api(settings, tmp_path, monkeypatch):
    from test_tiktok_you import empty_reviewed_composer
    import xml.etree.ElementTree as ET
    cfg = settings.model_copy(update={"tiktok_username": ACCOUNT, "tiktok_transport": "android",
        "state_dir": tmp_path / "state", "log_dir": tmp_path / "logs",
        "enable_physical_tiktok_send": False, "enable_physical_windows_send": False})
    monkeypatch.setattr("app.main.LiveService", MockLive)
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        service, transport = app.state.delivery, Transport()
        service.controller = ADBAndroidController(transport, physical_enabled=False)
        service.controller.on_stage = service._stage
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            client.headers["X-Admin-CSRF"] = app.state.admin.csrf_token
            assert (await client.post("/api/admin/device/verify-account", json={})).status_code == 200
            assert (await client.post("/api/admin/live/connect", json={"username": ACCOUNT})).status_code == 202
            runtime = app.state.operator_runtime
            await runtime.live.service.started.wait()
            comment = Comment(username="viewer", text="Is it automatic?", message_id="4321")
            runtime.live.service.on_receipt(comment)
            record = await runtime.live.service.on_comment(comment)
            envelope = app.state.admin.suggestions.get(record.id)
            approved = await client.post(f"/api/admin/suggestions/{record.id}/approve",
                json={"expected_suggestion_revision": envelope.suggestion_revision})
            assert approved.status_code == 200
            transport.xml = ET.tostring(empty_reviewed_composer(), encoding="unicode")
            status = await client.post("/api/admin/device/inspect", json={})
            assert status.status_code == 200
            body = {"suggestion_id": record.id, "expected_suggestion_revision": envelope.suggestion_revision,
                "approval_id": approved.json()["approval"]["approval_id"], "idempotency_key": "manual_gate_false"}
            yield app, client, service, transport, envelope, body


async def test_manual_fill_api_gate_false_without_ui_live_or_send_authority(manual_fill_api):
    from app.android.tiktok_ui import unique_node, rid
    app, client, service, transport, envelope, body = manual_fill_api
    status = (await client.get("/api/admin/delivery/status")).json()
    assert not status["physical_delivery_enabled"] and not status["physical_ready"]
    assert status["mode"] == "MANUAL" and status["auto_state"] == "AUTO OFF"
    assert status["fill_ready"] and status["delivery"] == "FILL READY" and status["reason"] == "ready"
    assert status["inspection"]["account_verified"] and not status["inspection"]["live_verified"]
    assert status["inspection"]["send_control_state"] == "UNAVAILABLE"
    assert service.controller._live_evidence is None
    binding = service.controller._manual_fill_evidence
    assert not service.controller.fill_ready_for("f" * 32, ACCOUNT)
    assert not service.controller.fill_ready_for(envelope.live_session_id, "other")
    service.controller._manual_fill_evidence = (*binding[:3], binding[3] - 91)
    assert not service.controller.fill_ready_for(envelope.live_session_id, ACCOUNT)
    service.controller._manual_fill_evidence = binding
    review = (await client.get(f"/api/admin/suggestions/{envelope.suggestion_id}")).json()
    assert review["fill_allowed"] and review["eligibility"]["eligible"]
    assert not review["physical_send_allowed"] and not app.state.admin.settings.enable_physical_windows_send
    def reserved_before_input(command):
        if command in {AdbCommand.SELECT_ALL, AdbCommand.DELETE, AdbCommand.TEXT}:
            assert service.journal.lookup(body["idempotency_key"]).status == "reserved"
        return asyncio.sleep(0)
    transport.after = reserved_before_input
    response = await client.post("/api/admin/delivery/fill", json=body)
    assert response.status_code == 200 and response.json() == {"status":"filled", "reason":"filled_waiting_user_send"}
    assert unique_node(transport.xml, rid("h4k")).get("text") == envelope.suggested_reply
    entries = [args["text"] for command, args in transport.calls if command == AdbCommand.TEXT]
    assert entries == [envelope.suggested_reply]
    assert not {AdbCommand.TAP_SEND, AdbCommand.TAB, AdbCommand.ACTIVATE} & {c for c, _ in transport.calls}
    assert transport.activations == 0 and service.controller._live_evidence is None
    assert service.controller.last.screen.value == "COMMENT_INPUT_READY" and not service.controller.last.live_verified
    assert service.journal.lookup(body["idempotency_key"]).status == "filled"
    baseline = len(entries)
    assert (await client.post("/api/admin/delivery/fill", json=body)).json()["status"] != "filled"
    assert len([c for c, _ in transport.calls if c == AdbCommand.TEXT]) == baseline


async def test_manual_capability_never_enables_programmatic_send_or_auto(manual_fill_api):
    app, client, service, transport, _, body = manual_fill_api
    transport.calls.clear()
    result = await client.post("/api/admin/delivery/send", json=body)
    assert result.status_code == 200 and result.json()["reason"] == "physical_delivery_disabled"
    runtime = app.state.operator_runtime
    automatic = await client.post("/api/admin/delivery/mode", json={"mode":"AUTOMATIC",
        "expected_live_session_id":runtime.live.session_id, "expected_active_runtime_revision":runtime.active_runtime_revision})
    assert automatic.status_code == 409 and service.mode == "MANUAL"
    assert not transport.calls and service.controller._live_evidence is None


@pytest.mark.parametrize("field", ["text", "suggested_reply", "device_serial"])
async def test_manual_fill_api_rejects_browser_text_or_device_substitution(manual_fill_api, field):
    _, client, _, transport, _, body = manual_fill_api
    transport.calls.clear()
    response = await client.post("/api/admin/delivery/fill", json=body | {field:"arbitrary"})
    assert response.status_code == 422 and not transport.calls


@pytest.mark.parametrize("failure", ["unknown", "security", "account", "device", "install", "version",
    "stale", "session", "runtime", "unapproved", "consumed", "focus"])
async def test_manual_fill_api_keeps_all_pre_input_guards(manual_fill_api, failure):
    app, client, service, transport, envelope, body = manual_fill_api
    if failure == "unknown": transport.xml = '<hierarchy><node package="com.zhiliaoapp.musically"/></hierarchy>'
    elif failure == "security": transport.xml = transport.xml.replace("Введите сообщение...", "captcha")
    elif failure == "account": service.controller._account_binding = (service.controller._installation, "other")
    elif failure == "device": transport.serial = "other"
    elif failure == "install": transport.info = transport.info.replace("10:00:00", "11:00:00")
    elif failure == "version": transport.info = transport.info.replace("46.8.3", "46.8.4")
    elif failure == "stale": body = body | {"expected_suggestion_revision":"f" * 64}
    elif failure == "session":
        runtime = app.state.operator_runtime
        await runtime.live.disconnect(runtime.live.session_id)
        await runtime.live.connect(ACCOUNT)
        await runtime.live.service.started.wait()
    elif failure == "runtime": app.state.operator_runtime.active_runtime_revision = "f" * 32
    elif failure == "unapproved": app.state.admin.suggestions.approvals.clear()
    elif failure == "consumed":
        journal = service.journal_factory()
        try:
            approval = app.state.admin.suggestions.approvals[envelope.suggestion_id]
            operation, reserved = journal.reserve(request(envelope, approval, key="consumed_manual"))
            assert reserved
            journal.finish(operation.operation_id, DeliveryResult(status="unknown", reason="timeout"))
        finally: journal.close()
    elif failure == "focus": transport.xml = transport.xml.replace('focused="true"', 'focused="false"')
    transport.calls.clear()
    response = await client.post("/api/admin/delivery/fill", json=body)
    assert response.status_code in {200,409}
    if response.status_code == 200: assert response.json()["status"] != "filled"
    assert not {AdbCommand.SELECT_ALL, AdbCommand.DELETE, AdbCommand.TEXT, AdbCommand.TAP_SEND,
        AdbCommand.TAB, AdbCommand.ACTIVATE} & {c for c, _ in transport.calls}


@pytest.fixture
async def captured_manual_fill_api(manual_fill_api, monkeypatch):
    """Operator-provided hierarchy(8)/metadata(8); read-only ADB returns exact bytes."""
    from hashlib import sha256
    app, client, service, transport, envelope, body = manual_fill_api
    directory = Path(__file__).parent / "fixtures/android/reviewed_manual_fill_4683"
    raw = (directory / "hierarchy.xml").read_bytes()
    metadata = json.loads((directory / "metadata.json").read_bytes())
    assert sha256(raw).hexdigest() == metadata["hierarchy_sha256"] == "1f64b5ad6665183d7a70bf8ebc5d4ad92e42f84dbf26f040d16e5364f3142a98"
    assert (metadata["device_model"], metadata["android_version"], metadata["version"]) == ("M2101K9G", "13", "46.8.3")
    assert metadata["foreground_activity"] == "com.windukk.mod.icon.default" and metadata["profile_verified"]
    transport.xml = raw.decode("utf-8")
    original = transport.run
    reads = []
    async def preserving_raw(command, **kwargs):
        before = transport.xml
        result = await original(command, **kwargs)
        if command not in {AdbCommand.DELETE, AdbCommand.TEXT, AdbCommand.TAB}:
            transport.xml = before
        if command == AdbCommand.HIERARCHY:
            reads.append(before.encode("utf-8"))
            return before
        return result
    monkeypatch.setattr(transport, "run", preserving_raw)
    transport.calls.clear()
    yield manual_fill_api, raw, reads


@pytest.mark.parametrize("take_capture", [False, True])
async def test_exact_capture_status_capture_then_final_manual_fill(captured_manual_fill_api, tmp_path, monkeypatch, take_capture):
    fixture, raw, reads = captured_manual_fill_api
    app, client, service, transport, envelope, body = fixture
    monkeypatch.setattr("app.android.diagnostics.CAPTURE_ROOT", tmp_path / "captures")
    status = (await client.post("/api/admin/device/inspect", json={})).json()
    assert status["inspection"]["screen"] == "COMMENT_INPUT_READY"
    assert status["inspection"]["comment_input_found"] and status["fill_ready"]
    assert not status["inspection"]["live_verified"] and not status["inspection"]["send_button_found"]
    binding = service.controller._account_binding
    if take_capture:
        captured = await client.post("/api/admin/device/capture", json={})
        assert captured.status_code == 200
        assert (tmp_path / "captures" / captured.json()["capture_id"] / "hierarchy.xml").read_bytes() == raw
    result = await client.post("/api/admin/delivery/fill", json=body)
    assert result.json() == {"status": "filled", "reason": "filled_waiting_user_send"}
    assert service.controller._account_binding == binding
    assert service.controller.last.account_verified
    assert service.controller.last.screen.value == "COMMENT_INPUT_READY"
    assert not service.controller.last.live_verified and not service.controller.physical_enabled
    count = 6 if take_capture else 5  # status, optional capture, admission, controller, clear/delete guards
    assert reads[:count] == [raw] * count
    assert [args["text"] for command, args in transport.calls if command == AdbCommand.TEXT] == [envelope.suggested_reply]
    assert not {AdbCommand.TAP_SEND, AdbCommand.TAB, AdbCommand.ACTIVATE} & {c for c, _ in transport.calls}
    assert service.journal.lookup(body["idempotency_key"]).status == "filled"


INPUT_ACTIONS = {AdbCommand.SELECT_ALL, AdbCommand.DELETE, AdbCommand.TEXT,
    AdbCommand.TAP_SEND, AdbCommand.TAB, AdbCommand.ACTIVATE}


@pytest.mark.parametrize("phase", [1, 2, 3])
@pytest.mark.parametrize("incomplete", ["dump", "empty", "partial"])
async def test_manual_one_incomplete_acquisition_recheck_before_input(captured_manual_fill_api, monkeypatch, phase, incomplete):
    fixture, raw, reads = captured_manual_fill_api
    _, client, service, transport, envelope, body = fixture
    original, failures, acquisitions = transport.run, [], []
    async def fail_once(command, **kwargs):
        if command == AdbCommand.DUMP:
            acquisitions.append(command)
            if len(acquisitions) == phase and incomplete == "dump":
                failures.append(command)
                return "ERROR: could not get idle state"
        if command == AdbCommand.HIERARCHY and len(acquisitions) == phase and incomplete != "dump":
            failures.append(command)
            return "" if incomplete == "empty" else '<?xml version="1.0"?><hierarchy><node'
        return await original(command, **kwargs)
    monkeypatch.setattr(transport, "run", fail_once)
    result = await client.post("/api/admin/delivery/fill", json=body)
    assert result.json() == {"status":"filled", "reason":"filled_waiting_user_send"}
    assert len(failures) == 1 and reads[0] == raw
    assert [args["text"] for command, args in transport.calls if command == AdbCommand.TEXT] == [envelope.suggested_reply]
    assert not {AdbCommand.TAP_SEND, AdbCommand.TAB, AdbCommand.ACTIVATE} & {c for c, _ in transport.calls}
    assert service.journal.lookup(body["idempotency_key"]).status == "filled"


@pytest.mark.parametrize("failure", ["twice", "unknown", "security", "foreign", "conflict", "offline", "budget", "timeout", "account"])
async def test_exact_capture_manual_failures_block_before_input(captured_manual_fill_api, monkeypatch, caplog, failure):
    from app.android.adb import AdbError
    fixture, _, _ = captured_manual_fill_api
    _, client, service, transport, _, body = fixture
    original, acquisitions = transport.run, []
    if failure == "timeout": monkeypatch.setattr("app.android.manual_inspection.RECHECK_TIMEOUT", 0.01)
    if failure == "account": service.controller.invalidate_trust()
    async def failing_read(command, **kwargs):
        if command == AdbCommand.DUMP:
            acquisitions.append(command)
            if failure in {"twice", "timeout"} or failure == "budget" and len(acquisitions) in {1, 3}:
                return "ERROR: could not get idle state"
        if command == AdbCommand.FOREGROUND:
            if failure == "foreign": return "ResumedActivity: other.app/.Main\nmFocusedApp=ActivityRecord{x u0 other.app/.Main t1}"
            if failure == "conflict": return "ResumedActivity: com.zhiliaoapp.musically/com.windukk.mod.icon.default\nmFocusedApp=ActivityRecord{x u0 other.app/.Main t1}"
            if failure == "offline": raise AdbError()
        if command == AdbCommand.HIERARCHY:
            if failure == "unknown": return '<hierarchy><node package="com.zhiliaoapp.musically"/></hierarchy>'
            if failure == "security": return transport.xml.replace("Введите сообщение...", "captcha")
        return await original(command, **kwargs)
    monkeypatch.setattr(transport, "run", failing_read)
    result = await client.post("/api/admin/delivery/fill", json=body)
    assert result.status_code == 200 and result.json()["status"] != "filled"
    assert not INPUT_ACTIONS & {c for c, _ in transport.calls}
    assert len(acquisitions) <= (2 if failure == "twice" else 3 if failure == "budget" else 1)
    if failure == "account":
        assert result.json()["reason"] == "account_unverified"
        assert service.controller.last.screen.value == "COMMENT_INPUT_READY"
        assert not service.controller.last.account_verified
    record = next(r for r in caplog.records if "ANDROID_MANUAL_FILL_BLOCKED " in r.getMessage())
    detail = json.loads(record.getMessage().split("ANDROID_MANUAL_FILL_BLOCKED ")[1])
    assert detail["final_block_reason"] in {"unknown_screen", "security_ui", "account_unverified"}
    assert "runtime_revision" in detail and "hierarchy_sha256" in detail and "session_match" in detail
    assert "Введите сообщение" not in record.getMessage() and "<hierarchy" not in record.getMessage()


@pytest.mark.parametrize("after_command", [AdbCommand.SELECT_ALL, AdbCommand.TEXT])
async def test_manual_never_rechecks_acquisition_after_input_starts(captured_manual_fill_api, monkeypatch, after_command):
    fixture, _, _ = captured_manual_fill_api
    _, client, service, transport, _, body = fixture
    original, started, failures = transport.run, False, []
    async def fail_after_input(command, **kwargs):
        nonlocal started
        if command == AdbCommand.DUMP and started:
            failures.append(command)
            return "ERROR: could not get idle state"
        result = await original(command, **kwargs)
        if command == after_command: started = True
        return result
    monkeypatch.setattr(transport, "run", fail_after_input)
    response = await client.post("/api/admin/delivery/fill", json=body)
    assert response.json()["status"] != "filled" and len(failures) == 1
    assert len([c for c, _ in transport.calls if c == AdbCommand.TEXT]) == (1 if after_command == AdbCommand.TEXT else 0)
    assert not {AdbCommand.TAP_SEND, AdbCommand.TAB, AdbCommand.ACTIVATE} & {c for c, _ in transport.calls}
    assert service.journal.lookup(body["idempotency_key"]).status != "filled"
