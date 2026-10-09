"""PC-SEND-1B..1G: production serializers/contracts, fully fake UI/COM boundaries."""
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


def fake_scenarios(system_directory="System32"):
    if sys.platform != "win32": pytest.skip("Stock .NET fake boundary harness requires Windows")
    paths = [Path(p).absolute() for p in ("backend/app/windows/NativeDiscovery.cs", "backend/app/windows/Ia2Discovery.cs", "backend/app/windows/ClientDiscovery.cs", "backend/app/windows/ChromiumDiscovery.cs",
        "backend/tests/fixtures/windows/FakeDiscovery.cs", "backend/tests/fixtures/windows/FakeMsaaVariants.cs", "backend/tests/fixtures/windows/FakeNativeVariants.cs", "backend/tests/fixtures/windows/FakeIa2Probe.cs", "backend/tests/fixtures/windows/FakeClientProbe.cs", "backend/tests/fixtures/windows/FakeChromiumProbe.cs")]
    literals = ",".join("'" + str(path).replace("'", "''") + "'" for path in paths)
    command = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); "
        "Add-Type -Path @(" + literals + ") -ReferencedAssemblies 'System.dll','Accessibility.dll'; "
        "[LiveStudioDiscovery.Harness]::All() | ConvertTo-Json -Depth 14 -Compress")
    executable = str(Path(os.environ["SystemRoot"]) / system_directory / "WindowsPowerShell/v1.0/powershell.exe")
    if not Path(executable).is_file(): pytest.skip("Stock .NET architecture unavailable")
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand",
        base64.b64encode(command.encode("utf-16-le")).decode("ascii")],
        capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return json.loads(result.stdout.decode("utf-8-sig"))


@pytest.fixture(scope="module")
def scenarios(): return fake_scenarios()


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
    assert simple.owner_kind == "simple_child" and simple.child_id == 1
    assert data.schema_version == 7 and data.backend == "msaa"
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


@pytest.mark.parametrize("case,code", [("malformed", "msaa_state_unavailable"), ("bad_count", "msaa_child_count_unavailable"),
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
    com_wrapper = source.split("sealed class NativeComReads", 1)[1].split("sealed class NativeAccessible", 1)[0]
    members = set(re.findall(r"accessible\.([A-Za-z_]+)", com_wrapper))
    assert members == {"get_accState", "get_accRole", "get_accName", "get_accChild", "accChildCount", "accLocation"}
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


@pytest.mark.parametrize("case,names", [("seven_ids", False), ("seven_names", True)])
def test_raw_vt_i4_seven_children_keep_exact_ids_and_parent_metadata(scenarios, case, names):
    data = report(scenarios, case)
    assert data.status == "discovered" and not data.errors and data.unreadable == 0
    # Independent HWND roots can expose the same COM object without a false cycle.
    assert len(data.msaa.nodes) == 3 * 8
    for source in (100, 200, 300):
        nodes = [n for n in data.msaa.nodes if n.source_hwnd == source]
        root, *children = nodes
        assert root.owner_kind == "accessible_object" and root.child_id is None
        assert [n.child_id for n in children] == list(range(1, 8))
        assert len({n.node_id for n in children}) == 7
        assert all(n.parent_id == root.node_id and n.depth == 1 and n.owner_kind == "simple_child" for n in children)
        assert all(n.owner_hwnd == 200 and n.process_id == 11301 and n.child_count == 0 for n in children)
        assert all(n.name == (f"Label {n.child_id}" if names else None) for n in children)
    calls = scenarios[case]["calls"]
    for child_id in range(1, 8):
        for prop in ("state", "role", "location", "child_S_FALSE"):
            assert f"{prop}:{child_id}" in calls
        assert (f"name:{child_id}" in calls) is names


@pytest.mark.parametrize("case", ["child_object", "object_properties"])
def test_child_object_expands_after_preserved_child_id_and_can_supply_own_properties(scenarios, case):
    data = report(scenarios, case)
    assert data.status == "discovered" and not data.errors
    nodes = [n for n in data.msaa.nodes if n.source_hwnd == 100]
    root, simple, expanded, leaf = nodes
    assert simple.child_id == 1 and simple.parent_id == root.node_id
    assert expanded.owner_kind == "accessible_object" and expanded.child_id is None
    assert expanded.parent_id == simple.node_id and expanded.depth == 2
    assert leaf.child_id == 1 and leaf.parent_id == expanded.node_id
    assert "child:1" in scenarios[case]["calls"] and "state:0" in scenarios[case]["object_calls"]
    assert scenarios[case]["object_closes"] == 3


def test_vt_dispatch_is_an_object_node_not_a_simple_child(scenarios):
    data = report(scenarios, "dispatch")
    root, child, leaf = [n for n in data.msaa.nodes if n.source_hwnd == 100]
    assert child.owner_kind == "accessible_object" and child.parent_id == root.node_id
    assert child.child_id is None and leaf.parent_id == child.node_id and leaf.child_id == 1


@pytest.mark.parametrize("case,code", [("object_cycle", "msaa_repeated"), ("simple_repeat", "msaa_repeated"),
    ("variant_nodes", "msaa_node_limit"), ("variant_depth", "msaa_depth_limit")])
def test_raw_variant_cycles_and_budgets_remain_bounded(scenarios, case, code):
    data = report(scenarios, case)
    assert data.status == "unknown_ui_state" and data.truncated and code in data.errors
    if case == "variant_nodes": assert len(data.msaa.nodes) == 2
    if case == "variant_depth": assert max(n.depth for n in data.msaa.nodes) == 1


@pytest.mark.parametrize("case,code,field", [("role_failure", "msaa_role_unavailable", "role"),
    ("name_failure", "msaa_name_unavailable", "name"), ("location_failure", "msaa_location_unavailable", "bounds"),
    ("count_failure", "msaa_child_count_unavailable", "child_count")])
def test_optional_property_hresult_preserves_node_with_null(scenarios, case, code, field):
    data = report(scenarios, case)
    nodes = [n for n in data.msaa.nodes if n.source_hwnd == 100]
    node = nodes[0] if case == "count_failure" else next(n for n in nodes if n.child_id == 1)
    assert getattr(node, field) is None and code in data.errors and data.unreadable > 0
    assert "PRIVATE HRESULT" not in data.model_dump_json()


@pytest.mark.parametrize("case", ["simple_protected", "simple_unknown", "protected_object", "object_protected_parent_clear", "object_unknown_state"])
def test_raw_variant_protected_or_unknown_state_never_reads_name_or_descendants(scenarios, case):
    data = report(scenarios, case)
    assert "name:1" not in scenarios[case]["calls"]
    if case in {"simple_unknown", "object_unknown_state"}:
        assert "msaa_state_unavailable" in data.errors
        assert not any(n.child_id == 1 for n in data.msaa.nodes)
    else:
        node = next(n for n in data.msaa.nodes if n.child_id == 1)
        assert node.protected_content and node.name is None
        assert not any(n.parent_id == node.node_id for n in data.msaa.nodes)
    if case in {"protected_object", "object_protected_parent_clear", "object_unknown_state"}:
        assert "name:0" not in scenarios[case]["object_calls"]
        assert scenarios[case]["object_closes"] == 3


def test_child_object_failure_and_unsupported_variant_are_distinct(scenarios):
    failed = report(scenarios, "object_failure")
    assert "msaa_child_object_unavailable" in failed.errors
    assert any(n.child_id == 1 for n in failed.msaa.nodes)
    unsupported = report(scenarios, "unsupported_variant")
    assert "msaa_child_unsupported" in unsupported.errors and "msaa_unreadable" not in unsupported.errors
    enumeration = report(scenarios, "children_failure")
    assert enumeration.errors == ("msaa_children_unavailable",)
    assert len(enumeration.msaa.nodes) == 3 and "PRIVATE HRESULT" not in enumeration.model_dump_json()


def test_eight_target_roots_followed_by_unrelated_windows_are_not_truncated(scenarios):
    data = report(scenarios, "eight_roots")
    assert data.status == "discovered" and not data.truncated and not data.errors
    assert sum(n.depth == 0 for n in data.hwnd.windows) == 8


@pytest.mark.parametrize("case,code", [("desktop_limit", "desktop_scan_limit"), ("nine_roots", "target_root_limit"),
    ("target_limit", "target_hwnd_scan_limit"), ("process_scan_limit", "process_scan_limit")])
def test_scan_limits_identify_source_and_preserve_conservative_completeness(scenarios, case, code):
    data = report(scenarios, case)
    assert data.truncated and data.status == "unknown_ui_state" and data.errors == (code,)
    assert data.hwnd.windows
    if case == "desktop_limit":
        assert len(data.hwnd.windows) == 3  # Found root's children complete; other roots remain unproven.


def test_schema_requires_child_id_only_for_simple_representation(scenarios):
    data = report(scenarios, "seven_ids").model_dump()
    data["msaa"]["nodes"][1]["child_id"] = None
    with pytest.raises(ValidationError): DiscoveryReport.model_validate(data)


@pytest.mark.parametrize("case,tags", [("native_i4", [3] * 7), ("native_dispatch", [9] * 7),
    ("native_mixed", [3, 9, 3, 9, 3, 9, 3])])
def test_native_buffer_seven_children_and_owned_dispatch_lifetimes(scenarios, case, tags):
    data = report(scenarios, case)
    assert data.status == "discovered" and not data.msaa.child_diagnostics
    nodes = [n for n in data.msaa.nodes if n.source_hwnd == 100]
    root, *children = nodes
    assert len(children) == 7
    for index, (node, vt) in enumerate(zip(children, tags, strict=True)):
        assert node.parent_id == root.node_id and node.depth == 1
        assert node.child_id == (index + 1 if vt == 3 else None)
        assert node.owner_kind == ("simple_child" if vt == 3 else "accessible_object")
    counts = scenarios[case]
    assert counts["queries"] == counts["closes"] == tags.count(9) * 3
    assert counts["inits"] == counts["clears"] == 21
    assert counts["payload_offset"] == 8
    assert counts["slot_size"] == (24 if counts["pointer_size"] == 8 else 16)


def test_partial_s_false_uses_only_obtained_and_ignores_empty_tail(scenarios):
    data = report(scenarios, "native_partial")
    assert len(data.msaa.nodes) == 9  # Three HWNDs, each root plus two children, not seven empty slots.
    assert len(data.msaa.child_diagnostics) == 3
    for diagnostic in data.msaa.child_diagnostics:
        assert diagnostic.reason == "partial_accessible_children"
        assert diagnostic.requested == 7 and diagnostic.obtained == 2
        assert diagnostic.hresult_category == "s_false" and diagnostic.vt is None
    assert "msaa_child_unsupported" not in data.errors
    assert data.errors == ("msaa_children_incomplete",)
    assert scenarios["native_partial"]["inits"] == scenarios["native_partial"]["clears"] == 21


@pytest.mark.parametrize("case,vt,category,reason,hr", [
    ("native_empty", 0, "empty", "empty_slot", "s_ok"),
    ("native_qi", 9, "dispatch", "dispatch_qi_failed", "no_interface"),
    ("native_malformed", 65535, "other", "malformed_variant", "s_ok"),
    ("native_bad_id", 3, "i4", "reserved_system_objid", "s_false"),
    ("native_null_dispatch", 9, "dispatch", "malformed_variant", "s_ok"),
])
def test_bad_native_child_has_scalar_diagnostics_and_does_not_abort_siblings(scenarios, case, vt, category, reason, hr):
    data = report(scenarios, case)
    diagnostic = data.msaa.child_diagnostics[0]
    assert diagnostic.vt == vt and diagnostic.category == category
    assert diagnostic.reason == reason and diagnostic.hresult_category == hr
    assert diagnostic.source_hwnd == 100 and diagnostic.parent_id == 1
    assert diagnostic.child_index == (1 if case == "native_empty" else 0)
    if case == "native_bad_id":
        assert not data.errors and data.status == "discovered"
    else:
        assert "msaa_child_unsupported" in data.errors and data.status == "unknown_ui_state"
    if case != "native_qi": assert len([n for n in data.msaa.nodes if n.source_hwnd == 100]) == 7
    assert scenarios[case]["inits"] == scenarios[case]["clears"] == 21


def test_unsupported_native_payloads_are_never_dereferenced_or_serialized(scenarios):
    data = report(scenarios, "native_unsupported")
    diagnostics = data.msaa.child_diagnostics
    assert [(d.vt, d.category, d.reason) for d in diagnostics[:3]] == [
        (8, "other", "unsupported_variant_type"), (0x4003, "byref", "unsupported_variant_type"),
        (0x2003, "array", "unsupported_variant_type")]
    assert all(set(d.model_dump()) == {"source_hwnd", "parent_id", "child_index", "vt", "category",
        "reason", "hresult_category", "requested", "obtained", "child_id_kind"} for d in diagnostics)
    output = data.model_dump_json()
    for private in ("PRIVATE", "305419896", "1985229328", "12345678", "76543210", "object-"):
        assert private not in output
    assert len(data.msaa.nodes) == 15  # Valid siblings after unsupported slots are still visited.


@pytest.mark.parametrize("case,reason,hr", [("native_bad_obtained", "malformed_variant", "s_ok"),
    ("native_fetch_failure", "child_resolution_failed", "access_denied")])
def test_invalid_obtained_or_failed_fetch_never_reads_children(scenarios, case, reason, hr):
    data = report(scenarios, case)
    assert len(data.msaa.nodes) == 3 and scenarios[case]["queries"] == 0
    assert all(d.reason == reason and d.hresult_category == hr and d.vt is None for d in data.msaa.child_diagnostics)
    assert scenarios[case]["inits"] == scenarios[case]["clears"] == 21


def test_property_resolution_and_bounded_diagnostics_remain_distinct(scenarios):
    assert all(d.reason == "child_property_failed" for d in report(scenarios, "role_failure").msaa.child_diagnostics)
    assert all(d.reason == "child_resolution_failed" for d in report(scenarios, "object_failure").msaa.child_diagnostics)
    bounded = report(scenarios, "native_diag_cap")
    assert len(bounded.msaa.child_diagnostics) == 64 and bounded.msaa.diagnostics_dropped == 236
    assert bounded.unreadable == 300 and len(bounded.errors) <= 16
    assert scenarios["early_disposal"] == {"inits": 7, "clears": 7}


def test_failed_slots_consume_node_budget_and_clear_failure_still_cleans_all_slots(scenarios):
    budget = report(scenarios, "native_diag_budget")
    assert len(budget.msaa.nodes) == len(budget.msaa.child_diagnostics) == 1
    assert budget.truncated and "msaa_node_limit" in budget.errors
    assert scenarios["native_diag_budget"]["inits"] == scenarios["native_diag_budget"]["clears"] == 1
    failed = report(scenarios, "native_clear_failure")
    assert failed.status == "unknown_ui_state" and "msaa_children_unavailable" in failed.errors
    assert all(d.hresult_category == "invalid_argument" for d in failed.msaa.child_diagnostics)
    assert scenarios["native_clear_failure"]["inits"] == scenarios["native_clear_failure"]["clears"] == 21


def test_visible_root_priority_and_observed_totals_without_raising_caps(scenarios):
    data = report(scenarios, "root_priority")
    stats = data.hwnd.root_statistics
    assert (stats.target_roots_total, stats.target_roots_selected, stats.target_roots_skipped) == (13, 8, 5)
    assert stats.enumeration_complete and data.errors == ("target_root_limit",)
    assert [n.hwnd for n in data.hwnd.windows[:3]] == [100, 200, 300]
    roots = data.hwnd.selected_roots
    assert [r.hwnd for r in roots] == list(range(100, 108))
    assert roots[0].visible and all(not r.visible for r in roots[1:])
    assert all(r.hwnd_traversal_complete and r.msaa_traversal_complete is None for r in roots)
    desktop = report(scenarios, "desktop_limit")
    assert not desktop.hwnd.root_statistics.enumeration_complete
    assert desktop.hwnd.selected_roots[0].hwnd_traversal_complete
    incomplete = report(scenarios, "nodes").hwnd.selected_roots[0]
    assert incomplete.hwnd_traversal_complete and incomplete.msaa_traversal_complete is False


@pytest.mark.parametrize("field,value", [("vt", 65536), ("child_index", -1), ("category", "PRIVATE"),
    ("reason", "PRIVATE"), ("hresult_category", "PRIVATE"), ("pointer", 0x12345678)])
def test_diagnostic_schema_rejects_unbounded_or_private_metadata(scenarios, field, value):
    data = report(scenarios, "native_empty").model_dump()
    data["msaa"]["child_diagnostics"][0][field] = value
    with pytest.raises(ValidationError): DiscoveryReport.model_validate(data)


def test_x86_variant_layout_and_seven_slots_use_same_production_reader():
    cases = fake_scenarios("SysWOW64")
    for mode in ("native_i4", "native_dispatch", "native_mixed"):
        assert cases[mode]["pointer_size"] == 4 and cases[mode]["slot_size"] == 16
        assert cases[mode]["payload_offset"] == 8
        assert report(cases, mode).status == "discovered"
        assert cases[mode]["inits"] == cases[mode]["clears"] == 21


@pytest.mark.parametrize("case,status,provider,attempted,supported", [
    ("ia2_no_provider", "provider_unavailable", False, False, False),
    ("ia2_no_interface", "ia2_unavailable", True, True, False),
    ("ia2_unchanged", "supported", True, True, True),
    ("ia2_null", "ia2_unavailable", True, True, False),
    ("ia2_query_error", "capability_failed", True, True, False),
])
def test_ia2_capability_query_service_contract_and_failures(scenarios, case, status, provider, attempted, supported):
    data = report(scenarios, case)
    assert data.backend == "ia2" and data.ia2 is not None
    assert len(data.ia2.capability_checks) == 3
    for check in data.ia2.capability_checks:
        capability = check.capability
        assert capability.status == status
        assert capability.iserviceprovider_available is provider
        assert capability.query_service_attempted is attempted and capability.ia2_available is supported
        assert check.owner_hwnd == 200 and check.process_id == 11301
        assert scenarios[case]["calls"][str(check.source_hwnd)] == [
            "provider:6d5140c1-7436-11ce-8034-00aa006009fa",
            *(["service:618736e0-3c3d-11cf-810c-00aa00389b71:e89f726e-c4f4-4c19-bb19-b647d7fa8478"] if attempted else [])]
    assert data.ia2.query_success_count == (3 if supported else 0)
    assert data.ia2.query_failure_count == (3 if attempted and not supported else 0)
    assert scenarios[case]["accessible_calls"] == scenarios[case]["root_closes"] == scenarios[case]["bridge_closes"] == 3


@pytest.mark.parametrize("case,changed,after", [("ia2_unchanged", False, 6), ("ia2_expands", True, 8)])
def test_ia2_reuses_exact_root_and_snapshots_all_baselines_before_queries(scenarios, case, changed, after):
    data = report(scenarios, case)
    evidence = data.ia2
    assert evidence.before_msaa_nodes == len(evidence.before_msaa.nodes) == 6
    assert evidence.after_msaa_nodes == len(data.msaa.nodes) == after
    assert evidence.comparison_complete and evidence.tree_changed_after_ia2_query is changed
    assert evidence.visible_renderer_checked and evidence.visible_renderer_supported
    renderer = next(c for c in evidence.capability_checks if c.source_hwnd == 200)
    assert renderer.source_process_id == 11301 and renderer.root_hwnd == 100 and renderer.visible_root
    assert renderer.before_child_count == renderer.after_child_count == 1  # Count alone cannot detect changed descendants.
    assert renderer.tree_changed_after_ia2_query is changed
    trace = scenarios[case]["trace"]
    first_query = next(i for i, event in enumerate(trace) if event.endswith(":query"))
    for hwnd in (100, 200, 300): assert f"{hwnd}:count:0" in trace[:first_query]
    assert [event for event in trace if event.endswith(":query")] == ["100:query", "200:query", "300:query"]
    # Existing safe serializer owns local parents and serializes the expansion; no IA2 traversal.
    after_nodes = [n for n in data.msaa.nodes if n.source_hwnd == 200]
    assert len(after_nodes) == (4 if changed else 2)
    if changed:
        assert after_nodes[2].parent_id == after_nodes[1].node_id
        assert after_nodes[2].child_id == 1


def test_ia2_visible_renderer_precedes_hidden_sources_despite_root_cap(scenarios):
    data = report(scenarios, "ia2_priority")
    stats = data.hwnd.root_statistics
    assert (stats.target_roots_total, stats.target_roots_selected, stats.target_roots_skipped) == (15, 8, 7)
    assert "target_root_limit" in data.errors and data.truncated
    assert [c.source_hwnd for c in data.ia2.capability_checks[:2]] == [100, 200]
    assert data.ia2.visible_renderer_supported and data.ia2.comparison_complete
    assert data.ia2.tree_changed_after_ia2_query is False


@pytest.mark.parametrize("case,code", [("ia2_budget", "msaa_node_limit"), ("ia2_depth", "msaa_depth_limit"), ("ia2_deadline", "timeout")])
def test_ia2_expansion_has_joint_budgets_and_incomplete_comparisons_are_unknown(scenarios, case, code):
    data = report(scenarios, case)
    assert data.truncated and code in data.errors and data.status == "unknown_ui_state"
    assert data.ia2.tree_changed_after_ia2_query is None and not data.ia2.comparison_complete
    if case == "ia2_budget":
        assert data.ia2.before_msaa_nodes + data.ia2.after_msaa_nodes <= 4
        assert data.ia2.visible_renderer_supported  # Capability query has its own bounded budget.
        renderer = next(c for c in data.ia2.capability_checks if c.source_hwnd == 200)
        assert renderer.before_msaa_nodes is not None and renderer.after_msaa_nodes is not None
    if case == "ia2_depth":
        assert max(n.depth for n in (*data.msaa.nodes, *data.ia2.before_msaa.nodes)) <= 1
    if case == "ia2_deadline":
        assert [t for t in scenarios[case]["trace"] if t.endswith(":query")] == ["100:query"]
        assert data.ia2.after_msaa_nodes == 0


@pytest.mark.parametrize("case,status", [("ia2_protected", "protected"), ("ia2_unknown", "unknown_protection")])
def test_ia2_protection_gate_precedes_names_children_and_capability_query(scenarios, case, status):
    data = report(scenarios, case)
    renderer = next(c for c in data.ia2.capability_checks if c.source_hwnd == 200)
    assert renderer.capability.status == status and not renderer.capability.query_service_attempted
    assert not scenarios[case]["calls"]["200"]
    assert not any(t.startswith("200:name:") or t.startswith("200:count:") for t in scenarios[case]["trace"])
    assert not any(n.source_hwnd == 200 for n in (*data.msaa.nodes, *data.ia2.before_msaa.nodes))
    assert scenarios[case]["bridge_closes"] == 2 and scenarios[case]["root_closes"] == 3


def test_ia2_names_are_opt_in_in_both_snapshots_and_private_com_data_stays_absent(scenarios):
    hidden = report(scenarios, "ia2_expands")
    assert all(n.name is None for n in (*hidden.msaa.nodes, *hidden.ia2.before_msaa.nodes))
    named = report(scenarios, "ia2_names")
    assert any(n.name for n in named.msaa.nodes) and any(n.name for n in named.ia2.before_msaa.nodes)
    for case in ("ia2_unchanged", "ia2_query_error", "ia2_names"):
        output = report(scenarios, case).model_dump_json()
        for private in ("PRIVATE ROOT ADDRESS", "PRIVATE COM CHILD", "0x76543210", "COM ADDRESS", "exception", "pointer"):
            assert private not in output
        if case != "ia2_names": assert "PRIVATE" not in output
    invalid = named.model_dump()
    invalid["names_included"] = False
    with pytest.raises(ValidationError): DiscoveryReport.model_validate(invalid)


def test_ia2_uses_minimal_read_only_service_declaration_and_never_calls_ia2_methods():
    source = Path("backend/app/windows/Ia2Discovery.cs").read_text(encoding="utf-8")
    declaration = source.split("interface INativeServiceProvider {", 1)[1].split("}", 1)[0]
    assert "[PreserveSig] int QueryService(ref Guid service, ref Guid iid, out IntPtr result);" in declaration
    assert "InterfaceType(ComInterfaceType.InterfaceIsIUnknown)" in source
    assert "interface IAccessible2" not in source and "GetObjectForIUnknown(ia2)" not in source
    combined = source + Path("backend/app/windows/NativeDiscovery.cs").read_text(encoding="utf-8")
    assert not re.search(r"\b(?:doAction|scrollTo|scrollToPoint|accDoDefaultAction|accSelect|SetFocus|setValue|get_accValue|accValue|attributes|SendInput|SetClipboardData|WriteProcessMemory|CreateRemoteThread)\s*\(", combined)
    assert "Marshal.Release(ia2)" in source  # Even failed calls returning a pointer release it.


def test_childid_self_is_informational_and_sign_classification_is_sanitized(scenarios):
    self_ref = report(scenarios, "native_self")
    assert self_ref.status == "discovered" and not self_ref.errors and self_ref.unreadable == 0
    assert len(self_ref.msaa.nodes) == 21
    assert all(d.child_id_kind == "zero" and d.reason == "childid_self_reference" for d in self_ref.msaa.child_diagnostics)
    negative = report(scenarios, "native_bad_id")
    assert all(d.child_id_kind == "negative" and d.reason == "reserved_system_objid" and d.hresult_category == "s_false" for d in negative.msaa.child_diagnostics)
    positive = report(scenarios, "role_failure")
    assert all(d.child_id_kind == "positive" for d in positive.msaa.child_diagnostics)
    assert all(n.child_id is None or n.child_id > 0 for n in self_ref.msaa.nodes)


def test_ia2_failure_isolated_from_default_backends_and_worker_timeout_is_shared(scenarios, worker_platform):
    data = report(scenarios, "ia2_no_interface")
    assert len(data.msaa.nodes) == len(data.ia2.before_msaa.nodes) == 6
    for mode in ("hwnd", "msaa"):
        assert report(scenarios, mode).ia2 is None
    commands = []
    def runner(argv, **kwargs):
        command = base64.b64decode(argv[-1]).decode("utf-16-le")
        assert "Ia2Discovery.cs" in command and "'ia2',256,500,12,$false,9.0)" in command
        assert kwargs["timeout"] == 9
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=data.model_dump_json().encode())
    assert collect(backend="ia2", process_id=11300, timeout=9, runner=runner).ia2 is not None
    assert len(commands) == 1
    def blocked(*a, **k): raise subprocess.TimeoutExpired("PRIVATE", 9)
    failure = collect(backend="ia2", process_id=11300, timeout=9, runner=blocked)
    assert failure.status == "timeout" and failure.physical_action is False


@pytest.mark.parametrize("field,value", [("raw_pointer", 0x76543210), ("owner_hwnd", -1), ("chromium_kind", "PRIVATE")])
def test_ia2_schema_rejects_private_fields_and_invalid_context(scenarios, field, value):
    data = report(scenarios, "ia2_unchanged").model_dump()
    data["ia2"]["capability_checks"][0][field] = value
    with pytest.raises(ValidationError): DiscoveryReport.model_validate(data)


def test_ia2_schema_validates_before_snapshot_privacy_and_aggregate_counts(scenarios):
    for mutate in (lambda d: d["ia2"].update(query_success_count=0),
        lambda d: d["ia2"]["before_msaa"]["nodes"][0].update(name="PRIVATE"),
        lambda d: d["ia2"]["capability_checks"][0].update(source_hwnd=999999)):
        data = report(scenarios, "ia2_unchanged").model_dump()
        mutate(data)
        with pytest.raises(ValidationError): DiscoveryReport.model_validate(data)


@pytest.mark.parametrize("case,queried", [("ia2_scope", False), ("ia2_scope_after", True)])
def test_ia2_rechecks_source_process_scope_before_query_and_after_it(scenarios, case, queried):
    data = report(scenarios, case)
    renderer = next(c for c in data.ia2.capability_checks if c.source_hwnd == 200)
    assert renderer.capability.query_service_attempted is queried
    assert renderer.after_msaa_nodes is None and not renderer.comparison_complete
    assert data.status == "unknown_ui_state" and data.unreadable > 0
    assert data.ia2.tree_changed_after_ia2_query is None and not data.ia2.comparison_complete
    assert ("200:query" in scenarios[case]["trace"]) is queried
    if queried: assert "ia2_scope_changed" in data.errors


def test_ia2_cli_saves_private_paired_report_and_prints_only_bounded_metadata(scenarios, tmp_path, monkeypatch, capsys):
    data = report(scenarios, "ia2_expands")
    calls = []
    def native(**kwargs):
        calls.append(kwargs)
        return data
    monkeypatch.setattr("app.windows.native_discovery.collect", native)
    monkeypatch.setattr("app.windows.probe.save_report", lambda value, instance: save_report(value, instance, root=tmp_path))
    monkeypatch.setattr("app.windows.probe.probe", lambda **kwargs: pytest.fail("IA2 must not start UIA"))
    assert cli(["--backend", "ia2", "--process-id", "11300", "--instance-id", "vm1"]) == 0
    assert len(calls) == 1 and calls[0]["backend"] == "ia2" and not calls[0]["include_names"]
    output = json.loads(capsys.readouterr().out)
    assert output["backend"] == "ia2" and output["msaa_nodes"] == 8 and output["physical_action"] is False
    assert "capability_checks" not in output and "PRIVATE" not in json.dumps(output)
    stored = DiscoveryReport.model_validate_json(Path(output["report"]).read_text(encoding="utf-8"))
    assert stored.ia2.before_msaa_nodes == 6 and stored.ia2.after_msaa_nodes == 8


def test_ia2_check_cap_cannot_evict_visible_renderer_and_diagnostics_cap_is_joint(scenarios):
    capped = report(scenarios, "ia2_check_cap")
    assert len(capped.ia2.capability_checks) == 32 and "ia2_check_limit" in capped.errors
    assert [c.source_hwnd for c in capped.ia2.capability_checks[:2]] == [100, 200]
    assert capped.ia2.visible_renderer_supported
    assert scenarios["ia2_check_cap"]["accessible_calls"] == 32
    noisy = report(scenarios, "ia2_diagnostics")
    assert len(noisy.ia2.before_msaa.child_diagnostics) + len(noisy.msaa.child_diagnostics) == 64
    assert noisy.ia2.before_msaa.diagnostics_dropped > 0 and noisy.msaa.diagnostics_dropped > 0
    assert noisy.ia2.tree_changed_after_ia2_query is None


def test_ia2_primary_renderer_precedes_other_visible_widgets_and_their_success_does_not_mask_failure(scenarios):
    data = report(scenarios, "ia2_primary_failure")
    assert data.ia2.primary_root_hwnd == 100
    assert [c.source_hwnd for c in data.ia2.capability_checks] == [100, 200, 101, 400]
    assert data.ia2.visible_renderer_checked and not data.ia2.visible_renderer_supported
    other = next(c for c in data.ia2.capability_checks if c.source_hwnd == 400)
    assert other.capability.ia2_available and other.visible_root
    assert data.ia2.tree_changed_after_ia2_query is None


def client_renderer(data):
    return next(p for p in data.client_objects.checks if p.source_hwnd == 200)


def test_dual_probe_retains_separate_window_and_application_client_sources(scenarios):
    data = report(scenarios, "dual_tree")
    assert data.backend == "client" and data.msaa is None
    renderer = client_renderer(data)
    assert renderer.objid_window.available and renderer.objid_window.role == 9
    assert renderer.objid_window.source_kind == "objid_window" and renderer.objid_window.object_id == 0
    assert renderer.objid_window.child_count == 2 and renderer.objid_window.bounds == (0, 2, 30, 40)
    assert renderer.objid_client.available and renderer.objid_client.role == 15
    assert renderer.objid_client.source_kind == "objid_client" and renderer.objid_client.object_id == -4
    assert renderer.objid_client.child_count == 4 and renderer.objid_client.metadata_phase == "before"
    assert {n.semantic_hint for n in data.msaa_client.nodes} >= {"document", "pane", "edit", "button", "text"}
    assert all(n.source_kind == "objid_client" and n.object_id == -4 for n in data.msaa_client.nodes)
    window_nodes = data.client_objects.window_msaa.nodes
    assert len(window_nodes) == 3
    assert all(n.source_kind == "objid_window" and n.object_id == 0 and n.parent_id is None for n in window_nodes)
    client_nodes = [n for n in data.msaa_client.nodes if n.source_hwnd == 200]
    assert len(client_nodes) == data.client_objects.visible_renderer_client_nodes == 6
    assert all(n.parent_id is not None for n in client_nodes[1:])
    assert any(n.owner_kind == "simple_child" and n.child_id == 1 for n in client_nodes)
    assert data.client_objects.visible_renderer_client_available
    assert scenarios["dual_tree"]["acquired_count"] == scenarios["dual_tree"]["closes"] == 6


def test_dual_probe_exact_native_acquisition_object_ids_iid_and_client_priority(scenarios):
    trace = scenarios["dual_tree"]["trace"]
    opens = [event for event in trace if ":open:" in event]
    assert opens == [f"{hwnd}:client:open:4294967292:618736e0-3c3d-11cf-810c-00aa00389b71" for hwnd in (100, 200, 300)] + [
        f"{hwnd}:window:open:0:618736e0-3c3d-11cf-810c-00aa00389b71" for hwnd in (100, 200, 300)]
    # Injected IObjectApi is the exact boundary used by NativeObjectApi's P/Invoke.
    source = Path("backend/app/windows/ClientDiscovery.cs").read_text(encoding="utf-8")
    assert "api.FromWindow(hwnd, unchecked((uint)objectId), NativeDesktop.AccessibleId, out reads)" in source
    assert "AccessibleObjectFromWindow(new IntPtr(hwnd), objectId, ref iid, out accessible)" in source
    legacy = Path("backend/app/windows/NativeDiscovery.cs").read_text(encoding="utf-8")
    assert "OpenAccessible(hwnd, ObjectIds.Client)" in legacy
    assert "CreateStdAccessibleObject(" not in source + legacy
    assert not re.search(r"\b(?:SendMessage|PostMessage)\s*\(", source + legacy)


def test_dual_probe_queries_ia2_only_on_client_and_window_provider_failure_cannot_mask_support(scenarios):
    data = report(scenarios, "dual_tree")
    renderer = client_renderer(data)
    assert renderer.objid_window.iserviceprovider_available is False
    assert renderer.objid_window.provider_hresult_category == "no_interface"
    assert renderer.objid_window.ia2_available is None and not renderer.objid_window.query_service_attempted
    assert renderer.objid_window.ia2_query_status == "not_queried_window"
    assert renderer.objid_client.iserviceprovider_available and renderer.objid_client.ia2_available
    assert renderer.objid_client.query_hresult_category == "s_ok"
    assert data.client_objects.visible_renderer_client_ia2_supported
    for key, calls in scenarios["dual_tree"]["calls"].items():
        assert len(calls) == (2 if key.endswith(":client") else 1)
        if key.endswith(":window"): assert not any(c.startswith("service:") for c in calls)


def test_window_service_provider_success_still_never_requests_ia2(scenarios):
    data = report(scenarios, "dual_window_provider")
    window = client_renderer(data).objid_window
    assert window.iserviceprovider_available is True and window.provider_hresult_category == "s_ok"
    assert window.ia2_available is None and window.ia2_query_status == "not_queried_window"
    assert scenarios["dual_window_provider"]["calls"]["200:window"] == ["provider:6d5140c1-7436-11ce-8034-00aa006009fa"]


@pytest.mark.parametrize("case,client_available,window_available,client_hr,window_hr", [
    ("dual_client_no_interface", False, True, "no_interface", "s_ok"),
    ("dual_client_error", False, True, "access_denied", "s_ok"),
    ("dual_window_failure", True, False, "s_ok", "failed"),
    ("dual_both_failure", False, False, "no_interface", "failed"),
])
def test_dual_acquisition_failures_are_sanitized_independent_and_release_failed_retvals(scenarios, case, client_available, window_available, client_hr, window_hr):
    data = report(scenarios, case)
    renderer = client_renderer(data)
    assert renderer.objid_client.available is client_available
    assert renderer.objid_window.available is window_available
    assert renderer.objid_client.hresult_category == client_hr and renderer.objid_window.hresult_category == window_hr
    assert data.client_objects.visible_renderer_client_available is client_available
    assert bool(data.msaa_client.nodes) is client_available
    assert bool(data.client_objects.window_msaa.nodes) is window_available
    assert scenarios[case]["acquired_count"] == scenarios[case]["closes"]
    for private in ("PRIVATE", "0x76543210", "COM ADDRESS", "EXCEPTION"):
        assert private not in data.model_dump_json()


def test_client_success_with_shell_roles_is_evidence_without_readiness(scenarios):
    data = report(scenarios, "dual_shell")
    renderer = client_renderer(data)
    assert renderer.objid_client.available and renderer.objid_client.role == 9
    assert renderer.objid_client.ia2_available is False and renderer.objid_client.query_hresult_category == "invalid_argument"
    assert not data.client_objects.visible_renderer_client_ia2_supported
    assert data.physical_action is False
    assert data.ia2.tree_changed_after_ia2_query is False


def test_dual_visible_renderer_client_precedes_hidden_roots_even_at_root_limit(scenarios):
    data = report(scenarios, "dual_priority")
    assert "target_root_limit" in data.errors and data.hwnd.root_statistics.target_roots_skipped == 7
    assert [p.source_hwnd for p in data.client_objects.checks[:2]] == [100, 200]
    opens = [event for event in scenarios["dual_priority"]["trace"] if ":open:" in event]
    assert opens[1].startswith("200:client:open:")
    assert data.client_objects.visible_renderer_client_available and data.client_objects.visible_renderer_client_ia2_supported


def test_dual_visible_renderer_cannot_be_evicted_by_hidden_renderers_of_same_primary_root(scenarios):
    data = report(scenarios, "dual_hidden_renderer")
    assert len(data.client_objects.checks) == 32 and "ia2_check_limit" in data.errors
    assert [p.source_hwnd for p in data.client_objects.checks[:2]] == [100, 200]
    assert all(not p.source_visible for p in data.client_objects.checks[2:])
    assert data.client_objects.visible_renderer_client_available and data.client_objects.visible_renderer_client_ia2_supported


@pytest.mark.parametrize("case,code", [("dual_budget", "msaa_node_limit"), ("dual_depth", "msaa_depth_limit"), ("dual_deadline", "timeout")])
def test_dual_client_tree_joint_limits_depth_and_deadline(scenarios, case, code):
    data = report(scenarios, case)
    assert data.truncated and code in data.errors and not data.ia2.comparison_complete
    assert data.ia2.tree_changed_after_ia2_query is None
    if case == "dual_budget":
        assert sum(len(s.nodes) for s in (data.msaa_client, data.ia2.before_msaa, data.client_objects.window_msaa)) <= 4
        assert data.client_objects.visible_renderer_client_ia2_supported
    if case == "dual_depth": assert max(n.depth for n in data.msaa_client.nodes) <= 1
    if case == "dual_deadline":
        assert not data.msaa_client.nodes and not data.client_objects.window_msaa.nodes
        assert not any(":window:open:" in t for t in scenarios[case]["trace"])


@pytest.mark.parametrize("case", ["dual_protected_client", "dual_unknown_client", "dual_protected_window"])
def test_dual_protection_precedes_names_children_and_provider(scenarios, case):
    data = report(scenarios, case)
    renderer = client_renderer(data)
    trace = scenarios[case]["trace"]
    kind = "window" if case == "dual_protected_window" else "client"
    assert not any(t.startswith(f"200:{kind}:name:") or t.startswith(f"200:{kind}:count:") for t in trace)
    assert not scenarios[case]["calls"][f"200:{kind}"]
    if kind == "client":
        assert renderer.objid_window.status == "protected"
        assert not any(t.startswith("200:window:open:") for t in trace)
    else:
        assert renderer.objid_window.state == 0x20000000 and renderer.objid_window.name is None
    assert scenarios[case]["acquired_count"] == scenarios[case]["closes"]


def test_dual_names_opt_in_and_read_only_surfaces_have_no_actions_values_or_pointers(scenarios):
    data = report(scenarios, "dual_names")
    assert client_renderer(data).objid_client.name and client_renderer(data).objid_window.name
    hidden = report(scenarios, "dual_tree")
    assert all(p.objid_client.name is None and p.objid_window.name is None for p in hidden.client_objects.checks)
    text = data.model_dump_json()
    assert "PRIVATE COM ADDRESS" not in text and "pointer" not in text
    combined = "\n".join(Path(f"backend/app/windows/{name}.cs").read_text(encoding="utf-8") for name in ("NativeDiscovery", "Ia2Discovery", "ClientDiscovery"))
    assert not re.search(r"\b(?:doAction|scrollTo|scrollToPoint|accDoDefaultAction|accSelect|SetFocus|setValue|get_accValue|accValue|attributes|SendInput|SetClipboardData|WriteProcessMemory|CreateRemoteThread)\s*\(", combined)


@pytest.mark.parametrize("mutate", [
    lambda d: d["client_objects"]["checks"][0]["objid_client"].update(object_id=0),
    lambda d: d["client_objects"]["checks"][0]["objid_window"].update(ia2_available=True),
    lambda d: d["client_objects"]["checks"][0]["objid_client"].update(name="PRIVATE"),
    lambda d: d["client_objects"].update(visible_renderer_client_nodes=0),
    lambda d: d["client_objects"]["window_msaa"]["nodes"][0].update(object_id=-4),
    lambda d: d["msaa_client"]["nodes"][0].update(object_id=0, source_kind="objid_window"),
])
def test_dual_schema_rejects_mixed_sources_private_names_and_false_summary(scenarios, mutate):
    data = report(scenarios, "dual_tree").model_dump()
    mutate(data)
    with pytest.raises(ValidationError): DiscoveryReport.model_validate(data)


def test_dual_scope_changes_do_not_fall_back_to_window_source(scenarios):
    data = report(scenarios, "dual_scope_after")
    assert "ia2_scope_changed" in data.errors
    assert client_renderer(data).objid_window.status == "out_of_scope"
    assert not any(t.startswith("200:window:open:") for t in scenarios["dual_scope_after"]["trace"])


def test_client_cli_saves_distinct_sources_and_worker_compiles_fixed_native_bridge(scenarios, tmp_path, monkeypatch, capsys, worker_platform):
    data = report(scenarios, "dual_tree")
    def runner(argv, **kwargs):
        command = base64.b64decode(argv[-1]).decode("utf-16-le")
        assert "ClientDiscovery.cs" in command and "'client',256,500,12,$false,20.0)" in command
        return SimpleNamespace(returncode=0, stdout=data.model_dump_json().encode())
    assert collect(backend="client", process_id=11300, runner=runner).client_objects is not None
    monkeypatch.setattr("app.windows.native_discovery.collect", lambda **kwargs: data)
    monkeypatch.setattr("app.windows.probe.save_report", lambda value, instance: save_report(value, instance, root=tmp_path))
    assert cli(["--backend", "client", "--process-id", "11300"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["backend"] == "client" and output["msaa_nodes"] == len(data.msaa_client.nodes)
    assert "objid_client" not in output and output["physical_action"] is False
    assert DiscoveryReport.model_validate_json(Path(output["report"]).read_text(encoding="utf-8")).client_objects


def ax_renderer(data, hwnd=200):
    return next(c for c in data.chromium_accessibility_activation.checks if c.source_hwnd == hwnd)


@pytest.mark.parametrize("case,polls,changed", [("ax_expands", 1, True), ("ax_delayed", 3, True), ("ax_unchanged", 21, False)])
def test_chromium_activation_fresh_client_polling_and_comparison(scenarios, case, polls, changed):
    data = report(scenarios, case)
    assert data.schema_version == 7 and data.msaa is None and data.ia2 is None
    assert data.accessibility_activation_performed is True and data.physical_action is False
    check = ax_renderer(data)
    assert (check.message, check.honeypot_object_id, check.wparam_object_id, check.timeout_ms) == (61, 1, -4, 250)
    assert check.attempted and check.send_status == "completed" and check.poll_attempts == polls
    assert check.baseline_role == 10 and check.baseline_child_count == 0
    assert check.after_role == (15 if changed else 10) and check.after_child_count == (1 if changed else 0)
    assert check.tree_changed is changed and check.comparison_complete
    assert check.activation_elapsed_ms <= 2000
    trace = scenarios[case]["trace"]
    assert trace.index("200:open:-4:1") < trace.index("200:activate:11300:250") < trace.index("200:open:-4:2")
    assert trace.count("200:activate:11300:250") == trace.count("400:activate:11300:250") == 1
    queries = [t for t in trace if ":ia2:fresh:" in t]
    assert queries == ([f"200:ia2:fresh:{polls + 1}", f"400:ia2:fresh:{polls + 1}"] if changed else [])
    assert check.ia2.query_service_attempted is changed
    assert scenarios[case]["acquired"] == scenarios[case]["closes"] and scenarios[case]["api_closes"] == 1


@pytest.mark.parametrize("case,status,attempted", [("ax_foreign", "scope_rejected", False), ("ax_scope_changed", "scope_rejected", False),
    ("ax_class_changed", "scope_rejected", False), ("ax_timeout", "timeout", True), ("ax_disappears", "renderer_disappeared", True),
    ("ax_protected", "protected", False), ("ax_unknown_state", "unknown_protection", False)])
def test_chromium_scope_timeout_disappearance_and_protection_fail_closed(scenarios, case, status, attempted):
    data = report(scenarios, case)
    check = ax_renderer(data)
    assert check.send_status == status and check.attempted is attempted
    assert check.tree_changed is None and not check.comparison_complete
    assert not check.ia2.query_service_attempted
    trace = scenarios[case]["trace"]
    assert sum(t.startswith("200:activate:") for t in trace) == int(attempted)
    assert not any(t.startswith("300:activate:") for t in trace)
    if case == "ax_timeout": assert check.poll_attempts == 0 and "activation_failed" in data.errors
    if case in {"ax_protected", "ax_unknown_state"}:
        assert not any(":name:" in t or ":count:" in t for t in trace)
        assert data.accessibility_activation_performed is False
    assert scenarios[case]["acquired"] == scenarios[case]["closes"]


@pytest.mark.parametrize("mode", ["hwnd", "msaa", "client", "ia2"])
def test_standard_backends_do_not_activate_or_watch(scenarios, mode):
    data = report(scenarios, "ax_normal_" + mode)
    assert data.accessibility_activation_performed is False and data.chromium_accessibility_activation is None and data.focus_watch is None
    assert not any(":activate:" in t for t in scenarios["ax_normal_" + mode]["trace"])
    assert scenarios["ax_normal_" + mode]["api_closes"] == 0


def test_chromium_budget_and_deadline_never_claim_complete_comparison(scenarios):
    data = report(scenarios, "ax_budget")
    assert data.truncated and "msaa_node_limit" in data.errors
    assert len(data.chromium_accessibility_activation.before_msaa.nodes) + len(data.chromium_accessibility_activation.after_msaa.nodes) <= 2
    assert not ax_renderer(data, 400).comparison_complete and ax_renderer(data, 400).tree_changed is None
    deadline = report(scenarios, "ax_deadline")
    assert deadline.truncated and "timeout" in deadline.errors
    assert ax_renderer(deadline).poll_attempts == 0 and not ax_renderer(deadline, 400).attempted


def test_activation_check_cap_prioritizes_both_visible_renderers_and_names_stay_private(scenarios):
    data = report(scenarios, "ax_check_cap")
    assert len(data.chromium_accessibility_activation.checks) == 32 and "activation_check_limit" in data.errors
    assert [c.source_hwnd for c in data.chromium_accessibility_activation.checks[:2]] == [200, 400]
    assert sum(":activate:" in t for t in scenarios["ax_check_cap"]["trace"]) == 32
    named = report(scenarios, "ax_names").chromium_accessibility_activation
    assert all(n.name for s in (named.before_msaa, named.after_msaa) for n in s.nodes)
    hidden = report(scenarios, "ax_expands").chromium_accessibility_activation
    assert all(n.name is None for s in (hidden.before_msaa, hidden.after_msaa) for n in s.nodes)
    for case in ("ax_names", "ax_check_cap", "ax_negative", "focus_names"):
        text = report(scenarios, case).model_dump_json()
        assert all(private not in text for private in ("PRIVATE ADDRESS", "pointer", "0x76543210", "EXCEPTION"))
    pane = ax_renderer(report(scenarios, "ax_pane"))
    assert pane.after_role == 16 and not pane.ia2.query_service_attempted and pane.ia2.status == "not_queried_shell"


@pytest.mark.parametrize("case", ["ax_before_unavailable", "ax_after_unavailable"])
def test_unavailable_client_acquisition_is_distinct_from_snapshot_budget(scenarios, case):
    data = report(scenarios, case)
    check = ax_renderer(data)
    assert data.status == "unknown_ui_state" and data.unreadable > 0 and "msaa_unavailable" in data.errors
    assert "msaa_node_limit" not in data.errors and check.tree_changed is None
    assert check.attempted and check.send_status == "completed"
    assert getattr(check, "baseline_hresult_category" if case == "ax_before_unavailable" else "after_hresult_category") == "no_interface"
    assert scenarios[case]["acquired"] == scenarios[case]["closes"]


def test_negative_native_variants_resolve_exact_signed_ids_before_classification(scenarios):
    resolved = report(scenarios, "native_negative_object")
    negative = [n for n in resolved.msaa.nodes if n.original_child_kind == "negative"]
    assert len(negative) == 3 and all(n.owner_kind == "accessible_object" and n.child_id is None and n.role == 42 and n.child_resolution_category == "negative_accessible_object" for n in negative)
    assert scenarios["native_negative_object"]["calls"].count("child:-100005") == 3
    for case, category, hr in [("native_negative_failure", "unresolved_negative_child", "failed"), ("native_reserved_failure", "reserved_system_objid", "failed"), ("native_negative_null", "unresolved_negative_child", "s_false")]:
        data = report(scenarios, case)
        diagnostic = data.msaa.child_diagnostics[0]
        assert diagnostic.reason == category and diagnostic.hresult_category == hr and diagnostic.child_id_kind == "negative"
        assert "child:" + ("-4" if case == "native_reserved_failure" else "-100005") in scenarios[case]["calls"]
        assert "malformed_variant" not in [d.reason for d in data.msaa.child_diagnostics]
        if hr == "s_false": assert data.status == "discovered" and data.unreadable == 0
        else: assert data.unreadable > 0
        assert "-100005" not in data.model_dump_json() and "PRIVATE" not in data.model_dump_json()
    cycle = report(scenarios, "native_negative_cycle")
    assert cycle.truncated and "msaa_repeated" in cycle.errors
    assert "child:0" not in scenarios["native_self"]["calls"]
    assert "child:1" in scenarios["seven_ids"]["calls"]
    assert "child:1" in scenarios["simple_protected"]["calls"]
    activated = report(scenarios, "ax_negative")
    assert any(n.original_child_kind == "negative" for n in activated.chromium_accessibility_activation.after_msaa.nodes)


def test_secondary_visible_window_metadata_is_a_hypothesis_without_geometry_selector(scenarios):
    data = report(scenarios, "ax_unchanged")
    architecture = data.hwnd.architecture
    assert architecture.primary_main_window == 100 and architecture.primary_selection_basis == "first_selected_visible_widget"
    assert architecture.secondary_visible_chromium_windows == (101,) and architecture.secondary_renderer_hwnds == (400,)
    main, second = [w for w in data.hwnd.windows if w.depth == 0]
    assert not main.chat_window_candidate and second.chat_window_candidate and second.direct_renderer_hwnds == (400,)
    assert second.owner_hwnd == 100 and second.ancestor_root == 101 and second.ancestor_rootowner == 100
    assert second.style == 0x10C40000 and second.style_flags == ("visible", "caption", "thickframe")
    assert second.exstyle_flags == ("appwindow",) and second.client_rect and second.window_rect and second.z_order_index == 1 and second.cloaked == 0
    assert ax_renderer(data, 400).secondary_window and not ax_renderer(data).secondary_window
    hidden = report(scenarios, "ax_hidden")
    assert not hidden.hwnd.architecture.secondary_visible_chromium_windows
    assert all(not w.chat_window_candidate for w in hidden.hwnd.windows)


@pytest.mark.parametrize("case,status,descendant,caret", [("focus_inner", "available", True, False), ("focus_shell", "available", False, False),
    ("focus_failure", "unavailable", False, False), ("focus_caret", "available", True, True), ("focus_protected", "protected", False, False), ("focus_unknown", "read_failed", False, False)])
def test_focus_event_exact_metadata_filtering_and_caret(scenarios, case, status, descendant, caret):
    data = report(scenarios, case)
    watch = data.focus_watch
    assert watch.status == "complete" and watch.hook_mode == "out_of_context" and watch.requested_seconds == 15
    assert len(watch.events) == 1  # Foreign PID event excluded before any COM call.
    event = watch.events[0]
    assert (event.hwnd, event.root_hwnd, event.process_id) == (400, 101, 11300)
    assert event.status == status and event.accessible_descendant is descendant
    assert event.related_secondary_window and event.related_secondary_renderer
    assert event.name is None and event.id_object_category == "objid_client"
    assert event.gui.hwndCaret == (401 if caret else None) and (event.gui.rcCaret is not None) is caret
    assert scenarios[case]["event_opens"] == 1
    assert "event:400:-4:" + ("0" if case == "focus_shell" else "-100009") in scenarios[case]["trace"]
    assert scenarios[case]["acquired"] == scenarios[case]["closes"]
    if case == "focus_failure": assert event.hresult_category == "access_denied"
    if case in {"focus_protected", "focus_unknown"}: assert "focus:name" not in scenarios[case]["trace"] and "focus:count" not in scenarios[case]["trace"]


def test_focus_privacy_caps_foreign_gui_handles_and_secondary_descendants(scenarios):
    named = report(scenarios, "focus_names")
    assert named.focus_watch.events[0].name == "Label 0"
    assert not any("focus:name" == t for t in scenarios["focus_inner"]["trace"])
    foreign = report(scenarios, "focus_foreign_gui").focus_watch.events[0].gui
    assert foreign.hwndActive == 101 and foreign.hwndFocus is None and foreign.hwndCaret is None and foreign.rcCaret is None
    cap = report(scenarios, "focus_cap").focus_watch
    assert len(cap.events) == 64 and cap.dropped == 7 and scenarios["focus_cap"]["event_opens"] == 64
    descendant = report(scenarios, "focus_descendant").focus_watch.events[1]
    assert descendant.hwnd == 401 and descendant.related_secondary_renderer and descendant.related_secondary_window
    self_edit = report(scenarios, "focus_self_edit").focus_watch.events[0]
    assert self_edit.id_child_kind == "zero" and self_edit.role == 42 and self_edit.accessible_descendant


def test_native_activation_and_focus_fixed_read_only_api_contract():
    source = Path("backend/app/windows/ChromiumDiscovery.cs").read_text(encoding="utf-8")
    assert "SendMessageTimeout(new IntPtr(hwnd), 0x3D, new IntPtr(-4), new IntPtr(1), 0x0002 | 0x0020, 250, out ignored)" in source
    assert "SetWinEventHook(0x8005, 0x8005, IntPtr.Zero, callback, (uint)pid, 0, 0)" in source
    assert "AccessibleObjectFromEvent(new IntPtr(hwnd), unchecked((uint)objectId), unchecked((uint)childId), out accessible, out child)" in source
    assert "GetGUIThreadInfo(thread, ref info)" in source and "UnhookWinEvent(owned)" in source
    combined = source + "\n".join(p.read_text(encoding="utf-8") for p in Path("backend/app/windows").glob("*.cs"))
    assert not re.search(r"\b(?:SendMessage|PostMessage|SendInput|mouse_event|SetCursorPos|SetForegroundWindow|SetFocus|SetClipboardData|accDoDefaultAction|accSelect|get_accValue|accValue|setValue|WriteProcessMemory|CreateRemoteThread|doAction)\s*\(", combined)
    assert not re.search(r"328574|197318|459284|132028|344\s*,\s*640", source)


@pytest.mark.parametrize("mutate", [lambda d: d.update(accessibility_activation_performed=False),
    lambda d: d["chromium_accessibility_activation"]["checks"][0].update(message=62),
    lambda d: d["chromium_accessibility_activation"]["checks"][0].update(honeypot_object_id=2),
    lambda d: d["chromium_accessibility_activation"]["checks"][0].update(target_pid=555),
    lambda d: d["chromium_accessibility_activation"]["checks"][0].update(raw_pointer=1234),
    lambda d: d["hwnd"]["architecture"].update(primary_main_window=101),
    lambda d: d["hwnd"]["windows"][0].update(chat_window_candidate=True),
    lambda d: d["focus_watch"]["events"][0].update(name="PRIVATE"),
    lambda d: d["focus_watch"].update(hook_mode="in_context"),
    lambda d: d["focus_watch"]["events"][0].update(process_id=555),
    lambda d: d["focus_watch"]["events"][0]["gui"].update(rcCaret=(1, 2, 3, 4))])
def test_activation_focus_schema_rejects_unsafe_or_inconsistent_evidence(scenarios, mutate):
    data = report(scenarios, "focus_inner").model_dump()
    mutate(data)
    with pytest.raises(ValidationError): DiscoveryReport.model_validate(data)


def test_chromium_cli_readiness_private_report_and_fixed_worker(scenarios, worker_platform, tmp_path, monkeypatch, capsys):
    data = report(scenarios, "focus_inner")
    def runner(argv, **kwargs):
        command = base64.b64decode(argv[-1]).decode("utf-16-le")
        assert "ChromiumDiscovery.cs" in command and "'chromium-ax',256,500,12,$false,30.0,15)" in command
        return SimpleNamespace(returncode=0, stdout=data.model_dump_json().encode())
    assert collect(backend="chromium-ax", process_id=11300, timeout=30, watch_focus_seconds=15, runner=runner).focus_watch
    def native(**kwargs):
        assert kwargs["watch_focus_seconds"] == 15
        kwargs["on_focus_ready"]()
        return data
    monkeypatch.setattr("app.windows.native_discovery.collect", native)
    monkeypatch.setattr("app.windows.probe.save_report", lambda value, instance: save_report(value, instance, root=tmp_path))
    assert cli(["--backend", "chromium-ax", "--process-id", "11300", "--watch-focus-seconds", "15", "--timeout", "30"]) == 0
    ready, output = map(json.loads, capsys.readouterr().out.splitlines())
    assert ready == {"event": "live_studio_focus_watch_ready", "seconds": 15}
    assert output["accessibility_activation_performed"] is True and output["physical_action"] is False
    assert output["msaa_nodes"] == len(data.chromium_accessibility_activation.after_msaa.nodes)
    assert DiscoveryReport.model_validate_json(Path(output["report"]).read_text(encoding="utf-8")).focus_watch.events


@pytest.mark.parametrize("args", [["--watch-focus-seconds", "15"], ["--backend", "client", "--process-id", "11300", "--watch-focus-seconds", "15"],
    ["--backend", "chromium-ax", "--process-id", "11300", "--watch-focus-seconds", "31"],
    ["--backend", "chromium-ax", "--process-id", "11300", "--watch-focus-seconds", "15", "--timeout", "15"]])
def test_invalid_focus_cli_never_starts_worker(args, monkeypatch):
    monkeypatch.setattr("app.windows.native_discovery.collect", lambda **kwargs: pytest.fail("Unexpected native inspection"))
    with pytest.raises(SystemExit) as error: cli(args)
    assert error.value.code == 2


def test_worker_timeout_preserves_uncertainty_about_activation(worker_platform):
    def blocked(*a, **k): raise subprocess.TimeoutExpired("PRIVATE", 20)
    data = collect(backend="chromium-ax", process_id=11300, runner=blocked)
    assert data.status == "timeout" and data.accessibility_activation_performed is None and data.physical_action is False


def test_streamed_ready_marker_is_exact_once_and_stderr_is_discarded():
    from app.windows.native_discovery import _run_watch_worker
    ready = []
    code = "import sys;sys.stderr.write('PRIVATE EXCEPTION\\nLIVE_STUDIO_FOCUS_WATCH_READY\\nLIVE_STUDIO_FOCUS_WATCH_READY\\n');sys.stderr.flush();sys.stdout.buffer.write(b'{}')"
    result = _run_watch_worker([sys.executable, "-c", code], timeout=3, on_ready=lambda: ready.append(True), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert ready == [True] and result.returncode == 0 and result.stdout == b"{}"


def test_stream_worker_output_cap_and_timeout_settle_owned_child(monkeypatch):
    from app.windows.native_discovery import _run_watch_worker
    monkeypatch.setattr("app.windows.native_discovery.MAX_REPORT_BYTES", 1024)
    kwargs = dict(on_ready=lambda: None, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    result = _run_watch_worker([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x'*100000);sys.stdout.flush()"], timeout=3, **kwargs)
    assert result.returncode != 0 and len(result.stdout) <= 1024
    with pytest.raises(subprocess.TimeoutExpired):
        _run_watch_worker([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.1, **kwargs)
