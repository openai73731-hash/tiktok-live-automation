"""Release candidate tests use sanitized captures and mock ADB only."""
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
import shutil
import subprocess
from unittest.mock import AsyncMock
from uuid import uuid4
import xml.etree.ElementTree as ET

import pytest

from app.android.adb import AdbCommand, AdbTransport, AdbError, DUMP_PATH, ascii_supported
from app.android.controller import ADBAndroidController
from app.android.tiktok_ui import TIKTOK_YOU_4683 as PROFILE, rid, inspect_hierarchy, unique_node
from app.android.models import ScreenState
from app.delivery.models import AndroidTarget, AndroidSubmission, ApprovalInput, ManualDeliveryInput
from app.delivery.service import ManualDeliveryService
from app.delivery.journal import SQLiteDeliveryOperationStore
from app.live.models import Comment, LiveState, LiveStatus
from test_predelivery_hardening import panel, suggestion, rehash, MockLive

FIXTURES = Path(__file__).parent / "fixtures" / "android"
ACCOUNT = "mitchelljoneswn0632256"
SERIAL = "903ed867"

def xml(name): return (FIXTURES / ("you_4683_" + name + ".xml")).read_text(encoding="utf-8")
def target(session="a"*32, account=ACCOUNT):
    return AndroidTarget(live_session_id=session, account=account, device_serial=SERIAL)
def request(text="One safe reply."):
    return AndroidSubmission(operation_id=uuid4().hex, target=target(), text=text,
        expires_at=datetime.now(UTC) + timedelta(minutes=5))

class Transport:
    executable = Path("fixture/adb.exe")
    def __init__(self):
        self.xml = xml("profile")
        self.calls = []
        self.info = "versionName=46.8.3\nversionCode=2024608030\nfirstInstallTime=2026-10-07 10:00:00\nlastUpdateTime=2026-10-07 10:00:00"
        self.serial = SERIAL
        self.mismatch = False
        self.after = None
        self.activations = 0
    async def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.after: await self.after(command)
        if kwargs.get("guard") and not kwargs["guard"](): raise AdbError()
        if command == AdbCommand.ACTIVATE: self.activations += 1
        root = ET.fromstring(self.xml)
        entry = next((n for n in root.iter() if n.get("resource-id") == rid("h4k")), None)
        if command == AdbCommand.DELETE: entry.set("text", "")
        elif command == AdbCommand.TEXT: entry.set("text", "corrupt" if self.mismatch else kwargs["text"])
        elif command == AdbCommand.TAB:
            focused = next(n for n in root.iter() if n.get("focused") == "true")
            order = [rid(n) for n in ("h4k", "qde", "qcv", "vp8")]
            name = order[(order.index(focused.get("resource-id"))+1) % len(order)]
            focused.set("focused", "false")
            next(n for n in root.iter() if n.get("resource-id") == name).set("focused", "true")
        self.xml = ET.tostring(root, encoding="unicode")
        values = {AdbCommand.DEVICES: "List of devices attached\n"+self.serial+" device\n",
            AdbCommand.PACKAGES: "package:"+PROFILE.package,
            AdbCommand.FOREGROUND: "mResumedActivity: ActivityRecord{abc u0 "+PROFILE.package+"/com.windukk.mod.icon.default t1}",
            AdbCommand.PACKAGE_INFO: self.info, AdbCommand.DUMP: DUMP_PATH, AdbCommand.HIERARCHY: self.xml,
            AdbCommand.DEVICE_MODEL: "M2101K9G", AdbCommand.ANDROID_VERSION: "13"}
        return values.get(command, "")
    async def aclose(self): pass

@pytest.fixture(autouse=True)
def forbid_process(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=AssertionError("No real ADB")))

async def prepared(enabled=True, dest=None):
    dest = dest or target()
    t = Transport(); c = ADBAndroidController(t, physical_enabled=enabled)
    assert await c.verify_account(dest)
    t.xml = xml("live_idle"); s = await c.inspect(dest); assert s.live_verified
    t.xml = xml("composer"); s = await c.inspect(dest)
    assert s.send_control_state == "SEND_CONTROL_CANDIDATE" and not s.ready
    assert await c.confirm_send(dest, s.send_confirmation_token)
    return c,t

@pytest.mark.parametrize("name", ["live_idle", "live_comments"])
def test_real_live_combination(name):
    state = inspect_hierarchy(xml(name), PROFILE.package, ACCOUNT, PROFILE, account_bound=True)
    assert state["live_verified"] and state["screen"] == ScreenState.LIVE_READY

@pytest.mark.parametrize("name", ["profile", "composer"])
def test_non_live_without_fresh_host_evidence(name):
    assert not inspect_hierarchy(xml(name), PROFILE.package, ACCOUNT, PROFILE)["live_verified"]

@pytest.mark.parametrize("marker", [x[0] for x in PROFILE.live_markers])
def test_missing_live_marker(marker):
    tree = ET.fromstring(xml("live_idle"))
    for parent in tree.iter():
        for child in list(parent):
            if child.get("resource-id") == marker: parent.remove(child)
    assert not inspect_hierarchy(ET.tostring(tree,encoding="unicode"), PROFILE.package, ACCOUNT, PROFILE)["live_verified"]

@pytest.mark.parametrize("change", ["missing", "duplicate", "class", "disabled", "not_focusable", "modal", "foreign"])
def test_composer_fail_closed(change):
    root = ET.fromstring(xml("composer")); n = next(n for n in root.iter() if n.get("resource-id") == rid("h4k"))
    if change == "missing": n.set("resource-id", rid("other"))
    elif change == "duplicate": root.append(ET.fromstring(ET.tostring(n)))
    elif change == "class": n.set("class", "android.widget.TextView")
    elif change == "disabled": n.set("enabled", "false")
    elif change == "not_focusable": n.set("focusable", "false")
    elif change == "modal": ET.SubElement(root,"node", {"class":"android.app.Dialog"})
    elif change == "foreign": ET.SubElement(root,"node", {"package":"other.app"})
    state = inspect_hierarchy(ET.tostring(root,encoding="unicode"),PROFILE.package,ACCOUNT,PROFILE,
        account_bound=True, live_bound=True, send_confirmed=True)
    assert state["reason"] != "ready"

async def test_account_exact_wrong_and_restart():
    t=Transport(); c=ADBAndroidController(t)
    assert not await c.verify_account(target(account="wrong"))
    assert await c.verify_account(target())
    assert c._account_binding
    restarted=ADBAndroidController(t)
    t.xml=xml("live_idle")
    assert not (await restarted.inspect(target())).account_verified


async def test_same_handle_without_owner_control_is_not_trusted():
    t=Transport(); t.xml=t.xml.replace("Добавить описание", "Follow")
    c=ADBAndroidController(t)
    assert not await c.verify_account(target())


async def test_empty_field_can_disable_send_until_text_is_entered():
    c,t=await prepared(); original=t.run
    async def run(command, **kwargs):
        value=await original(command, **kwargs)
        if command in {AdbCommand.DELETE, AdbCommand.TEXT}:
            root=ET.fromstring(t.xml)
            for n in root.iter():
                if n.get("resource-id") in {rid("vp8"),rid("vpl")}:
                    n.set("enabled", "false" if command==AdbCommand.DELETE else "true")
            t.xml=ET.tostring(root,encoding="unicode")
        return value
    t.run=run
    assert (await c.submit(request(),guard=lambda:True)).status=="unknown"
    assert t.activations==1


async def test_live_proof_does_not_enable_new_session_button():
    c,t=await prepared()
    assert c.ready_for("a"*32,ACCOUNT)
    assert not c.ready_for("b"*32,ACCOUNT)
    assert not c.ready_for("a"*32,"other")

@pytest.mark.parametrize("change", ["version", "code", "reinstall", "device", "account", "package", "activity"])
async def test_binding_change_invalidates(change):
    c,t=await prepared()
    dest=target()
    if change=="version": t.info=t.info.replace("46.8.3","46.8.4")
    elif change=="code": t.info=t.info.replace("2024608030","2024608031")
    elif change=="reinstall": t.info=t.info.replace("10:00:00","11:00:00")
    elif change=="device": t.serial="other"
    elif change=="account": dest=target(account="other")
    elif change in {"package","activity"}:
        original=t.run
        async def run(command, **kwargs):
            value=await original(command, **kwargs)
            if command==AdbCommand.FOREGROUND:
                value=value.replace(PROFILE.package,"other.app") if change=="package" else value.replace("com.windukk.mod.icon.default",".Other")
            return value
        t.run=run
    state=await c.inspect(dest)
    assert not state.ready and not c.current_ready

async def test_candidate_token_only_exact_backend_candidate():
    c,t=await prepared(); c._send_binding=None
    state=await c.inspect(target()); token=state.send_confirmation_token
    assert not await c.confirm_send(target(), "0"*32)
    assert await c.confirm_send(target(), token)
    assert not await c.confirm_send(target(), token)

@pytest.mark.parametrize("text", ["Hello world", "One, two: three; yes!", "It's automatic (approved).", "A1 B2?", "  spaces  "])
async def test_ascii_exact_old_text_clear_spaces_punctuation(text):
    c,t=await prepared()
    outcome=await c.submit(request(text), guard=lambda: True)
    assert outcome.status=="unknown"
    commands=[x[0] for x in t.calls]
    assert commands.count(AdbCommand.SELECT_ALL)==commands.count(AdbCommand.DELETE)==commands.count(AdbCommand.TEXT)==commands.count(AdbCommand.ACTIVATE)==1
    assert commands.index(AdbCommand.DELETE)<commands.index(AdbCommand.TEXT)<commands.index(AdbCommand.ACTIVATE)
    assert unique_node(t.xml,rid("h4k")).get("text")==text

@pytest.mark.parametrize("text", ["Привет", "café", "你好", "100% sure", "a\nb", "x$HOME", "x`id`", "", "\u200b"])
async def test_unsupported_input_never_types_or_sends(text):
    assert not ascii_supported(text)
    if not text: return
    c,t=await prepared()
    outcome=await c.submit(request(text), guard=lambda: True)
    assert outcome.reason=="unicode_entry_unavailable"
    assert not any(cmd in {AdbCommand.TEXT,AdbCommand.DELETE,AdbCommand.ACTIVATE} for cmd,_ in t.calls)

async def test_mismatch_clears_safely_no_send():
    c,t=await prepared(); t.mismatch=True
    outcome=await c.submit(request(),guard=lambda:True)
    assert outcome.status=="needs_operator"
    assert unique_node(t.xml,rid("h4k")).get("text")==""
    assert not any(cmd==AdbCommand.ACTIVATE for cmd,_ in t.calls)

@pytest.mark.parametrize("change", ["gate", "focus", "security", "stale_guard", "no_focus_route", "lease", "session"])
async def test_last_checks_no_send(change, monkeypatch):
    c,t=await prepared(enabled=change!="gate")
    dest=request()
    if change=="focus": t.xml=t.xml.replace('focused="true"','focused="false"')
    elif change=="security": t.xml=t.xml.replace('text="Test"','text="security check"')
    elif change=="lease": monkeypatch.setattr("app.android.controller.monotonic", lambda: c._live_evidence[3]+91)
    elif change=="session": dest=dest.model_copy(update={"target":target("b"*32)})
    elif change=="no_focus_route":
        original=t.run
        async def run(command,**kwargs):
            if command==AdbCommand.TAB: return ""
            return await original(command,**kwargs)
        t.run=run
    outcome=await c.submit(dest,guard=lambda:change!="stale_guard")
    assert outcome.status!="unknown"
    assert not any(cmd==AdbCommand.ACTIVATE for cmd,_ in t.calls)

def test_text_argv_device_shell_quote(tmp_path):
    adb=tmp_path/"adb.exe"; adb.touch(); t=AdbTransport(adb)
    import shlex
    value="It's safe; really! Yes (ok)."
    command=t.argv(AdbCommand.TEXT,serial=SERIAL,text=value)
    assert command[-3:-1]==["input","text"]
    assert shlex.split(command[-1])==[value.replace(" ","%s")]
    for bad in ("$HOME", "a\nb", "100%s", "\\", "Привет"):
        with pytest.raises(AdbError): t.argv(AdbCommand.TEXT,serial=SERIAL,text=bad)

async def test_browser_trust_routes_csrf_and_no_arbitrary_selector(panel):
    app,client=panel
    assert (await client.post("/api/admin/device/confirm-send",json={"token":"a"*32,"confirmed":True,"resource_id":"arbitrary"})).status_code==422
    assert (await client.post("/api/admin/device/verify-account",json={},headers={"X-Admin-CSRF":"wrong"})).status_code==403
    assert not app.state.delivery.settings.enable_physical_tiktok_send
    assert (await client.post("/api/admin/device/confirm-send",json={"token":"a"*32,"confirmed":False})).status_code==422

async def test_live_current_history_sse_dedup_archive_android_independent(panel):
    app,client=panel; admin=app.state.admin; runtime=admin.runtime; processor=admin.playground.processor
    sub=admin.events.subscribe()
    def select(s):
        runtime.live.session_id=s; runtime.live.session_invalidated=False; admin.begin_live_session(s)
    select("a"*32)
    for i in range(3): await admin.handle_live(Comment(username="viewer",text="Hello!",message_id=str(i+1)),processor,mode="real",session_id="a"*32)
    assert len(admin.current_live_history())==3
    runtime.live.session_invalidated=True; runtime.live.session_id=None
    assert not (await client.get("/api/admin/live/history")).json()
    select("b"*32)
    comment=Comment(username="viewer",text="Hello!",message_id="4")
    await admin.handle_live(comment,processor,mode="real",session_id="b"*32)
    await admin.handle_live(comment,processor,mode="real",session_id="b"*32)
    await admin.handle_live(Comment(username="viewer",text="Hello!",message_id="5"),processor,mode="real",session_id="a"*32)
    history=(await client.get("/api/admin/live/history")).json()
    assert len(history)==1 and history[0]["live_session_id"]=="b"*32
    assert len(admin.live_archive)==3
    assert admin.live_view().current_comment_count==1 and admin.live_view().last_comment_at==comment.timestamp
    events=[]
    while not sub.queue.empty(): events.append(sub.queue.get_nowait())
    comments=[json.loads(e.data) for e in events if e.name=="comment_received"]
    assert len(comments)==4 and comments[-1]["comment_identity"]==history[0]["comment_identity"]
    assert not app.state.delivery.controller.last.ready


async def test_actual_connect_disconnect_rotates_current_history(panel):
    app,client=panel; runtime=app.state.operator_runtime; admin=app.state.admin
    runtime.live.factory=MockLive
    a=(await client.post("/api/admin/live/connect",json={"username":"own"})).json()["live_session_id"]
    await runtime.live.service.started.wait()
    old_callback=runtime.live.service.on_comment
    for i in range(3): await old_callback(Comment(username="viewer",text="Hello!",message_id=str(i+1)))
    assert len((await client.get("/api/admin/live/history")).json())==3
    assert (await client.post("/api/admin/live/disconnect",json={"expected_live_session_id":a})).status_code==200
    b=(await client.post("/api/admin/live/connect",json={"username":"own"})).json()["live_session_id"]
    await runtime.live.service.started.wait()
    await old_callback(Comment(username="viewer",text="old",message_id="99"))
    await runtime.live.service.on_comment(Comment(username="viewer",text="Hello!",message_id="4"))
    rows=(await client.get("/api/admin/live/history")).json()
    assert len(rows)==1 and rows[0]["live_session_id"]==b and len(admin.live_archive)==3

async def test_live_pending_history_and_stale_ai_completion(panel):
    app,client=panel; admin=app.state.admin; runtime=admin.runtime; processor=admin.playground.processor
    runtime.live.session_id="a"*32; admin.begin_live_session("a"*32)
    entered,release=asyncio.Event(),asyncio.Event(); original=processor.handle
    async def delayed(comment):
        entered.set(); await release.wait(); return await original(comment)
    processor.handle=delayed
    task=asyncio.create_task(admin.handle_live(Comment(username="viewer",text="Hello!",message_id="11"),processor,mode="real",session_id="a"*32))
    await entered.wait()
    pending=(await client.get("/api/admin/live/history")).json()
    assert len(pending)==1 and pending[0]["text"]=="Hello!"
    runtime.live.session_id="b"*32; admin.begin_live_session("b"*32); release.set(); await task
    assert not admin.current_live_history() and admin.current_comment_count==0

def test_current_session_browser_rehydrate_stale_event():
    node=shutil.which("node"); assert node, "Node required for browser regression"
    script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'); const ctx={};
vm.runInNewContext(fs.readFileSync('backend/app/static/admin/live-feed.js','utf8'),ctx);
const feed=ctx.createLiveFeed();feed.select('A');
const row=(id,s)=>({id,live_session_id:s,timestamp:'2026-10-07T12:00:00Z'});
[1,2,3].forEach(i=>feed.put(row(String(i),'A')));const old=feed.token();feed.select('B');feed.put(row('B1','B'));
assert(!feed.put(row('old','A')));feed.hydrate([row('1','A')],old);assert.equal(feed.rows.size,1);
feed.hydrate([row('B1','B')],feed.token());assert.equal(feed.rows.size,1);
feed.put({...row('B1','B'),suggestion:{status:'ready'}});feed.put(row('B1','B'));assert(feed.rows.get('B1').suggestion);
feed.select(null);assert.equal(feed.rows.size,0);
"""
    done=subprocess.run([node,"-e",script],capture_output=True,text=True,timeout=10)
    assert done.returncode==0, done.stderr


@pytest.mark.parametrize("revoke", [None,"approval","expiry","session","runtime","reservation","text"])
async def test_production_ascii_coordinator_final_binding_and_unknown_no_retry(suggestion,settings,tmp_path,revoke):
    r=suggestion
    r.context=r.context.model_copy(update={"target_account":ACCOUNT})
    e=rehash(r.e,target_account=ACCOUNT)
    r.store.groups["live"][e.suggestion_id]=e
    r.store.approvals.clear()
    approval=r.store.approve(e.suggestion_id,ApprovalInput(expected_suggestion_revision=e.suggestion_revision),r.context)
    c,t=await prepared(dest=target(e.live_session_id))
    journal=SQLiteDeliveryOperationStore(tmp_path/"operations.db")
    settings=settings.model_copy(update={"enable_physical_tiktok_send":True})
    service=ManualDeliveryService(settings,r.store,lambda:r.context,controller=c,journal_factory=lambda:journal)
    body=ManualDeliveryInput(suggestion_id=e.suggestion_id,expected_suggestion_revision=e.suggestion_revision,
        approval_id=approval.approval_id,idempotency_key="rc_test")
    mutated=False
    async def after(command):
        nonlocal mutated
        if command!=AdbCommand.TAB or mutated or not revoke:return
        mutated=True
        if revoke=="approval":r.store.approvals.clear()
        elif revoke=="expiry":e._monotonic_deadline=0
        elif revoke=="session":r.context=r.context.model_copy(update={"live_session_id":"d"*32})
        elif revoke=="runtime":r.context=r.context.model_copy(update={"active_runtime_revision":"d"*32})
        elif revoke=="reservation":journal.connection.execute("UPDATE operations SET status='not_sent',reason='rejected'")
        elif revoke=="text":t.xml=t.xml.replace(e.suggested_reply,"changed")
    t.after=after
    try:
        result=await service.send(body)
        entered=[kwargs["text"] for cmd,kwargs in t.calls if cmd==AdbCommand.TEXT]
        assert entered==[e.suggested_reply]
        if revoke:
            assert t.activations==0 and result.status in {"needs_operator","unknown","not_sent"}
        else:
            assert result.status=="unknown" and service.paused and t.activations==1
            assert await service.send(body)==result
            assert (await service.send(body.model_copy(update={"idempotency_key":"other"}))).status=="needs_operator"
            assert t.activations==1
    finally:await service.aclose()
