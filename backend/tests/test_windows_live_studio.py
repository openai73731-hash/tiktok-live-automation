"""Offline PC-SEND-1 contracts. Native account UI inspection is explicitly opt-in."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.delivery.factory import create_delivery_service
from app.main import create_app
from app.windows.probe import ProbeReport, cli, probe, save_report
from app.windows.sender import WindowsLiveStudioReplySender

pytestmark = [pytest.mark.windows, pytest.mark.live_studio]


def fake_tree(*, names=False, password=False):
    def node(identity, parent, kind):
        return dict(node_id=identity, parent_id=parent, window_id=1, depth=identity - 1,
            process_id=123, native_handle=100 if identity == 1 else 0, control_type=kind,
            automation_id="synthetic", name="PRIVATE NAME" if names else None,
            class_name="SyntheticControl", enabled=True, focusable=kind == "Edit",
            focused=False, offscreen=False, password=password, bounds=[0, 0, 100, 30],
            supported_patterns=["ValuePatternIdentifiers.Pattern"] if kind == "Edit" else [],
            candidate_edit=kind == "Edit" and not password, candidate_button=False,
            candidate_chat_region=False)
    return dict(status="discovered", names_included=names, processes=[dict(process_id=123, name="SyntheticStudio")],
        windows=1, nodes=[node(1, None, "Window"), node(2, 1, "Edit")])


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr("app.windows.probe.sys.platform", "win32")
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(fake_tree()).encode(), stderr=b"")
    return run, calls


def test_probe_preserves_tree_and_uses_only_bundled_worker(worker):
    run, calls = worker
    report = probe(process_id=123, runner=run)
    assert report.status == "discovered" and report.nodes[1].parent_id == 1
    assert report.nodes[1].candidate_edit and report.nodes[1].name is None
    argv, options = calls[0]
    source = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert "-TargetProcessId 123" in source and "-IncludeNames:$false" in source
    assert "-ExecutionPolicy" not in argv and "shell" not in options
    assert options["timeout"] == 20 and options["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("limits", [{"max_nodes": 0}, {"max_depth": 21}, {"timeout": 61},
    {"process_id": -1}, {"tree_view": "x; command"}, {"timeout": float("nan")}])
def test_bad_limits_never_start_worker(worker, limits):
    run, calls = worker
    with pytest.raises(ValueError): probe(runner=run, **limits)
    assert calls == []


@pytest.mark.parametrize("failure", ["timeout", "missing", "malformed", "oversized", "exit"])
def test_probe_failure_is_static_and_has_no_ui_content(worker, failure):
    def run(*args, **kwargs):
        if failure == "timeout": raise subprocess.TimeoutExpired("PRIVATE", 1)
        if failure == "missing": raise OSError("PRIVATE")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0,
            stdout=b"x" * 2_000_001 if failure == "oversized" else b"PRIVATE", stderr=b"PRIVATE")
    report = probe(runner=run)
    assert report.status == ("timeout" if failure == "timeout" else "uia_unavailable")
    assert "PRIVATE" not in report.model_dump_json() and not report.nodes


def test_names_are_opt_in_and_password_names_are_rejected(worker):
    def run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(fake_tree(names=True)).encode())
    assert probe(runner=run).status == "uia_unavailable"
    assert probe(runner=run, include_names=True).nodes[1].name == "PRIVATE NAME"
    with pytest.raises(ValidationError): ProbeReport.model_validate(fake_tree(names=True, password=True))


@pytest.mark.parametrize("change", ["incomplete", "parent", "duplicate", "protected_parent"])
def test_inconsistent_tree_is_not_complete_discovery(change):
    tree = fake_tree()
    if change == "incomplete": tree["truncated"] = True
    elif change == "parent": tree["nodes"][1]["parent_id"] = 3
    elif change == "duplicate": tree["nodes"][1]["node_id"] = 1
    else: tree["nodes"][0]["password"] = True
    with pytest.raises(ValidationError): ProbeReport.model_validate(tree)


def test_non_windows_probe_never_spawns(monkeypatch):
    monkeypatch.setattr("app.windows.probe.sys.platform", "linux")
    assert probe(runner=lambda *a, **k: pytest.fail("Unexpected process")).status == "unsupported_platform"


def test_reports_are_bounded_and_names_never_reach_cli_stdout(tmp_path, monkeypatch, capsys):
    report = ProbeReport.model_validate(fake_tree(names=True))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("app.windows.probe.probe", lambda **kwargs: report)
    assert cli(["--include-names", "--instance-id", "vm1"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["instance_id"] == "vm1" and summary["physical_action"] is False
    assert "PRIVATE" not in json.dumps(summary)
    assert "PRIVATE NAME" in Path(summary["report"]).read_text(encoding="utf-8")
    for _ in range(19): save_report(report, "vm1")
    with pytest.raises(ValueError): save_report(report, "vm1")
    assert len(list((tmp_path / "diagnostics/windows/vm1").iterdir())) == 20
    with pytest.raises(ValueError): save_report(report, "../escape")


def test_report_parent_link_is_rejected(tmp_path, monkeypatch):
    directory = tmp_path / "diagnostics"
    directory.mkdir()
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path == directory or original(path))
    with pytest.raises(ValueError):
        save_report(ProbeReport(status="window_not_found"), "vm1", root=directory / "windows")


async def test_windows_sender_never_claims_prepare_or_send():
    sender = WindowsLiveStudioReplySender()
    assert not (await sender.healthcheck()).prepare_available
    assert not sender.get_status().submit_available
    for action in (sender.prepare, sender.send):
        assert (await action(None)).model_dump() == {"status": "not_sent", "reason": "not_configured"}


@pytest.mark.parametrize("field,value", [("tiktok_transport", "browser"), ("instance_id", "../vm1"),
    ("instance_id", "x" * 49), ("account_label", "hello\nworld")])
def test_transport_config_rejects_unknowns(field, value):
    with pytest.raises(ValidationError): Settings(_env_file=None, **{field: value})


def test_android_default_and_separate_state_paths(settings):
    from test_windows_sender import context
    service = create_delivery_service(settings, None, context)
    assert service.view().transport == "android" and service.journal is None
    assert Settings(_env_file=None, state_dir="work/vm1/data").state_dir == Path("work/vm1/data")


async def test_state_directory_isolates_lazy_legacy_journals(settings, tmp_path):
    from test_windows_sender import context
    for name in ("vm1", "vm2"):
        config = settings.model_copy(update={"state_dir": tmp_path / name, "enable_physical_tiktok_send": True})
        service = create_delivery_service(config, None, context)
        assert not config.state_dir.exists()
        service.prepare()
        assert (config.state_dir / "unified-delivery.sqlite3").exists()
        await service.aclose()


def test_runtime_logs_bind_startup_instance(settings, tmp_path):
    from app.logging_config import configure_logging
    logger = configure_logging(settings.model_copy(update={"log_dir": tmp_path, "instance_id": "vm1"}))
    logger.info("pc_contract", extra={"fields": {"instance_id": "untrusted"}})
    record = json.loads((tmp_path / "app.jsonl").read_text(encoding="utf-8"))
    assert record["instance_id"] == "vm1"


async def test_windows_web_has_no_adb_uia_db_listener_or_inference(settings, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs): pytest.fail("Unexpected I/O or platform construction")
    monkeypatch.setattr("app.delivery.service.AdbTransport", forbidden)
    monkeypatch.setattr("app.delivery.service.SQLiteDeliveryOperationStore", forbidden)
    monkeypatch.setattr("app.main.LiveService", forbidden)
    monkeypatch.setattr("app.windows.probe.probe", forbidden)
    config = settings.model_copy(update={"tiktok_transport": "windows_live_studio",
        "enable_physical_tiktok_send": True, "log_dir": tmp_path / "logs", "instance_id": "vm1"})
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            csrf = (await client.get("/api/admin/session")).json()["csrf_token"]
            client.headers["X-Admin-CSRF"] = csrf
            for route in ("delivery/status", "delivery/history", "settings", "status"):
                assert (await client.get("/api/admin/" + route)).status_code == 200
            view = (await client.get("/api/admin/delivery/status")).json()
            assert view["transport"] == "windows_live_studio" and view["inspection"] is None
            assert view["instance_id"] == "vm1" and not view["physical_delivery_enabled"]
            for route in ("device/inspect", "delivery/prepare"):
                assert (await client.post("/api/admin/" + route, json={})).json() == view
            body = dict(suggestion_id="a" * 32, expected_suggestion_revision="b" * 64,
                approval_id="c" * 32, idempotency_key="offline")
            assert (await client.post("/api/admin/delivery/send", json=body)).json()["reason"] == "not_configured"
            assert not (await client.post("/api/admin/delivery/dry-run", json=body)).json()["would_send"]
            assert (await client.post("/api/admin/device/capture", json={})).status_code == 409
            # Same production fake AI and immutable review path, still never deliverable.
            result = await client.post("/api/admin/playground/analyze", json={"text": "Is bot trading automatically?"})
            assert result.status_code == 200
            review = result.json()["delivery_review"]
            assert not review["physical_send_allowed"] and not review["text_entry_supported"]
            assert not app.state.admin.delivery_context().delivery_available
    assert app.state.delivery.closed


def test_windows_browser_hides_android_controls_and_keeps_send_disabled():
    node = shutil.which("node")
    if not node: pytest.skip("Optional Node regression")
    source = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'); const els=new Map();
const el=()=>({children:[],listeners:{},value:'',append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x},addEventListener(k,f){this.listeners[k]=f}});
const document={getElementById(id){if(!els.has(id))els.set(id,el());return els.get(id)}};const window={};
vm.runInNewContext(fs.readFileSync('backend/app/static/admin/delivery.js','utf8'),{window,document,crypto:require('crypto')});
const status={transport:'windows_live_studio',instance_id:'vm1',windows_status:'probe_required',inspection:null,physical_ready:false,delivery:'NOT READY'};
const calls=[];const api=async(path)=>{calls.push(path);return path.endsWith('/status')?status:[]};
const delivery=window.createDeliveryControls({api,node:(_,__,s)=>Object.assign(el(),{textContent:s}),toast(){},state:{connected:true},refreshCards(){}});
(async()=>{await delivery.refresh();assert(document.getElementById('android-settings').hidden);
for(const id of ['inspect-device','capture-device','verify-account','confirm-send']) {assert(document.getElementById(id).hidden);assert(document.getElementById(id).disabled)}
assert(document.getElementById('device-panel-title').textContent.includes('Windows'));
const send=el();delivery.attach(el(),{},send);assert(send.disabled);assert(!calls.some(p=>p.endsWith('/send')));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", source], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_real_live_studio_read_only_opt_in():
    if os.environ.get("RUN_LIVE_STUDIO_E2E") != "1":
        pytest.skip("Explicit read-only real UI discovery only: RUN_LIVE_STUDIO_E2E=1")
    report = probe()
    assert report.status == "discovered", report.status
    assert report.windows > 0 and report.nodes
