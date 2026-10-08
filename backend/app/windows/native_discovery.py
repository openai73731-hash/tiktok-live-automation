"""Isolated stock .NET/Win32/MSAA read-only worker and versioned evidence schema."""
import base64
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic
from typing import Literal

from pydantic import Field, model_validator

from app.delivery.models import FrozenModel
from app.windows.probe import MAX_REPORT_BYTES, ProbeReport

ErrorCode = Literal["hwnd_limit", "hwnd_repeated", "hwnd_depth_limit", "hwnd_unreadable",
    "msaa_node_limit", "msaa_repeated", "msaa_depth_limit", "msaa_children_incomplete",
    "msaa_unreadable", "msaa_state_unavailable", "msaa_unavailable", "msaa_location_unavailable", "msaa_role_unavailable",
    "msaa_owner_unavailable", "msaa_name_unavailable", "msaa_child_count_unavailable",
    "msaa_child_object_unavailable", "msaa_children_unavailable", "msaa_child_unsupported", "process_limit",
    "process_metadata_unavailable", "process_not_found", "window_limit", "desktop_scan_limit",
    "target_root_limit", "target_hwnd_scan_limit", "process_scan_limit",
    "native_unavailable", "worker_output_invalid", "timeout", "uia_incomplete", "ia2_check_limit", "ia2_scope_changed"]


class HwndNode(FrozenModel):
    hwnd: int = Field(gt=0)
    parent_hwnd: int | None
    root_hwnd: int = Field(gt=0)
    depth: int = Field(ge=0, le=20)
    process_id: int = Field(gt=0)
    class_name: str = Field(max_length=160)
    name: str | None = Field(max_length=160)
    chromium_kind: Literal["widget", "render_widget", "other"] | None
    visible: bool
    enabled: bool
    protected_content: bool
    bounds: tuple[int, int, int, int]


class MsaaNode(FrozenModel):
    node_id: int = Field(ge=1, le=2000)
    parent_id: int | None
    depth: int = Field(ge=0, le=20)
    source_hwnd: int = Field(gt=0)
    owner_hwnd: int | None
    owner_kind: Literal["simple_child", "accessible_object"]
    child_id: int | None = Field(gt=0, le=2**31 - 1)
    process_id: int | None = Field(gt=0)
    role: int | None = Field(ge=0)
    state: int = Field(ge=0)
    child_count: int | None = Field(ge=0, le=2**31 - 1)
    name: str | None = Field(max_length=160)
    semantic_hint: Literal["edit", "text", "button", "document", "list", "listitem", "pane", "other", "protected", "unknown"]
    focusable: bool
    focused: bool
    disabled: bool
    invisible: bool
    offscreen: bool
    protected_content: bool
    bounds: tuple[int, int, int, int] | None


class ProcessMetadata(FrozenModel):
    process_id: int = Field(gt=0)
    parent_process_id: int = Field(ge=0)
    filename: str | None = Field(max_length=160)
    executable_path: str | None = Field(max_length=1024)
    has_windows: bool
    hwnds: tuple[int, ...] = Field(max_length=2000)


class RootStatistics(FrozenModel):
    target_roots_total: int = Field(default=0, ge=0, le=512)
    target_roots_selected: int = Field(default=0, ge=0, le=8)
    target_roots_skipped: int = Field(default=0, ge=0, le=512)
    enumeration_complete: bool = True

    @model_validator(mode="after")
    def counts(self):
        if self.target_roots_total != self.target_roots_selected + self.target_roots_skipped:
            raise ValueError("Invalid root counts")
        return self


class RootTraversal(FrozenModel):
    hwnd: int = Field(gt=0)
    visible: bool | None
    hwnd_traversal_complete: bool
    msaa_traversal_complete: bool | None


class HwndSection(FrozenModel):
    windows: tuple[HwndNode, ...] = Field(default=(), max_length=2000)
    root_statistics: RootStatistics = Field(default_factory=RootStatistics)
    selected_roots: tuple[RootTraversal, ...] = Field(default=(), max_length=8)


class ChildDiagnostic(FrozenModel):
    source_hwnd: int = Field(gt=0)
    parent_id: int | None = Field(ge=1, le=2000)
    child_index: int | None = Field(ge=0, le=1999)
    vt: int | None = Field(ge=0, le=65535)
    child_id_kind: Literal["zero", "positive", "negative", "invalid"] | None = None
    category: Literal["i4", "dispatch", "empty", "byref", "array", "other", "unavailable"]
    reason: Literal["unsupported_variant_type", "dispatch_qi_failed", "child_property_failed",
        "child_resolution_failed", "empty_slot", "partial_accessible_children", "malformed_variant", "childid_self_reference"]
    hresult_category: Literal["s_ok", "s_false", "no_interface", "invalid_argument",
        "access_denied", "disconnected", "failed", "unexpected_success"] | None
    requested: int | None = Field(ge=1, le=2000)
    obtained: int | None = Field(ge=0, le=2000)


class MsaaSection(FrozenModel):
    nodes: tuple[MsaaNode, ...] = Field(default=(), max_length=2000)
    child_diagnostics: tuple[ChildDiagnostic, ...] = Field(default=(), max_length=64)
    diagnostics_dropped: int = Field(default=0, ge=0)


HresultCategory = Literal["s_ok", "s_false", "no_interface", "invalid_argument", "access_denied", "disconnected", "failed", "unexpected_success"]


class Ia2Capability(FrozenModel):
    iserviceprovider_available: bool
    query_service_attempted: bool
    ia2_available: bool
    provider_hresult_category: HresultCategory | None
    query_hresult_category: HresultCategory | None
    status: Literal["supported", "provider_unavailable", "ia2_unavailable", "capability_failed",
        "adapter_unavailable", "protected", "unknown_protection", "out_of_scope", "timeout"]

    @model_validator(mode="after")
    def contract(self):
        if self.iserviceprovider_available and self.provider_hresult_category != "s_ok":
            raise ValueError("Invalid IServiceProvider capability")
        if self.query_service_attempted and not self.iserviceprovider_available:
            raise ValueError("Invalid IA2 query sequence")
        if self.ia2_available and (not self.query_service_attempted or self.query_hresult_category != "s_ok" or self.status != "supported"):
            raise ValueError("Invalid IA2 capability")
        return self


class Ia2Check(FrozenModel):
    check_id: int = Field(ge=1, le=32)
    source_hwnd: int = Field(gt=0)
    root_hwnd: int = Field(gt=0)
    owner_hwnd: int | None = Field(gt=0)
    source_process_id: int = Field(gt=0)
    process_id: int | None = Field(gt=0)
    source_visible: bool
    visible_root: bool
    chromium_kind: Literal["widget", "render_widget", "other"]
    capability: Ia2Capability
    before_msaa_nodes: int | None = Field(ge=0, le=2000)
    after_msaa_nodes: int | None = Field(ge=0, le=2000)
    before_child_count: int | None = Field(ge=0, le=2**31 - 1)
    after_child_count: int | None = Field(ge=0, le=2**31 - 1)
    comparison_complete: bool
    tree_changed_after_ia2_query: bool | None


class Ia2Section(FrozenModel):
    primary_root_hwnd: int | None = Field(gt=0)
    capability_checks: tuple[Ia2Check, ...] = Field(max_length=32)
    query_success_count: int = Field(ge=0, le=32)
    query_failure_count: int = Field(ge=0, le=32)
    visible_renderer_supported: bool
    visible_renderer_checked: bool
    before_msaa_nodes: int = Field(ge=0, le=2000)
    after_msaa_nodes: int = Field(ge=0, le=2000)
    comparison_complete: bool
    tree_changed_after_ia2_query: bool | None
    before_msaa: MsaaSection


class DiscoveryReport(FrozenModel):
    schema_version: Literal[5] = 5
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    backend: Literal["hwnd", "msaa", "all", "ia2"]
    process_id: int = Field(gt=0)
    status: Literal["discovered", "window_not_found", "unknown_ui_state", "native_unavailable", "unsupported_platform", "timeout"]
    studio_version: str | None = Field(default=None, pattern=r"^[0-9]+(?:\.[0-9]+){1,3}$")
    names_included: bool = False
    processes: tuple[ProcessMetadata, ...] = Field(default=(), max_length=64)
    uia: ProbeReport | None = None
    hwnd: HwndSection = Field(default_factory=HwndSection)
    msaa: MsaaSection | None = None
    ia2: Ia2Section | None = None
    truncated: bool = False
    unreadable: int = Field(default=0, ge=0)
    errors: tuple[ErrorCode, ...] = Field(default=(), max_length=16)
    physical_action: Literal[False] = False

    @model_validator(mode="after")
    def private_tree(self):
        msaa_sections = [self.msaa.nodes if self.msaa else ()]
        if self.ia2:
            if self.backend != "ia2" or self.msaa is None:
                raise ValueError("Invalid IA2 backend")
            if self.ia2.before_msaa_nodes != len(self.ia2.before_msaa.nodes) or self.ia2.after_msaa_nodes != len(self.msaa.nodes):
                raise ValueError("Invalid IA2 snapshot counts")
            msaa_sections.append(self.ia2.before_msaa.nodes)
            checks = self.ia2.capability_checks
            if len({c.check_id for c in checks}) != len(checks): raise ValueError("Repeated IA2 check")
            if self.ia2.query_success_count != sum(c.capability.ia2_available for c in checks): raise ValueError("Invalid IA2 success count")
            if self.ia2.query_failure_count != sum(c.capability.query_service_attempted and not c.capability.ia2_available for c in checks): raise ValueError("Invalid IA2 failure count")
            windows = {w.hwnd: w for w in self.hwnd.windows}
            renderers = [c for c in checks if c.root_hwnd == self.ia2.primary_root_hwnd and c.visible_root and c.source_visible and c.chromium_kind == "render_widget"]
            if self.ia2.visible_renderer_supported != any(c.capability.ia2_available for c in renderers):
                raise ValueError("Invalid renderer capability")
            for check in checks:
                if check.source_hwnd not in windows or windows[check.source_hwnd].root_hwnd != check.root_hwnd:
                    raise ValueError("Out of scope IA2 source")
                if (check.tree_changed_after_ia2_query is not None) != check.comparison_complete:
                    raise ValueError("Invalid IA2 comparison")
            if (self.ia2.tree_changed_after_ia2_query is not None) != self.ia2.comparison_complete:
                raise ValueError("Invalid aggregate IA2 comparison")
            complete = bool(checks) and all(c.comparison_complete for c in checks)
            if self.ia2.comparison_complete != complete:
                raise ValueError("Incomplete IA2 check set")
        for section in (self.hwnd.windows, *msaa_sections):
            if any(n.name is not None and (not self.names_included or n.protected_content) for n in section):
                raise ValueError("Protected names must be absent")
        for section in msaa_sections:
            seen = {}
            for node in section:
                if (node.owner_kind == "simple_child") != (node.child_id is not None):
                    raise ValueError("Invalid MSAA child representation")
                if node.node_id in seen:
                    raise ValueError("Repeated MSAA node id")
                if node.parent_id is not None:
                    parent = seen.get(node.parent_id)
                    if parent is None or parent.protected_content or parent.source_hwnd != node.source_hwnd:
                        raise ValueError("Invalid MSAA parent")
                seen[node.node_id] = node
        if self.status == "discovered" and (not self.hwnd.windows or self.truncated or self.unreadable):
            raise ValueError("Incomplete discovery cannot be reported as complete")
        if self.uia and (self.uia.names_included != self.names_included or self.uia.physical_action):
            raise ValueError("Invalid UIA evidence privacy")
        return self


def collect(*, backend, process_id, max_hwnd=256, max_nodes=500, max_depth=12,
            include_names=False, tree_view="control", timeout=20, runner=None, uia_probe=None):
    # Same native caps as CLI; validate before constructing an interpreter command.
    if (backend not in {"hwnd", "msaa", "all", "ia2"} or type(process_id) is not int or not 1 <= process_id <= 2**31 - 1 or
        type(max_hwnd) is not int or not 1 <= max_hwnd <= 2000 or type(max_nodes) is not int or not 1 <= max_nodes <= 2000 or
        type(max_depth) is not int or not 1 <= max_depth <= 20 or not 1 <= timeout <= 60 or tree_view not in {"control", "raw"}):
        raise ValueError("invalid_argument")
    def failure(status, code=None):
        return DiscoveryReport(backend=backend, process_id=process_id, status=status,
            names_included=include_names, errors=(code,) if code else ())
    if sys.platform != "win32": return failure("unsupported_platform")
    deadline = monotonic() + timeout
    try:
        if not os.environ.get("SystemRoot"): return failure("native_unavailable", "native_unavailable")
        executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        sources = ",".join("'" + str(Path(__file__).with_name(name)).replace("'", "''") + "'" for name in ("NativeDiscovery.cs", "Ia2Discovery.cs"))
        # Path is our bundled source; all other substitutions are validated scalars.
        command = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); "
            "try { Add-Type -Path @(" + sources + ") -ReferencedAssemblies 'System.dll','Accessibility.dll'; "
            "[LiveStudioDiscovery.Entry]::Run(" + str(process_id) + ",'" + backend + "'," + str(max_hwnd) + "," +
            str(max_nodes) + "," + str(max_depth) + ",$" + str(bool(include_names)).lower() + "," + str(float(timeout)) + ") | ConvertTo-Json -Depth 12 -Compress } "
            "catch { '{\"status\":\"native_unavailable\"}' }")
        argv = [str(executable), "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand",
            base64.b64encode(command.encode("utf-16-le")).decode("ascii")]
        result = (runner or subprocess.run)(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode or len(result.stdout) > MAX_REPORT_BYTES: return failure("native_unavailable", "worker_output_invalid")
        if json.loads(result.stdout.decode("utf-8-sig")) == {"status": "native_unavailable"}:
            return failure("native_unavailable", "native_unavailable")
        report = DiscoveryReport.model_validate_json(result.stdout.decode("utf-8-sig"))
        if report.backend != backend or report.process_id != process_id or report.names_included != include_names:
            return failure("native_unavailable", "worker_output_invalid")
        if backend == "ia2" and (report.ia2 is None or report.ia2.before_msaa_nodes + report.ia2.after_msaa_nodes > max_nodes):
            return failure("native_unavailable", "worker_output_invalid")
        if backend == "all":
            remaining = deadline - monotonic()
            if remaining < 1: return failure("timeout", "timeout")
            from app.windows.probe import probe
            uia = (uia_probe or probe)(process_id=process_id, max_nodes=max_nodes, max_depth=max_depth,
                include_names=include_names, tree_view=tree_view, timeout=remaining)
            error = uia.status != "discovered"
            report = report.model_copy(update={"uia": uia, "truncated": report.truncated or uia.truncated,
                "unreadable": report.unreadable + uia.unreadable,
                "status": "timeout" if uia.status == "timeout" else "unknown_ui_state" if error or uia.truncated or uia.unreadable else report.status,
                "errors": tuple(dict.fromkeys((*report.errors, "uia_incomplete")))[:16] if error else report.errors})
        return DiscoveryReport.model_validate(report.model_dump())
    except subprocess.TimeoutExpired: return failure("timeout", "timeout")
    except (OSError, ValueError): return failure("native_unavailable", "worker_output_invalid")
