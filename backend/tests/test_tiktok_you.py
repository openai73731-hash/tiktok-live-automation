"""Observed MIUI client formats and explicit local capture; all devices are mocks."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import shutil
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.android.adb import AdbCommand, AdbTransport, DUMP_PATH
from app.android.controller import ADBAndroidController
from app.android.diagnostics import CaptureError
from app.android.models import ClientVariant, DeviceInspection, ScreenState
from app.android.tiktok_ui import ScreenProfile, foreground, observed_variant, package_version
from app.admin.events import sse_message
from test_predelivery_hardening import panel
from test_operator_runtime import MockLive, post_action, BASE

PACKAGE = "com.zhiliaoapp.musically"
ACTIVITY = "com.windukk.mod.icon.default"
COMPONENT = f"ActivityRecord{{588bfc5 u0 {PACKAGE}/{ACTIVITY}}} t197}}"
TOP = "topResumedActivity=" + COMPONENT
BARE = "ResumedActivity: " + COMPONENT
USER = "@mitchelljoneswn0632256"
PRIVATE = "PRIVATE_HIERARCHY_VIEWER_COMMENT"
XML = f'<hierarchy><node package="{PACKAGE}" text="{PRIVATE}" class="android.widget.EditText"/></hierarchy>'


@pytest.fixture(autouse=True)
def isolated_device(monkeypatch, tmp_path):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=AssertionError("Real ADB forbidden")))
    monkeypatch.setattr("app.android.diagnostics.CAPTURE_ROOT", tmp_path / "diagnostics" / "android")


@pytest.mark.parametrize("output", [TOP, BARE, TOP + "\n" + BARE, BARE + "\n" + TOP])
def test_exact_observed_miui_tiktok_you_fixture(output):
    assert foreground(output) == (PACKAGE, ACTIVITY)


@pytest.mark.parametrize("marker", ["topResumedActivity", "mResumedActivity", "ResumedActivity"])
@pytest.mark.parametrize("separator", [":", "="])
def test_task_suffix_supports_all_resumed_markers(marker, separator):
    assert foreground(f"\t{marker} \t{separator}\t {COMPONENT} \r\n") == (PACKAGE, ACTIVITY)


@pytest.mark.parametrize("suffix", [" history", " t197", " t-1}", " t197} arbitrary", " t197} }",
    " t197} other.package/.Activity", " t197}\nACTIVITY other.package/.History", "\nt197}"])
def test_task_suffix_is_not_an_arbitrary_trailing_text_escape(suffix):
    output = f"topResumedActivity=ActivityRecord{{abc u0 {PACKAGE}/{ACTIVITY}}}" + suffix
    # A valid complete first line remains authoritative; later history is ignored.
    expected = (PACKAGE, ACTIVITY) if suffix.startswith("\n") or suffix == " t197}\nACTIVITY other.package/.History" else (None, None)
    assert foreground(output) == expected


def test_task_suffix_priority_over_conflicting_lower_marker_history():
    lower = "ResumedActivity: ActivityRecord{abc u0 com.miui.global.packageinstaller/.Scan} t198}"
    assert foreground(lower + "\n" + TOP) == (PACKAGE, ACTIVITY)
    assert foreground(TOP.replace("t197}", "tBAD}") + "\n" + lower) == (None, None)


@pytest.mark.parametrize("package,activity,expected", [(PACKAGE, ACTIVITY, ClientVariant.TIKTOK_YOU),
    (PACKAGE, "com.ss.android.ugc.aweme.splash.SplashActivity", ClientVariant.UNKNOWN),
    ("another.package", ACTIVITY, ClientVariant.UNKNOWN), (PACKAGE, ACTIVITY + "Extra", ClientVariant.UNKNOWN)])
def test_variant_is_exact_observed_hint_not_package_or_namespace_proof(package, activity, expected):
    assert observed_variant(package, activity) == expected


@pytest.mark.parametrize("info,expected", [("versionName=47.0.3 versionCode=2024700030 minSdk=23", ("47.0.3", "2024700030")),
    ("versionName=unsafe/<body> versionCode=oops", (None, None)),
    ("versionName=47.0.3 versionName=other", (None, None))])
def test_only_bounded_version_metadata_is_projected(info, expected):
    assert package_version(info) == expected


class CaptureTransport:
    executable = Path("fixture/adb.exe")

    def __init__(self):
        self.calls = []
        self.foregrounds = iter((TOP, BARE, TOP))
        self.info = "versionName=47.0.3 versionCode=2024700030"
        self.model = "M2101K9G"
        self.hook = None

    async def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.hook: await self.hook(command)
        if command == AdbCommand.FOREGROUND: return next(self.foregrounds)
        return {AdbCommand.DEVICES: "List of devices attached\nUSB_FIXTURE device\n",
            AdbCommand.PACKAGES: "package:" + PACKAGE, AdbCommand.DUMP: "dump: " + DUMP_PATH,
            AdbCommand.HIERARCHY: XML, AdbCommand.PACKAGE_INFO: self.info,
            AdbCommand.DEVICE_MODEL: self.model, AdbCommand.ANDROID_VERSION: "13"}[command]

    async def aclose(self): pass


async def test_you_inspection_reports_client_version_but_no_verified_ui():
    controller = ADBAndroidController(CaptureTransport())
    state = await controller.inspect()
    assert state.package == PACKAGE and state.foreground_activity == ACTIVITY
    assert state.version == "47.0.3" and state.version_code == "2024700030"
    assert state.client_variant == ClientVariant.TIKTOK_YOU
    assert state.adb == "AVAILABLE" and state.device == "CONNECTED"
    assert state.screen == ScreenState.UNKNOWN_SCREEN and state.reason == "profile_unverified"
    assert not any((state.profile_verified, state.account_verified, state.live_verified, state.comment_input_found,
                    state.send_button_found, state.ready, controller.physical_enabled))


async def test_official_profile_cannot_verify_you_with_same_package_version():
    official = ScreenProfile(PACKAGE, "47.0.3", "fixture:i", "fixture:s", "fixture:a", "fixture:l", True,
        ClientVariant.OFFICIAL, (ACTIVITY,))
    state = await ADBAndroidController(CaptureTransport(), profiles=(official,)).inspect()
    assert state.client_variant == ClientVariant.TIKTOK_YOU and not state.profile_verified


async def test_official_label_requires_explicit_reviewed_activity_binding():
    activity = "com.ss.android.ugc.aweme.splash.SplashActivity"
    transport = CaptureTransport()
    transport.foregrounds = iter((TOP.replace(ACTIVITY, activity), BARE.replace(ACTIVITY, activity)))
    profile = ScreenProfile(PACKAGE, "47.0.3", "fixture:i", "fixture:s", "fixture:a", "fixture:l", True,
        ClientVariant.OFFICIAL, (activity,))
    state = await ADBAndroidController(transport, profiles=(profile,)).inspect()
    assert state.client_variant == ClientVariant.OFFICIAL
    assert not state.ready and not state.account_verified


async def test_capture_writes_raw_only_locally_and_hashes_serial(tmp_path):
    transport = CaptureTransport()
    controller = ADBAndroidController(transport)
    result = await controller.capture()
    directory = tmp_path / "diagnostics" / "android" / result.capture_id
    assert (directory / "hierarchy.xml").read_text(encoding="utf-8") == XML
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["serial_sha256"] == sha256(b"USB_FIXTURE").hexdigest()
    assert "USB_FIXTURE" not in json.dumps(metadata) and PRIVATE not in json.dumps(metadata)
    assert metadata["device_model"] == "M2101K9G" and metadata["android_version"] == "13"
    assert metadata["version"] == "47.0.3" and metadata["version_code"] == "2024700030"
    assert metadata["package"] == PACKAGE and metadata["foreground_activity"] == ACTIVITY
    assert metadata["client_variant"] == "TIKTOK_YOU" and not metadata["profile_verified"]
    assert metadata["timestamp"].endswith("+00:00")
    assert metadata["hierarchy_sha256"] == sha256(XML.encode()).hexdigest()
    assert set(result.model_dump()) == {"status", "capture_id"}
    assert [c for c, _ in transport.calls] == [AdbCommand.DEVICES, AdbCommand.PACKAGES, AdbCommand.FOREGROUND,
        AdbCommand.DUMP, AdbCommand.HIERARCHY, AdbCommand.PACKAGE_INFO, AdbCommand.FOREGROUND,
        AdbCommand.DEVICE_MODEL, AdbCommand.ANDROID_VERSION, AdbCommand.PACKAGE_INFO, AdbCommand.FOREGROUND]
    assert all(kwargs.get("serial") == "USB_FIXTURE" for c, kwargs in transport.calls if c != AdbCommand.DEVICES)
    assert not controller.physical_enabled
    assert not controller.last.ready and controller._profile is None
    assert not any(c in {AdbCommand.TEXT, AdbCommand.DELETE, AdbCommand.ACTIVATE} for c, _ in transport.calls)


@pytest.mark.parametrize("change", ["foreground", "version", "model"])
async def test_capture_changed_or_unsafe_metadata_never_writes(change, tmp_path):
    transport = CaptureTransport()
    if change == "foreground": transport.foregrounds = iter((TOP, BARE, "ResumedActivity=null"))
    if change == "model": transport.model = "unsafe\n" + PRIVATE
    if change == "version":
        async def hook(command):
            if command == AdbCommand.DEVICE_MODEL: transport.info = "versionName=other"
        transport.hook = hook
    with pytest.raises(CaptureError): await ADBAndroidController(transport).capture()
    assert not (tmp_path / "diagnostics").exists()


async def test_capture_cancellation_stops_owned_work_without_writing(tmp_path):
    transport = CaptureTransport()
    entered = asyncio.Event()
    async def hook(command):
        if command == AdbCommand.DEVICE_MODEL:
            entered.set()
            await asyncio.Event().wait()
    transport.hook = hook
    controller = ADBAndroidController(transport)
    task = asyncio.create_task(controller.capture())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not controller.lock.locked() and not (tmp_path / "diagnostics").exists()


async def test_capture_capacity_fails_closed_without_eviction(tmp_path):
    root = tmp_path / "diagnostics" / "android"
    root.mkdir(parents=True)
    for number in range(20): (root / str(number)).mkdir()
    with pytest.raises(CaptureError): await ADBAndroidController(CaptureTransport()).capture()
    assert len(list(root.iterdir())) == 20


@pytest.mark.parametrize("mode,attributes", [(stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)])
async def test_capture_rejects_symlink_or_windows_reparse_directory(tmp_path, monkeypatch, mode, attributes):
    root = tmp_path / "diagnostics" / "android"
    original = Path.lstat
    def lstat(path):
        if path == root: return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
        return original(path)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(CaptureError): await ADBAndroidController(CaptureTransport()).capture()
    assert not (tmp_path / "diagnostics").exists()


async def test_capture_failed_write_removes_only_new_partial_capture(tmp_path, monkeypatch):
    original = Path.open
    def open_file(path, *args, **kwargs):
        if path.name == "metadata.json": raise OSError(PRIVATE)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(CaptureError): await ADBAndroidController(CaptureTransport()).capture()
    assert list((tmp_path / "diagnostics" / "android").iterdir()) == []


@pytest.mark.parametrize("command,tail", [(AdbCommand.DEVICE_MODEL, ["shell", "getprop", "ro.product.model"]),
    (AdbCommand.ANDROID_VERSION, ["shell", "getprop", "ro.build.version.release"])])
def test_capture_metadata_adb_commands_are_fixed_allowlist(tmp_path, command, tail):
    path = tmp_path / "adb.exe"; path.touch()
    argv = AdbTransport(path).argv(command, serial="USB_FIXTURE")
    assert argv == [str(path.resolve()), "-H", "127.0.0.1", "-P", "5037", "-s", "USB_FIXTURE", *tail]


async def test_capture_api_does_not_expose_raw_in_status_sse_logs_or_serve_files(panel, tmp_path, caplog):
    app, client = panel
    app.state.delivery.controller = ADBAndroidController(CaptureTransport())
    subscriber = app.state.admin.events.subscribe()
    result = await client.post("/api/admin/device/capture", json={})
    assert result.status_code == 200 and result.json()["status"] == "captured"
    for path in ["/api/admin/status", "/api/admin/delivery/status", "/api/admin/delivery/history", "/api/admin/operator/settings"]:
        response = await client.get(path)
        assert response.status_code == 200 and PRIVATE not in response.text and "<hierarchy" not in response.text
    assert PRIVATE not in result.text and PRIVATE not in caplog.text
    while not subscriber.queue.empty():
        event = subscriber.queue.get_nowait()
        assert PRIVATE not in event.encode() and "<hierarchy" not in event.encode()
    assert PRIVATE not in sse_message("snapshot", app.state.admin.status().model_dump(mode="json"))
    app.state.admin.events.unsubscribe(subscriber)
    assert (await client.get("/diagnostics/android/" + result.json()["capture_id"] + "/hierarchy.xml")).status_code == 404
    assert (await client.get("/api/admin/device/capture")).status_code == 405
    assert app.state.delivery.journal is None and app.state.operator_runtime.live.service is None


@pytest.mark.parametrize("fail", [False, True])
async def test_capture_publishes_unlocked_connect_state_after_observed_busy(panel, fail):
    app, client = panel
    rt = app.state.operator_runtime
    await post_action(client, BASE + "settings", json={"tiktok": {"username": USER}})
    await post_action(client, BASE + "apply", json={})
    transport = CaptureTransport()
    entered, release = asyncio.Event(), asyncio.Event()
    async def hook(command):
        if command == AdbCommand.DEVICE_MODEL:
            entered.set(); await release.wait()
            if fail: transport.model = "invalid\nmodel"
    transport.hook = hook
    app.state.delivery.controller = ADBAndroidController(transport)
    subscriber = app.state.admin.events.subscribe()
    task = asyncio.create_task(client.post("/api/admin/device/capture", json={}))
    await entered.wait()
    locked = (await client.get(BASE + "settings")).json()
    assert locked["locked"] and not locked["connect_allowed"] and locked["connect_block_reason"]
    release.set()
    assert (await task).status_code == (409 if fail else 200)
    event = subscriber.queue.get_nowait()
    assert event.name == "operator_settings"
    final = json.loads(event.data)
    assert not final["locked"] and final["connect_allowed"] and final["connect_block_reason"] is None
    app.state.admin.events.unsubscribe(subscriber)


async def test_service_shutdown_cancels_only_owned_capture(panel, tmp_path):
    app, client = panel
    transport = CaptureTransport()
    entered = asyncio.Event()
    async def hook(command):
        if command == AdbCommand.DEVICE_MODEL:
            entered.set(); await asyncio.Event().wait()
    transport.hook = hook
    app.state.delivery.controller = ADBAndroidController(transport)
    unrelated = asyncio.create_task(asyncio.Event().wait())
    task = asyncio.create_task(app.state.delivery.capture())
    try:
        await entered.wait()
        await app.state.delivery.aclose()
        with pytest.raises(asyncio.CancelledError): await task
        assert not unrelated.done() and not app.state.delivery.tasks
        assert not app.state.delivery.controller.lock.locked()
        assert not (tmp_path / "diagnostics").exists()
    finally:
        unrelated.cancel(); await asyncio.gather(unrelated, return_exceptions=True)


@pytest.mark.parametrize("headers,body,status", [({"X-Admin-CSRF": "wrong"}, {}, 403),
    ({"Origin": "https://remote.example"}, {}, 403), ({"Host": "remote.example"}, {}, 403),
    ({"Content-Type": "text/plain"}, {}, 415), ({}, {"path": "external.xml"}, 422),
    ({}, {"serial": "another"}, 422)])
async def test_capture_api_local_guards_and_no_browser_paths(panel, headers, body, status, tmp_path):
    app, client = panel
    transport = CaptureTransport(); app.state.delivery.controller = ADBAndroidController(transport)
    assert (await client.post("/api/admin/device/capture", headers=headers, json=body)).status_code == status
    assert not transport.calls and not (tmp_path / "diagnostics").exists()


@pytest.mark.parametrize("variant", [ClientVariant.TIKTOK_YOU, ClientVariant.UNKNOWN])
async def test_username_save_apply_connect_independent_of_android_not_ready(panel, variant):
    app, client = panel
    MockLive.instances = []
    app.state.operator_runtime.live.factory = MockLive
    saved = await post_action(client, BASE + "settings", json={"tiktok": {"username": USER},
        "ai": {"provider": "lmstudio", "model": "qwen/qwen3-8b"}})
    assert saved.status_code == 200 and saved.json()["pending"] and not saved.json()["connect_allowed"]
    applied = await post_action(client, BASE + "apply", json={})
    assert applied.status_code == 200 and applied.json()["username"] == USER[1:]
    assert applied.json()["connect_allowed"] and applied.json()["connect_block_reason"] is None
    app.state.delivery.controller.last = DeviceInspection(adb="AVAILABLE", device="CONNECTED", package=PACKAGE,
        foreground_activity=ACTIVITY, version="47.0.3", client_variant=variant, screen=ScreenState.UNKNOWN_SCREEN,
        reason="profile_unverified")
    assert not app.state.delivery.controller.last.ready
    assert (await client.get(BASE + "settings")).json()["connect_allowed"]
    connected = await client.post("/api/admin/live/connect", json={"username": USER})
    assert connected.status_code == 202
    await MockLive.instances[0].started.wait()
    assert MockLive.instances[0].settings.tiktok_username == USER[1:]
    assert app.state.comment_processor.engine.name == "lmstudio"
    assert not app.state.delivery.settings.enable_physical_tiktok_send and app.state.delivery.journal is None


def test_operator_and_capture_browser_regressions():
    node = shutil.which("node")
    if not node: pytest.skip("Optional local Node runtime")
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),crypto=require('crypto');
const els=new Map();const el=()=>({value:'',options:[],children:[],listeners:{},append(...v){this.children.push(...v)},replaceChildren(...v){this.children=v},addEventListener(k,f){this.listeners[k]=f}});
const connect=[el(),el()],disconnect=[el(),el()],reasons=[el(),el()];
const document={createElement:el,getElementById(id){if(!els.has(id))els.set(id,el());return els.get(id)},querySelectorAll(s){return s==='[data-live-action="connect"]'?connect:s==='[data-live-action="disconnect"]'?disconnect:s==='[data-live-connect-reason]'?reasons:[]}};
const window={};const state={connected:true,status:{live:{connection_owned:false,state:'disconnected'}},delivery:{inspection:{client_variant:'TIKTOK_YOU',screen:'UNKNOWN_SCREEN'},physical_ready:false}};
vm.runInNewContext(fs.readFileSync('backend/app/static/admin/operator.js','utf8'),{window,document});
vm.runInNewContext(fs.readFileSync('backend/app/static/admin/delivery.js','utf8'),{window,document,crypto});
const desired={provider:'lmstudio',model:'qwen/qwen3-8b',temperature:0,timeout_seconds:10,reply_policy:'focused',max_reply_sentences:2,max_reply_chars:180};
let view={desired,active:desired,saved:{android:{device_serial:null}},saved_username:'',pending:false,locked:false,saved_config_revision:'a'.repeat(32),active_runtime_revision:'b'.repeat(32),connect_block_reason:'Укажите username.'};
const calls=[],captured='c'.repeat(32),status={inspection:{adb:'AVAILABLE',device:'CONNECTED',devices:[],client_variant:'TIKTOK_YOU',version:'47.0.3',package:'com.zhiliaoapp.musically',foreground_activity:'com.windukk.mod.icon.default',screen:'UNKNOWN_SCREEN',profile_verified:false},physical_delivery_enabled:false,physical_ready:false,delivery:'NOT READY',reason:'physical_delivery_disabled'};
const api=async(path,body)=>{calls.push([path,body]);if(path.endsWith('operator/settings')&&body){assert.equal(body.settings.tiktok.username,'@mitchelljoneswn0632256');view={...view,saved_username:'mitchelljoneswn0632256',pending:true,connect_block_reason:'Примените настройки.'};return view;}
if(path.endsWith('/apply')){view={...view,pending:false,connect_block_reason:null,connect_allowed:true};return view;}
if(path.endsWith('operator/settings'))return view;if(path==='/api/admin/status')return state.status;if(path.endsWith('delivery/status')||path.endsWith('/device/inspect'))return status;if(path.endsWith('/history'))return [];if(path.endsWith('/capture')){assert.deepEqual(body,{});return {status:'captured',capture_id:captured}};throw Error(path);};
const operator=window.createOperatorControls({api,state,toast(){},renderSettings(){},renderStatus(){},refreshQuality:async()=>{}});
const get=id=>document.getElementById(id),tick=()=>new Promise(r=>setImmediate(r));
(async()=>{operator.render(view);get('operator-username').value='@mitchelljoneswn0632256';get('operator-username').listeners.input();assert(connect.every(b=>b.disabled));assert(reasons[0].textContent.includes('Сохраните'));
get('operator-form').listeners.submit({preventDefault(){}});await tick();assert(connect.every(b=>b.disabled));assert(reasons[0].textContent.includes('Примените'));
await get('operator-apply').listeners.click();assert(connect.every(b=>!b.disabled));assert(reasons[0].textContent.includes('Android readiness не требуется'));
operator.render({...view,connect_block_reason:'Очистка LIVE не завершена.'});assert(connect.every(b=>b.disabled));assert(reasons[0].textContent.includes('Очистка'));
state.connected=false;operator.controls();assert(reasons[0].textContent.includes('Нет связи'));state.connected=true;
const delivery=window.createDeliveryControls({api,node:(_,__,v)=>Object.assign(el(),{textContent:v}),toast(){},state,refreshCards(){}});await delivery.refresh();
await get('inspect-device').listeners.click();assert.strictEqual(get('capture-device').disabled,false);assert.strictEqual(get('inspect-device').disabled,false);
const text=get('device-status').children.map(r=>r.children.map(c=>c.textContent).join(':')).join('\n');assert(text.includes('TikTok You'));assert(text.includes('47.0.3'));assert(text.includes('UNVERIFIED'));assert(!text.includes('Official'));
await get('capture-device').listeners.click();assert(get('capture-result').textContent.includes(captured));assert(calls.some(([p])=>p.endsWith('/capture')));assert(!calls.some(([p])=>p.endsWith('/send')||p.endsWith('/live/connect')));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
