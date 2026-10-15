"""Production API/LiveService/listener/controller/SQLite, fake SDK and OS only."""
import asyncio
from datetime import UTC, datetime, timedelta
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from TikTokLive.events import CommentEvent, ConnectEvent, DisconnectEvent

from app.android.adb import AdbTransport
from app.android.controller import ADBAndroidController
from app.delivery.models import DeliveryResult
from app.live.models import Comment
from app.main import create_app
from test_android_hierarchy_acquisition import FakeDevice, Process
from test_manual_send_rc import Transport, xml

OWN = "synthetic_owner"
INPUT = {"input", "tap", "keyevent", "keycombination"}


class Device(FakeDevice):
    def __init__(self):
        super().__init__(["idle"])
        self.info = Transport().info
        self.after = None
    async def spawn(self, *args, **kwargs):
        if self.after: await self.after(args)
        if args[-3:] == ("dumpsys", "package", "com.zhiliaoapp.musically"):
            self.calls.append(args)
            return Process(self.info.encode())
        return await super().spawn(*args, **kwargs)


class SDK:
    def __init__(self):
        self.handlers = {}
        self.closed = asyncio.Event()
        self.web = SimpleNamespace(close=AsyncMock())
    def add_listener(self, event, handler): self.handlers[event] = handler
    async def start(self):
        task = asyncio.create_task(self.closed.wait())
        self.handlers[ConnectEvent](ConnectEvent(unique_id=OWN, room_id=123456))
        return task
    async def disconnect(self): self.closed.set()
    def comment(self, text, *, author=OWN, identity="1001", created=None, user=True):
        event = CommentEvent.from_dict({"content": text,
            "user": {"displayId": author if user else "", "nickname": "Synthetic Host", "id": "12345"},
            "common": {"msgId": identity, "createTime": str(int((created or datetime.now(UTC)).timestamp()))}})
        self.handlers[CommentEvent](event)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate(): await asyncio.sleep(.001)


@pytest.fixture
async def mobile(settings, tmp_path, monkeypatch):
    phone, clients = Device(), []
    executable = tmp_path / "adb.exe"; executable.touch()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=phone.spawn))
    def sdk(**kwargs):
        assert kwargs["unique_id"] == OWN
        client = SDK(); clients.append(client); return client
    monkeypatch.setattr("app.live.listener.create_sdk_client", sdk)
    cfg = settings.model_copy(update={"tiktok_username": OWN, "tiktok_transport": "android",
        "adb_executable": executable, "state_dir": tmp_path / "state", "log_dir": tmp_path / "logs",
        "enable_physical_tiktok_send": False, "enable_physical_windows_send": False,
        "reconnect_initial_seconds": .001, "reconnect_max_seconds": .001})
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as http:
            http.headers["X-Admin-CSRF"] = app.state.admin.csrf_token
            yield SimpleNamespace(app=app, http=http, phone=phone, clients=clients,
                service=app.state.delivery, runtime=app.state.operator_runtime, config=cfg)
    assert not any(INPUT & set(args) for args in phone.calls)


async def prepare(m):
    result = await m.http.post("/api/admin/delivery/prepare", json={})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["mobile_prepared"] and not data["inspection"]["account_verified"]
    assert data["inspection"]["device"] == "CONNECTED" and data["auto_state"] == "AUTO OFF"
    assert data["inspection"]["reason"] == "hierarchy_unavailable"
    assert m.service.controller._account_binding is None
    assert m.service.journal is not None  # Real SQLite, never reset.


async def connect(m):
    await prepare(m)
    m.phone.snapshots = [xml("live_idle")]
    m.phone.activity = "com.windukk.mod.icon.default"
    result = await m.http.post("/api/admin/live/connect", json={"username": OWN})
    assert result.status_code == 202, result.text
    await until(lambda: m.service.account_proof.state == "WAITING")
    return m.service.account_proof.challenge.token


async def verify(m):
    token = await connect(m)
    m.clients[-1].comment(token, author=OWN.upper())
    await until(lambda: m.service.account_proof.state == "VERIFIED")
    assert m.service.controller.last.account_verified
    return token


async def test_two_channel_challenge_enables_existing_manual_path_without_input(mobile, caplog):
    m = mobile
    token = await verify(m)
    assert re.fullmatch(r"MOB-[A-F0-9]{24}", token)
    assert m.service.account_proof.valid()
    assert m.runtime.live.service.queue.empty() and m.app.state.admin.current_comment_count == 0
    assert not m.app.state.admin.live_history and not m.app.state.admin.suggestions.groups["live"]
    assert token not in caplog.text
    assert m.service.journal.connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    m.clients[-1].comment("Is it automatic?", author="external_viewer", identity="1002")
    await until(lambda: bool(m.app.state.admin.live_history))
    record = m.app.state.admin.live_history[0]
    envelope = m.app.state.admin.suggestions.get(record.id)
    result = await m.http.post(f"/api/admin/suggestions/{record.id}/approve",
        json={"expected_suggestion_revision": envelope.suggestion_revision})
    assert result.status_code == 200 and result.json()["approval_state"] == "approved"
    m.phone.snapshots = [xml("composer")]
    inspected = await m.http.post("/api/admin/device/inspect", json={})
    assert inspected.json()["fill_ready"] and inspected.json()["inspection"]["account_verified"]
    assert (await m.http.get(f"/api/admin/suggestions/{record.id}")).json()["fill_allowed"]
    assert m.service.mode == "MANUAL" and not m.service.settings.enable_physical_tiktok_send


@pytest.mark.parametrize("failure", ["author", "text", "whitespace", "session", "runtime", "selected_device", "install", "expired",
    "stale_event", "missing_id", "missing_handle", "reused_id", "forged_receipt", "fake", "replay", "playground", "security", "foreground", "sdk_account"])
async def test_invalid_evidence_cannot_verify_or_enter_ai(mobile, monkeypatch, failure):
    m = mobile
    token = await connect(m)
    proof, listener = m.service.account_proof, m.runtime.live.service.listener
    if failure == "session": m.runtime.live.session_id = "e" * 32
    elif failure == "runtime": m.runtime.active_runtime_revision = "e" * 32
    elif failure == "selected_device": m.service.controller.serial = "foreign"
    elif failure == "install": m.phone.info = m.phone.info.replace("10:00:00", "11:00:00")
    elif failure == "expired": proof.challenge.expires -= 61
    elif failure in {"fake", "replay"}: listener.settings = listener.settings.model_copy(update={"live_mode": failure})
    elif failure == "security": m.phone.snapshots = ['<hierarchy><node text="captcha"/></hierarchy>']
    elif failure == "foreground": m.phone.activity = "foreign.Activity"
    elif failure == "sdk_account": listener.connected_handle = "foreign"
    if failure == "reused_id": proof.seen.add((OWN, "1001"))
    if failure in {"forged_receipt", "playground"}:
        fake = Comment(username=OWN, author_handle=OWN, text=token, message_id="1001",
            source="playground" if failure == "playground" else "tiktok", server_created_at=datetime.now(UTC))
        assert proof.observe(fake)
    else:
        m.clients[-1].comment("MOB-WRONG" if failure == "text" else " " + token + " " if failure == "whitespace" else token,
            author="foreign" if failure == "author" else OWN, identity="0" if failure == "missing_id" else "1001",
            created=datetime.now(UTC)-timedelta(minutes=2) if failure == "stale_event" else None,
            user=failure != "missing_handle")
    await until(lambda: not any(not t.done() for t in m.service.tasks))
    assert not m.service.account_proof.valid() and not m.service.controller.last.account_verified
    assert not m.app.state.admin.live_history and m.runtime.live.service.queue.empty()
    assert m.service.mode == "MANUAL"


async def test_duplicate_event_cannot_reverify_or_refresh_account_lease(mobile):
    m = mobile
    token = await verify(m)
    evidence = m.service.controller._live_evidence
    count = len(m.phone.calls)
    m.clients[-1].comment(token)
    await asyncio.sleep(.01)
    assert m.service.account_proof.valid() and m.service.controller._live_evidence == evidence
    assert len(m.phone.calls) == count and not m.app.state.admin.live_history


async def test_reconnect_revokes_old_token_and_old_sdk_callback(mobile):
    m = mobile
    first = await connect(m)
    old = m.clients[-1]
    old.handlers[DisconnectEvent](None)
    old.closed.set()
    assert m.service.account_proof.challenge is None
    await until(lambda: len(m.clients) == 2 and m.service.account_proof.state == "WAITING")
    second = m.service.account_proof.challenge.token
    assert first != second
    old.comment(second)
    m.clients[-1].comment(first, identity="1002")
    assert not m.service.controller.last.account_verified
    m.clients[-1].comment(second, identity="1003")
    await until(lambda: m.service.account_proof.state == "VERIFIED")
    assert not m.app.state.admin.live_history


async def test_apply_restart_and_disconnect_invalidate_binding(mobile):
    m = mobile
    await verify(m)
    await m.runtime.live.disconnect(m.runtime.live.session_id)
    assert not m.service.controller.last.account_verified and not m.service.account_proof.valid()
    await m.runtime.apply(m.runtime.saved_config_revision, m.runtime.active_runtime_revision)
    assert not m.service.account_proof.prepared_now() and not m.service.controller._account_binding
    restarted = create_app(m.config)
    async with restarted.router.lifespan_context(restarted):
        assert not restarted.state.delivery.account_proof.valid()
        assert not restarted.state.delivery.controller.last.account_verified


async def test_browser_cannot_choose_token_or_connect_foreign_account(mobile):
    m = mobile
    before = m.runtime.active_runtime_revision
    assert (await m.http.post("/api/admin/device/live-account-challenge", json={})).status_code == 409
    assert (await m.http.post("/api/admin/device/live-account-challenge", json={"token":"MOB-FORGED"})).status_code == 422
    assert (await m.http.post("/api/admin/live/connect", json={"username":"foreign"})).status_code == 409
    assert m.runtime.live.service is None and not m.clients and m.runtime.active_runtime_revision == before
    token = await connect(m)
    assert (await m.http.post("/api/admin/device/live-account-challenge", json={})).status_code == 409
    assert m.service.account_proof.challenge.token == token


async def test_challenge_needs_fresh_reviewed_host_markers(mobile):
    m = mobile
    await prepare(m)
    m.phone.snapshots = [xml("composer")]
    m.phone.activity = "com.windukk.mod.icon.default"
    assert (await m.http.post("/api/admin/live/connect", json={"username":OWN})).status_code == 202
    await until(lambda: bool(m.clients) and not m.service.tasks)
    result = await m.http.post("/api/admin/device/live-account-challenge", json={})
    assert result.status_code == 409 and not m.service.account_proof.challenge
    assert not m.service.controller.last.account_verified


async def test_install_change_during_issue_or_readback_rejects(mobile):
    m = mobile
    token = await connect(m)
    async def changed(args):
        if "uiautomator" in args: m.phone.info = m.phone.info.replace("10:00:00", "11:00:00")
    m.phone.after = changed
    m.clients[-1].comment(token)
    await until(lambda: not m.service.tasks)
    assert not m.service.account_proof.valid() and not m.service.controller.last.account_verified


async def test_journal_lookup_failure_is_fail_closed(mobile, monkeypatch):
    m = mobile
    token = await connect(m)
    def unavailable(*args, **kwargs): raise OSError("private DB failure")
    monkeypatch.setattr(m.service.journal, "_duplicate", unavailable)
    m.clients[-1].comment(token)
    assert not m.service.account_proof.valid() and not m.service.controller.last.account_verified
    assert m.service.account_proof.challenge is None and not m.app.state.admin.live_history


async def test_failed_receipt_observer_cannot_leak_challenge_to_ai_or_logs(mobile, caplog):
    m = mobile
    token = await connect(m)
    def broken(comment): raise RuntimeError("private observer failure")
    m.runtime.live.service.on_receipt = broken
    m.clients[-1].comment(token)
    await asyncio.sleep(.01)
    assert m.runtime.live.service.queue.empty() and not m.app.state.admin.live_history
    assert not m.service.account_proof.valid() and token not in caplog.text


async def test_auto_cannot_arm_before_manual_acceptance_even_with_startup_gate(mobile):
    m = mobile
    await verify(m)
    m.service.settings = m.service.settings.model_copy(update={"enable_physical_tiktok_send":True})
    m.phone.snapshots = [xml("composer")]
    result = await m.http.post("/api/admin/device/inspect", json={})
    candidate = result.json()["inspection"]["send_confirmation_token"]
    assert candidate
    assert (await m.http.post("/api/admin/device/confirm-send", json={"token":candidate,"confirmed":True})).status_code == 200
    result = await m.http.post("/api/admin/delivery/mode", json={"mode":"AUTOMATIC",
        "expected_live_session_id":m.runtime.live.session_id,"expected_active_runtime_revision":m.runtime.active_runtime_revision})
    assert result.status_code == 409 and m.service.mode == "MANUAL"


@pytest.mark.parametrize("offline", [False, True])
async def test_uiautomator_timeout_uses_connected_transport_probe(tmp_path, monkeypatch, offline):
    phone = Device()
    hanging = []
    class Hung:
        def __init__(self):
            self.returncode = None
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.stopped = asyncio.Event()
        def kill(self):
            self.returncode = -9
            self.stdout.feed_eof(); self.stderr.feed_eof(); self.stopped.set()
        async def wait(self): await self.stopped.wait(); return self.returncode
    async def spawn(*args, **kwargs):
        if "uiautomator" in args:
            process = Hung(); hanging.append(process)
            phone.offline = offline
            return process
        return await phone.spawn(*args, **kwargs)
    path = tmp_path / "adb.exe"; path.touch()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))
    transport = AdbTransport(path, timeout=.5, hierarchy_timeout=.1)
    c = ADBAndroidController(transport)
    state = await c.inspect()
    assert state.reason == ("adb_failed" if offline else "hierarchy_unavailable")
    assert state.screen.value == ("DEVICE_DISCONNECTED" if offline else "UNKNOWN_SCREEN")
    assert hanging and not transport.processes and all(p.returncode == -9 for p in hanging)


async def test_expired_host_lease_still_blocks_composer_fill(mobile):
    m = mobile
    await verify(m)
    c = m.service.controller
    c._live_evidence = (*c._live_evidence[:3], c._live_evidence[3] - 91)
    m.phone.snapshots = [xml("composer")]
    result = await m.http.post("/api/admin/device/inspect", json={})
    assert not result.json()["fill_ready"] and not result.json()["inspection"]["live_verified"]


async def test_durable_consumed_message_cannot_verify_even_without_receipt_cache(mobile):
    from test_unified_transports import request
    m = mobile
    token = await connect(m)
    m.clients[-1].comment("Is it automatic?", author="external_viewer", identity="1001")
    await until(lambda: bool(m.app.state.admin.live_history))
    record = m.app.state.admin.live_history[0]
    envelope = m.app.state.admin.suggestions.get(record.id)
    approved = await m.http.post(f"/api/admin/suggestions/{record.id}/approve",
        json={"expected_suggestion_revision":envelope.suggestion_revision})
    assert approved.status_code == 200
    approval = m.app.state.admin.suggestions.approvals[record.id]
    operation, reserved = m.service.journal.reserve(request(envelope, approval, key="consumed_challenge_message"))
    assert reserved
    m.service.journal.finish(operation.operation_id, DeliveryResult(status="confirmed",reason="feed_confirmed"))
    # Isolate the durable guard: an absent process receipt cache cannot erase SQLite consumption.
    m.service.account_proof.seen.discard((OWN, "1001"))
    m.clients[-1].comment(token, identity="1001")
    assert not m.service.account_proof.valid() and not m.service.controller.last.account_verified
    assert m.service.journal.lookup("consumed_challenge_message").status == "confirmed"


async def test_only_successful_manual_readonly_verification_unlocks_auto_acceptance(mobile):
    from app.android.feed import host_feed
    from app.delivery.models import AndroidTarget, ManualDeliveryInput
    from test_guarded_delivery import capture, posted
    from test_unified_transports import request
    m = mobile
    await verify(m)
    assert not m.service.account_proof.manual_accepted

    m.clients[-1].comment("Is it automatic?", author="external_viewer", identity="1002")
    await until(lambda: bool(m.app.state.admin.live_history))
    record = m.app.state.admin.live_history[0]
    e = m.app.state.admin.suggestions.get(record.id)
    approved = await m.http.post(f"/api/admin/suggestions/{record.id}/approve",
        json={"expected_suggestion_revision":e.suggestion_revision})
    assert approved.status_code == 200
    approval = m.app.state.admin.suggestions.approvals[record.id]
    # Seed a filled journal row: physical typing belongs to the operator, not this test.
    operation, reserved = m.service.journal.reserve(request(e, approval, key="manual_verified_challenge"))
    assert reserved
    m.service.journal.finish(operation.operation_id, DeliveryResult(status="filled",reason="filled_waiting_user_send"))
    body = ManualDeliveryInput(suggestion_id=record.id, approval_id=approval.approval_id,
        expected_suggestion_revision=e.suggestion_revision,idempotency_key="manual_verified_challenge")
    target = AndroidTarget(account=OWN,live_session_id=m.runtime.live.session_id,device_serial="903ed867")
    m.service.pending_fill = (body, target, host_feed(capture("live_viewer")), e.suggested_reply, operation.operation_id)
    m.phone.snapshots = [posted(e.suggested_reply)]
    result = await m.http.post("/api/admin/delivery/verify-fill", json={})
    assert result.status_code == 200 and result.json()["status"] == "confirmed"
    assert m.service.account_proof.manual_accepted and m.service.mode == "MANUAL"
    assert m.service.journal.lookup("manual_verified_challenge").status == "confirmed"
    await m.runtime.live.disconnect(m.runtime.live.session_id)
    assert not m.service.account_proof.manual_accepted



def test_challenge_ui_expires_without_reload_and_never_submits_browser_token():
    node = shutil.which("node"); assert node
    source = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const els=new Map(),calls=[]; let expiry;
const el=()=>({value:'',textContent:'',append(){},replaceChildren(){},addEventListener(name,fn){this[name]=fn;}});
const document={getElementById(id){if(!els.has(id))els.set(id,el());return els.get(id)}};
const window={setTimeout(fn){expiry=fn;return 1},clearTimeout(){}};
const base={transport:'android',inspection:{devices:[]},mode:'MANUAL',account_challenge:{state:'EXPIRED',available:true}};
vm.runInNewContext(fs.readFileSync('backend/app/static/admin/delivery.js','utf8'),{window,document});
const controls=window.createDeliveryControls({api:async(p,b)=>{calls.push([p,b]);return p.endsWith('/history')?[]:base},node:el,toast(){},state:{connected:true},refreshCards(){}});
controls.render({...base,account_challenge:{state:'WAITING',token:'MOB-SYNTHETIC',expires_in_seconds:1,available:false}});
assert(els.get('mobile-challenge-note').textContent.includes('MOB-SYNTHETIC'));
assert(els.get('live-account-challenge').disabled);
(async()=>{await expiry();await new Promise(r=>setImmediate(r));
assert(!els.get('mobile-challenge-note').textContent.includes('MOB-SYNTHETIC'));
assert(!els.get('live-account-challenge').disabled);
await els.get('live-account-challenge').click();
const issued=calls.find(c=>c[0].endsWith('/live-account-challenge'));assert(issued);assert.deepStrictEqual(Object.keys(issued[1]),[]);
controls.render({transport:'windows_live_studio',windows_sender:{}});
assert(els.get('live-account-challenge').hidden && els.get('mobile-challenge-note').hidden);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node,"-e",source],capture_output=True,text=True,encoding="utf-8",timeout=15)
    assert result.returncode == 0, result.stderr
