"""PC-SEND-1B: compile the real walkers against fully fake in-memory boundaries."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.windows.native_discovery import DiscoveryReport, collect
from app.windows.probe import ProbeReport, cli, probe, save_report

pytestmark = [pytest.mark.windows, pytest.mark.live_studio]


@pytest.fixture
def worker_platform(monkeypatch):
    # Mock Windows process launch boundaries even on non-Windows test hosts.
    monkeypatch.setattr("app.windows.native_discovery.sys.platform", "win32")
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)


@pytest.fixture(scope="module")
def scenarios():
    if sys.platform != "win32": pytest.skip("Stock .NET fake boundary harness requires Windows")
    paths = [Path("backend/app/windows/NativeDiscovery.cs").absolute(), Path("backend/tests/fixtures/windows/FakeDiscovery.cs").absolute()]
    literals = ",".join("'" + str(path).replace("'", "''") + "'" for path in paths)
    command = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); "
        "Add-Type -Path @(" + literals + ") -ReferencedAssemblies 'System.dll','Accessibility.dll'; "
        "[LiveStudioDiscovery.Harness]::All() | ConvertTo-Json -Depth 14 -Compress")
    executable = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand",
        base64.b64encode(command.encode("utf-16-le")).decode("ascii")],
        capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return json.loads(result.stdout.decode("utf-8-sig"))


def report(scenarios, mode): return DiscoveryReport.model_validate(scenarios[mode]["report"])


def test_rooted_hwnd_hierarchy_attributes_actual_child_pid(scenarios):
    data = report(scenarios, "hwnd")
    assert [(n.hwnd, n.parent_hwnd, n.depth, n.process_id) for n in data.hwnd.windows] == [
        (100, None, 0, 11300), (200, 100, 1, 11301), (300, 200, 2, 11302)]
    assert [n.chromium_kind for n in data.hwnd.windows] == ["widget", "render_widget", "other"]
    assert scenarios["hwnd"]["accessible_calls"] == 0
    assert data.msaa is None and data.physical_action is False
    assert {p.process_id for p in data.processes} == {11300, 11301, 11302}
    assert data.processes[1].parent_process_id == 11300 and data.processes[1].hwnds == (200,)


def test_msaa_roles_parent_relations_and_state_are_metadata(scenarios):
    data = report(scenarios, "msaa")
    nodes = data.msaa.nodes
    assert {n.semantic_hint for n in nodes} == {"document", "edit", "button", "text", "list", "listitem", "pane"}
    edit = next(n for n in nodes if n.semantic_hint == "edit")
    assert edit.parent_id == 1 and edit.depth == 1 and edit.focusable and edit.focused
    assert edit.owner_hwnd == 200 and edit.process_id == 11301
    simple = next(n for n in nodes if n.semantic_hint == "listitem")
    assert simple.owner_kind == "container"
    assert data.schema_version == 2 and data.backend == "msaa"
    assert data.studio_version == "1.36.6"
    assert scenarios["msaa"]["root_closes"] > 0


def test_names_are_opt_in_and_protected_subtree_never_read(scenarios):
    hidden = report(scenarios, "msaa")
    assert all(n.name is None for n in (*hidden.hwnd.windows, *hidden.msaa.nodes))
    assert scenarios["msaa"]["name_reads"] == 0
    visible = report(scenarios, "names")
    assert any(n.name == "PRIVATE UI NAME" for n in visible.msaa.nodes)
    protected = report(scenarios, "password")
    node = next(n for n in protected.msaa.nodes if n.protected_content)
    assert node.name is None and node.child_count == 0 and node.semantic_hint == "protected"
    assert not any(n.parent_id == node.node_id for n in protected.msaa.nodes)
    assert scenarios["password"]["name_reads"] == scenarios["password"]["protected_reads"] == 0
    hwnd = report(scenarios, "protected_hwnd")
    assert len(hwnd.hwnd.windows) == 1 and hwnd.hwnd.windows[0].name is None
    assert scenarios["protected_hwnd"]["accessible_calls"] == 0
    unknown = report(scenarios, "unreadable_state")
    assert unknown.status == "unknown_ui_state" and not unknown.msaa.nodes
    assert scenarios["unreadable_state"]["root_name_reads"] == 0


@pytest.mark.parametrize("case,code", [("cycle", "msaa_repeated"), ("hwnd_cycle", "hwnd_repeated"),
    ("depth", "msaa_depth_limit"), ("nodes", "msaa_node_limit"), ("hwnd_limit", "hwnd_limit")])
def test_cycles_and_limits_stop_traversal(scenarios, case, code):
    data = report(scenarios, case)
    assert data.truncated and code in data.errors and data.status == "unknown_ui_state"
    if case == "nodes": assert len(data.msaa.nodes) == 1
    if case == "hwnd_limit": assert len(data.hwnd.windows) == 1
    if case == "depth":
        assert max(n.depth for n in data.msaa.nodes) <= 1
        assert max(n.depth for n in data.hwnd.windows) <= 1 and "hwnd_depth_limit" in data.errors


@pytest.mark.parametrize("case,code", [("malformed", "msaa_unreadable"), ("bad_count", "msaa_unreadable"),
    ("bad_window", "hwnd_unreadable"), ("no_msaa", "msaa_unavailable")])
def test_unreadable_nodes_never_expose_exception_text(scenarios, case, code):
    data = report(scenarios, case)
    assert data.unreadable > 0 and code in data.errors
    assert data.status == "unknown_ui_state"
    assert "PRIVATE" not in data.model_dump_json() and len(data.errors) <= 16


@pytest.mark.parametrize("case", ["no_path", "denied_path"])
def test_process_path_is_optional(scenarios, case):
    data = report(scenarios, case)
    assert data.processes and all(p.executable_path is None for p in data.processes)
    assert data.studio_version is None and "process_metadata_unavailable" not in data.errors
    assert data.status == "discovered" and not data.unreadable


def test_unknown_owner_and_absent_pid_are_honest(scenarios):
    data = report(scenarios, "unknown_owner")
    assert any(n.owner_hwnd is None and n.process_id is None for n in data.msaa.nodes)
    missing = report(scenarios, "invalid_pid")
    assert missing.status == "window_not_found" and "process_not_found" in missing.errors


def test_version_uses_actual_image_path_before_report_shortening(scenarios):
    data = report(scenarios, "long_path")
    assert data.studio_version == "1.36.6" and len(data.processes[0].executable_path) == 1024


def test_native_wrapper_has_no_actions_or_value_reads():
    source = Path("backend/app/windows/NativeDiscovery.cs").read_text(encoding="utf-8")
    members = set(re.findall(r"accessible\.([A-Za-z_]+)", source))
    assert members == {"get_accState", "get_accRole", "get_accName", "accChildCount", "accLocation"}
    assert not re.search(r"SendMessage|SendInput|SetFocus|accDoDefaultAction|accSelect|Clipboard|WriteProcessMemory|CreateRemoteThread", source)


@pytest.mark.parametrize("args", [["--max-nodes", "3000"], ["--max-hwnd", "0"], ["--max-depth", "21"],
    ["--timeout", "nan"], ["--timeout", "61"], ["--process-id", "-1"], ["--process-id", "0"],
    ["--process-id", "not-a-pid"], ["--backend", "all"], ["--instance-id", "../escape"]])
def test_invalid_cli_arguments_are_clear_and_never_inspect(args, monkeypatch, capsys):
    monkeypatch.setattr("app.windows.probe.probe", lambda **kwargs: pytest.fail("Unexpected UIA"))
    monkeypatch.setattr("app.windows.native_discovery.collect", lambda **kwargs: pytest.fail("Unexpected native inspection"))
    with pytest.raises(SystemExit) as raised: cli(args)
    assert raised.value.code == 2
    assert "invalid_argument" in capsys.readouterr().err


def test_combined_report_uses_one_deadline_and_preserves_uia(scenarios, monkeypatch):
    native = dict(scenarios["names"]["report"], backend="all")
    calls = []
    def runner(argv, **kwargs):
        command = base64.b64decode(argv[-1]).decode("utf-16-le")
        assert "NativeDiscovery.cs" in command and "::Run(11300,'all'" in command
        assert "shell" not in kwargs and kwargs["timeout"] == 20
        calls.append("native")
        return SimpleNamespace(returncode=0, stdout=json.dumps(native).encode())
    def uia(**kwargs):
        calls.append("uia")
        assert 1 <= kwargs["timeout"] <= 20
        return ProbeReport(status="window_not_found", names_included=True)
    data = collect(backend="all", process_id=11300, include_names=True, runner=runner, uia_probe=uia)
    assert calls == ["native", "uia"] and data.uia is not None
    assert data.status == "unknown_ui_state" and "uia_incomplete" in data.errors


@pytest.mark.parametrize("failure", ["timeout", "bad_json", "oversized", "exit"])
def test_worker_failures_are_static(failure, worker_platform):
    def runner(*args, **kwargs):
        if failure == "timeout": raise subprocess.TimeoutExpired("PRIVATE", 1)
        return SimpleNamespace(returncode=1 if failure == "exit" else 0,
            stdout=b"x" * 2_000_001 if failure == "oversized" else b"PRIVATE")
    result = collect(backend="msaa", process_id=11300, runner=runner)
    assert result.status == ("timeout" if failure == "timeout" else "native_unavailable")
    assert "PRIVATE" not in result.model_dump_json() and result.physical_action is False


def test_combined_deadline_does_not_start_uia_after_native_budget(scenarios, monkeypatch):
    monkeypatch.setattr("app.windows.native_discovery.monotonic", iter((100, 120)).__next__)
    native = dict(scenarios["hwnd"]["report"], backend="all")
    result = collect(backend="all", process_id=11300,
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(native).encode()),
        uia_probe=lambda **k: pytest.fail("Deadline must prevent UIA start"))
    assert result.status == "timeout" and result.errors == ("timeout",)


def test_compile_failure_is_sanitized(worker_platform):
    result = collect(backend="hwnd", process_id=11300,
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=b'{"status":"native_unavailable"}'))
    assert result.errors == ("native_unavailable",)


def test_uia_worker_failure_preserves_requested_privacy_and_error_code(worker_platform):
    result = probe(include_names=True, tree_view="raw",
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=b'{"status":"uia_unavailable"}'))
    assert result.status == "uia_unavailable" and result.names_included
    assert result.errors == ("uia_unavailable",) and result.tree_view == "raw"


def test_schema_rejects_physical_action_and_private_worker_output(scenarios):
    for field, value in (("physical_action", True), ("names_included", True), ("process_id", 999)):
        native = dict(scenarios["hwnd"]["report"], **{field: value})
        result = collect(backend="hwnd", process_id=11300,
            runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(native).encode()))
        assert result.status == "native_unavailable" and result.physical_action is False


def test_versioned_report_storage_remains_ignored_and_bounded(scenarios, tmp_path):
    data = report(scenarios, "names")
    for _ in range(20): save_report(data, "vm1", root=tmp_path / "windows")
    with pytest.raises(ValueError): save_report(data, "vm1", root=tmp_path / "windows")
    ignored = subprocess.run(["git", "check-ignore", "diagnostics/windows/vm1/synthetic.json"], capture_output=True, text=True)
    assert ignored.returncode == 0
    bad = data.model_dump()
    bad["msaa"]["nodes"][0]["protected_content"] = True
    with pytest.raises(ValidationError): DiscoveryReport.model_validate(bad)
