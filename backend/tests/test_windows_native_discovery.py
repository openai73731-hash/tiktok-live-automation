"""PC-SEND-1B..1E: production serializers/contracts, fully fake UI/COM boundaries."""
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
    paths = [Path(p).absolute() for p in ("backend/app/windows/NativeDiscovery.cs", "backend/app/windows/Ia2Discovery.cs",
        "backend/tests/fixtures/windows/FakeDiscovery.cs", "backend/tests/fixtures/windows/FakeMsaaVariants.cs", "backend/tests/fixtures/windows/FakeNativeVariants.cs", "backend/tests/fixtures/windows/FakeIa2Probe.cs")]
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
    assert data.schema_version == 5 and data.backend == "msaa"
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
    ("native_bad_id", 3, "i4", "malformed_variant", "s_ok"),
    ("native_null_dispatch", 9, "dispatch", "malformed_variant", "s_ok"),
])
def test_bad_native_child_has_scalar_diagnostics_and_does_not_abort_siblings(scenarios, case, vt, category, reason, hr):
    data = report(scenarios, case)
    diagnostic = data.msaa.child_diagnostics[0]
    assert diagnostic.vt == vt and diagnostic.category == category
    assert diagnostic.reason == reason and diagnostic.hresult_category == hr
    assert diagnostic.source_hwnd == 100 and diagnostic.parent_id == 1
    assert diagnostic.child_index == (1 if case == "native_empty" else 0)
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
    assert all(d.child_id_kind == "negative" and d.reason == "malformed_variant" for d in negative.msaa.child_diagnostics)
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
