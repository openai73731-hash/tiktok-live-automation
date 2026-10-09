"""Isolated stock .NET/Win32/MSAA read-only worker and versioned evidence schema."""
import base64
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
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
    "native_unavailable", "worker_output_invalid", "timeout", "uia_incomplete", "ia2_check_limit", "ia2_scope_changed", "activation_check_limit", "activation_failed", "focus_watch_incomplete"]


class HwndNode(FrozenModel):
    owner_hwnd: int | None = Field(default=None, gt=0)
    ancestor_root: int | None = Field(default=None, gt=0)
    ancestor_rootowner: int | None = Field(default=None, gt=0)
    style: int | None = Field(default=None, ge=0, le=2**32 - 1)
    exstyle: int | None = Field(default=None, ge=0, le=2**32 - 1)
    style_flags: tuple[Literal["popup", "child", "visible", "disabled", "caption", "thickframe", "sysmenu"], ...] = Field(default=(), max_length=7)
    exstyle_flags: tuple[Literal["topmost", "toolwindow", "windowedge", "appwindow", "noactivate"], ...] = Field(default=(), max_length=5)
    client_rect: tuple[int, int, int, int] | None = None
    window_rect: tuple[int, int, int, int] | None = None
    z_order_index: int | None = Field(default=None, ge=0, le=511)
    cloaked: int | None = Field(default=None, ge=0, le=7)
    direct_renderer_hwnds: tuple[int, ...] = Field(default=(), max_length=2000)
    chat_window_candidate: bool = False
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
    original_child_kind: Literal["positive", "negative"] | None = None
    child_resolution_category: Literal["negative_accessible_object"] | None = None
    source_kind: Literal["objid_client", "objid_window"] = "objid_client"
    object_id: Literal[-4, 0] = -4
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


class WindowArchitecture(FrozenModel):
    primary_main_window: int | None = Field(default=None, gt=0)
    primary_selection_basis: Literal["first_selected_visible_widget"] = "first_selected_visible_widget"
    secondary_visible_chromium_windows: tuple[int, ...] = Field(default=(), max_length=7)
    secondary_renderer_hwnds: tuple[int, ...] = Field(default=(), max_length=2000)


class HwndSection(FrozenModel):
    windows: tuple[HwndNode, ...] = Field(default=(), max_length=2000)
    root_statistics: RootStatistics = Field(default_factory=RootStatistics)
    selected_roots: tuple[RootTraversal, ...] = Field(default=(), max_length=8)
    architecture: WindowArchitecture = Field(default_factory=WindowArchitecture)


class ChildDiagnostic(FrozenModel):
    source_hwnd: int = Field(gt=0)
    parent_id: int | None = Field(ge=1, le=2000)
    child_index: int | None = Field(ge=0, le=1999)
    vt: int | None = Field(ge=0, le=65535)
    child_id_kind: Literal["zero", "positive", "negative", "invalid"] | None = None
    category: Literal["i4", "dispatch", "empty", "byref", "array", "other", "unavailable"]
    reason: Literal["unsupported_variant_type", "dispatch_qi_failed", "child_property_failed",
        "child_resolution_failed", "empty_slot", "partial_accessible_children", "malformed_variant", "childid_self_reference", "reserved_system_objid", "unresolved_negative_child"]
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
        "adapter_unavailable", "protected", "unknown_protection", "out_of_scope", "timeout", "not_queried_shell"]

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
    source_kind: Literal["objid_client"] = "objid_client"
    object_id: Literal[-4] = -4
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


class ObjectObservation(FrozenModel):
    source_kind: Literal["objid_client", "objid_window"]
    object_id: Literal[-4, 0]
    available: bool
    status: Literal["not_attempted", "available", "unavailable", "protected", "unknown_protection",
        "out_of_scope", "timeout", "capability_failed", "adapter_unavailable"]
    hresult_category: HresultCategory | None
    metadata_phase: Literal["before", "after", "window"] | None
    role: int | None = Field(ge=0)
    state: int | None = Field(ge=0)
    child_count: int | None = Field(ge=0, le=2**31 - 1)
    bounds: tuple[int, int, int, int] | None
    name: str | None = Field(max_length=160)
    iserviceprovider_available: bool | None
    provider_hresult_category: HresultCategory | None
    query_service_attempted: bool
    query_hresult_category: HresultCategory | None
    ia2_available: bool | None
    ia2_query_status: Literal["not_queried", "not_queried_window", "supported", "provider_unavailable", "ia2_unavailable",
        "capability_failed", "adapter_unavailable", "protected", "unknown_protection", "out_of_scope", "timeout"]

    @model_validator(mode="after")
    def contract(self):
        if self.object_id != (-4 if self.source_kind == "objid_client" else 0):
            raise ValueError("Invalid object source")
        if self.available and self.hresult_category != "s_ok":
            raise ValueError("Invalid object acquisition")
        if self.iserviceprovider_available is True and self.provider_hresult_category != "s_ok":
            raise ValueError("Invalid object provider")
        if self.source_kind == "objid_window" and (self.query_service_attempted or self.ia2_available is not None or self.ia2_query_status != "not_queried_window"):
            raise ValueError("WINDOW must not query IA2")
        if self.query_service_attempted and (not self.available or self.iserviceprovider_available is not True):
            raise ValueError("Invalid CLIENT query sequence")
        if self.ia2_available is True and (not self.query_service_attempted or self.query_hresult_category != "s_ok"):
            raise ValueError("Invalid CLIENT capability")
        return self


class ObjectPair(FrozenModel):
    source_hwnd: int = Field(gt=0)
    root_hwnd: int = Field(gt=0)
    source_process_id: int = Field(gt=0)
    source_visible: bool
    visible_root: bool
    chromium_kind: Literal["widget", "render_widget", "other"]
    objid_client: ObjectObservation
    objid_window: ObjectObservation


class ClientObjects(FrozenModel):
    primary_root_hwnd: int | None = Field(gt=0)
    checks: tuple[ObjectPair, ...] = Field(max_length=32)
    window_msaa: MsaaSection
    visible_renderer_client_available: bool
    visible_renderer_client_nodes: int = Field(ge=0, le=2000)
    visible_renderer_client_ia2_supported: bool


class AxCheck(FrozenModel):
    source_hwnd: int = Field(gt=0)
    root_hwnd: int = Field(gt=0)
    target_pid: int = Field(gt=0)
    message: Literal[61]
    honeypot_object_id: Literal[1]
    wparam_object_id: Literal[-4]
    timeout_ms: Literal[250]
    poll_attempts: int = Field(ge=0, le=21)
    visible: bool
    secondary_window: bool
    attempted: bool
    comparison_complete: bool
    send_status: Literal["not_attempted", "scope_rejected", "deadline", "protected", "unknown_protection", "completed", "timeout", "failed", "renderer_disappeared"]
    baseline_hresult_category: HresultCategory | None
    after_hresult_category: HresultCategory | None
    baseline_role: int | None = Field(ge=0)
    baseline_child_count: int | None = Field(ge=0)
    after_role: int | None = Field(ge=0)
    after_child_count: int | None = Field(ge=0)
    tree_changed: bool | None
    activation_elapsed_ms: int = Field(ge=0)
    ia2: Ia2Capability


class AxSection(FrozenModel):
    checks: tuple[AxCheck, ...] = Field(max_length=32)
    before_msaa: MsaaSection
    after_msaa: MsaaSection


class GuiEvidence(FrozenModel):
    hwndActive: int | None = Field(gt=0)
    hwndFocus: int | None = Field(gt=0)
    hwndCaret: int | None = Field(gt=0)
    rcCaret: tuple[int, int, int, int] | None
    flags: int | None = Field(ge=0, le=31)


class FocusRow(FrozenModel):
    hwnd: int = Field(gt=0)
    root_hwnd: int = Field(gt=0)
    process_id: int = Field(gt=0)
    id_object_category: Literal["objid_client", "objid_window", "reserved_system_objid", "custom_positive", "other_negative"]
    id_child_kind: Literal["zero", "positive", "negative"]
    accessible_kind: Literal["unavailable", "accessible_object", "simple_child"]
    hresult_category: HresultCategory | None
    status: Literal["unavailable", "available", "protected", "out_of_scope", "read_failed"]
    related_secondary_window: bool
    related_secondary_renderer: bool
    accessible_descendant: bool
    role: int | None = Field(ge=0)
    state: int | None = Field(ge=0)
    child_count: int | None = Field(ge=0)
    bounds: tuple[int, int, int, int] | None
    name: str | None = Field(max_length=160)
    gui: GuiEvidence


class FocusEvidence(FrozenModel):
    requested_seconds: int = Field(ge=0, le=30)
    dropped: int = Field(ge=0)
    status: Literal["disabled", "complete", "hook_failed", "deadline", "unhook_failed"]
    hook_mode: Literal["out_of_context"]
    events: tuple[FocusRow, ...] = Field(max_length=64)


class DiscoveryReport(FrozenModel):
    schema_version: Literal[7] = 7
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    backend: Literal["hwnd", "msaa", "all", "ia2", "client", "chromium-ax"]
    process_id: int = Field(gt=0)
    status: Literal["discovered", "window_not_found", "unknown_ui_state", "native_unavailable", "unsupported_platform", "timeout"]
    studio_version: str | None = Field(default=None, pattern=r"^[0-9]+(?:\.[0-9]+){1,3}$")
    names_included: bool = False
    processes: tuple[ProcessMetadata, ...] = Field(default=(), max_length=64)
    uia: ProbeReport | None = None
    hwnd: HwndSection = Field(default_factory=HwndSection)
    msaa: MsaaSection | None = None
    msaa_client: MsaaSection | None = None
    client_objects: ClientObjects | None = None
    ia2: Ia2Section | None = None
    chromium_accessibility_activation: AxSection | None = None
    focus_watch: FocusEvidence | None = None
    accessibility_activation_performed: bool | None = False
    truncated: bool = False
    unreadable: int = Field(default=0, ge=0)
    errors: tuple[ErrorCode, ...] = Field(default=(), max_length=16)
    physical_action: Literal[False] = False

    @model_validator(mode="after")
    def private_tree(self):
        windows = {w.hwnd: w for w in self.hwnd.windows}
        visible_widgets = [w for w in self.hwnd.windows if w.depth == 0 and w.process_id == self.process_id and w.chromium_kind == "widget" and w.visible]
        architecture = self.hwnd.architecture
        secondary = [w.hwnd for w in visible_widgets[1:]]
        if architecture.primary_main_window != (visible_widgets[0].hwnd if visible_widgets else None) or list(architecture.secondary_visible_chromium_windows) != secondary:
            raise ValueError("Invalid window architecture")
        secondary_renderers = [w.hwnd for w in self.hwnd.windows if w.parent_hwnd in secondary and w.chromium_kind == "render_widget" and w.visible]
        if list(architecture.secondary_renderer_hwnds) != secondary_renderers:
            raise ValueError("Invalid secondary renderers")
        for root in self.hwnd.windows:
            if root.depth != 0 or root.process_id != self.process_id or root.chromium_kind != "widget": continue
            direct = [w.hwnd for w in self.hwnd.windows if w.parent_hwnd == root.hwnd and w.chromium_kind == "render_widget"]
            if list(root.direct_renderer_hwnds) != direct or root.chat_window_candidate != (root.hwnd in secondary and any(windows[h].visible for h in direct)):
                raise ValueError("Invalid window hypothesis")
        if self.backend != "chromium-ax" and self.accessibility_activation_performed is not False:
            raise ValueError("Unexpected activation flag")
        after = self.msaa_client if self.backend == "client" else self.msaa
        msaa_sections = [after.nodes if after else ()]
        if any(n.source_kind != "objid_client" or n.object_id != -4 for n in msaa_sections[0]):
            raise ValueError("Invalid CLIENT tree")
        if self.ia2:
            if self.backend not in {"ia2", "client"} or after is None:
                raise ValueError("Invalid IA2 backend")
            if self.ia2.before_msaa_nodes != len(self.ia2.before_msaa.nodes) or self.ia2.after_msaa_nodes != len(after.nodes):
                raise ValueError("Invalid IA2 snapshot counts")
            msaa_sections.append(self.ia2.before_msaa.nodes)
            if any(n.source_kind != "objid_client" or n.object_id != -4 for n in self.ia2.before_msaa.nodes):
                raise ValueError("Invalid CLIENT baseline")
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
        if self.client_objects:
            objects = self.client_objects
            if self.backend != "client" or self.ia2 is None or after is None or self.msaa is not None:
                raise ValueError("Invalid dual probe sections")
            if objects.primary_root_hwnd != self.ia2.primary_root_hwnd or len(objects.checks) != len(self.ia2.capability_checks):
                raise ValueError("Invalid dual probe scope")
            for pair, check in zip(objects.checks, self.ia2.capability_checks):
                if any(getattr(pair, f) != getattr(check, f) for f in ("source_hwnd", "root_hwnd", "source_process_id", "source_visible", "visible_root", "chromium_kind")):
                    raise ValueError("Invalid dual probe pair")
                if pair.objid_client.source_kind != "objid_client" or pair.objid_window.source_kind != "objid_window":
                    raise ValueError("Mixed object sources")
                for observation in (pair.objid_client, pair.objid_window):
                    if observation.name is not None and (not self.names_included or observation.state is None or observation.state & 0x20000000):
                        raise ValueError("Protected object name")
            renderers = [p for p in objects.checks if p.root_hwnd == objects.primary_root_hwnd and p.visible_root and p.source_visible and p.chromium_kind == "render_widget"]
            if objects.visible_renderer_client_available != any(p.objid_client.available for p in renderers):
                raise ValueError("Invalid CLIENT availability")
            if objects.visible_renderer_client_ia2_supported != any(p.objid_client.ia2_available is True for p in renderers):
                raise ValueError("Invalid CLIENT IA2 summary")
            if objects.visible_renderer_client_nodes != sum(n.source_hwnd in {p.source_hwnd for p in renderers} for n in after.nodes):
                raise ValueError("Invalid CLIENT node count")
            if any(n.source_kind != "objid_window" or n.object_id != 0 for n in objects.window_msaa.nodes):
                raise ValueError("Invalid WINDOW tree")
            msaa_sections.append(objects.window_msaa.nodes)
        elif self.msaa_client is not None:
            raise ValueError("Missing dual probe context")
        if self.chromium_accessibility_activation:
            activation = self.chromium_accessibility_activation
            if self.backend != "chromium-ax" or self.msaa is not None or self.focus_watch is None:
                raise ValueError("Invalid activation backend")
            if len({c.source_hwnd for c in activation.checks}) != len(activation.checks):
                raise ValueError("Repeated activation")
            if self.accessibility_activation_performed != any(c.attempted for c in activation.checks):
                raise ValueError("Invalid activation flag")
            windows = {w.hwnd: w for w in self.hwnd.windows}
            for check in activation.checks:
                w = windows.get(check.source_hwnd)
                if w is None or w.chromium_kind != "render_widget" or check.target_pid != self.process_id or w.root_hwnd != check.root_hwnd:
                    raise ValueError("Invalid activation source")
                if check.attempted and w.process_id != self.process_id:
                    raise ValueError("Foreign activation")
                if check.comparison_complete != (check.tree_changed is not None):
                    raise ValueError("Invalid activation comparison")
                if check.secondary_window != (check.root_hwnd in secondary):
                    raise ValueError("Invalid secondary activation context")
                if check.ia2.query_service_attempted and check.after_role not in {14, 15}:
                    raise ValueError("IA2 queried shell")
            if any(n.source_kind != "objid_client" or n.object_id != -4 for s in (activation.before_msaa, activation.after_msaa) for n in s.nodes):
                raise ValueError("Invalid activation CLIENT tree")
            msaa_sections.extend((activation.before_msaa.nodes, activation.after_msaa.nodes))
            for event in self.focus_watch.events:
                if event.process_id != self.process_id or event.root_hwnd not in windows or windows[event.root_hwnd].depth != 0:
                    raise ValueError("Foreign focus event")
                if event.name is not None and (not self.names_included or event.state is None or event.state & 0x20000000):
                    raise ValueError("Protected focus name")
                if event.gui.hwndCaret is None and event.gui.rcCaret is not None:
                    raise ValueError("Invalid caret metadata")
                if event.related_secondary_window != (event.root_hwnd in secondary) or event.related_secondary_renderer and not event.related_secondary_window:
                    raise ValueError("Invalid focus architecture")
                if event.accessible_descendant and (event.status != "available" or event.role in {9, 10, None} or event.id_child_kind == "zero" and event.role not in {41, 42}):
                    raise ValueError("Invalid focus descendant")
        elif self.accessibility_activation_performed or self.focus_watch is not None:
            raise ValueError("Unexpected activation/focus")
        for section in (self.hwnd.windows, *msaa_sections):
            if any(n.name is not None and (not self.names_included or n.protected_content) for n in section):
                raise ValueError("Protected names must be absent")
        for section in msaa_sections:
            seen = {}
            for node in section:
                if (node.original_child_kind == "negative") != (node.child_resolution_category == "negative_accessible_object") or node.original_child_kind and node.owner_kind != "accessible_object":
                    raise ValueError("Invalid child origin")
                if node.object_id != (-4 if node.source_kind == "objid_client" else 0):
                    raise ValueError("Invalid node source")
                if (node.owner_kind == "simple_child") != (node.child_id is not None):
                    raise ValueError("Invalid MSAA child representation")
                if node.node_id in seen:
                    raise ValueError("Repeated MSAA node id")
                if node.parent_id is not None:
                    parent = seen.get(node.parent_id)
                    if parent is None or parent.protected_content or parent.source_hwnd != node.source_hwnd or parent.source_kind != node.source_kind:
                        raise ValueError("Invalid MSAA parent")
                seen[node.node_id] = node
        if self.status == "discovered" and (not self.hwnd.windows or self.truncated or self.unreadable):
            raise ValueError("Incomplete discovery cannot be reported as complete")
        if self.uia and (self.uia.names_included != self.names_included or self.uia.physical_action):
            raise ValueError("Invalid UIA evidence privacy")
        return self


def _run_watch_worker(argv, *, timeout, on_ready, **kwargs):
    """Stream one fixed readiness marker; retain bounded stdout and discard stderr."""
    with subprocess.Popen(argv, **kwargs) as process:
        chunks = []
        overflow = False
        def stdout_reader():
            nonlocal overflow
            size = 0
            while block := process.stdout.read(65536):
                size += len(block)
                if size > MAX_REPORT_BYTES:
                    overflow = True
                    process.kill()
                    break
                chunks.append(block)
        def stderr_reader():
            delivered = False
            while line := process.stderr.readline(4096):
                if not delivered and line.strip() == b"LIVE_STUDIO_FOCUS_WATCH_READY":
                    delivered = True
                    try: on_ready()
                    except Exception: pass
        readers = [threading.Thread(target=f, daemon=True) for f in (stdout_reader, stderr_reader)]
        for reader in readers: reader.start()
        try: process.wait(timeout=timeout)
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            for reader in readers: reader.join(timeout=2)
        return SimpleNamespace(returncode=process.returncode if not overflow else 1, stdout=b"".join(chunks))


def collect(*, backend, process_id, max_hwnd=256, max_nodes=500, max_depth=12,
            include_names=False, tree_view="control", timeout=20, runner=None, uia_probe=None,
            watch_focus_seconds=0, on_focus_ready=None):
    # Same native caps as CLI; validate before constructing an interpreter command.
    if (backend not in {"hwnd", "msaa", "all", "ia2", "client", "chromium-ax"} or type(process_id) is not int or not 1 <= process_id <= 2**31 - 1 or
        type(max_hwnd) is not int or not 1 <= max_hwnd <= 2000 or type(max_nodes) is not int or not 1 <= max_nodes <= 2000 or
        type(max_depth) is not int or not 1 <= max_depth <= 20 or not 1 <= timeout <= 60 or tree_view not in {"control", "raw"} or
        type(watch_focus_seconds) is not int or not 0 <= watch_focus_seconds <= 30 or
        watch_focus_seconds and (backend != "chromium-ax" or watch_focus_seconds >= timeout)):
        raise ValueError("invalid_argument")
    worker_started = False
    def failure(status, code=None):
        return DiscoveryReport(backend=backend, process_id=process_id, status=status,
            names_included=include_names, errors=(code,) if code else (),
            accessibility_activation_performed=None if backend == "chromium-ax" and worker_started else False)
    if sys.platform != "win32": return failure("unsupported_platform")
    deadline = monotonic() + timeout
    try:
        if not os.environ.get("SystemRoot"): return failure("native_unavailable", "native_unavailable")
        executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        sources = ",".join("'" + str(Path(__file__).with_name(name)).replace("'", "''") + "'" for name in ("NativeDiscovery.cs", "Ia2Discovery.cs", "ClientDiscovery.cs", "ChromiumDiscovery.cs"))
        # Path is our bundled source; all other substitutions are validated scalars.
        command = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); "
            "try { Add-Type -Path @(" + sources + ") -ReferencedAssemblies 'System.dll','Accessibility.dll'; "
            "[LiveStudioDiscovery.Entry]::Run(" + str(process_id) + ",'" + backend + "'," + str(max_hwnd) + "," +
            str(max_nodes) + "," + str(max_depth) + ",$" + str(bool(include_names)).lower() + "," + str(float(timeout)) +
            ("," + str(watch_focus_seconds) if backend == "chromium-ax" else "") + ") | ConvertTo-Json -Depth 12 -Compress } "
            "catch { '{\"status\":\"native_unavailable\"}' }")
        argv = [str(executable), "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand",
            base64.b64encode(command.encode("utf-16-le")).decode("ascii")]
        worker_started = True
        launch = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
        if runner is None and watch_focus_seconds and on_focus_ready:
            result = _run_watch_worker(argv, on_ready=on_focus_ready, **launch)
        else:
            result = (runner or subprocess.run)(argv, check=False, **launch)
        if result.returncode or len(result.stdout) > MAX_REPORT_BYTES: return failure("native_unavailable", "worker_output_invalid")
        if json.loads(result.stdout.decode("utf-8-sig")) == {"status": "native_unavailable"}:
            return failure("native_unavailable", "native_unavailable")
        report = DiscoveryReport.model_validate_json(result.stdout.decode("utf-8-sig"))
        if report.backend != backend or report.process_id != process_id or report.names_included != include_names:
            return failure("native_unavailable", "worker_output_invalid")
        if backend == "ia2" and (report.ia2 is None or report.ia2.before_msaa_nodes + report.ia2.after_msaa_nodes > max_nodes):
            return failure("native_unavailable", "worker_output_invalid")
        if backend == "client" and (report.ia2 is None or report.client_objects is None or report.msaa_client is None or
                report.ia2.before_msaa_nodes + report.ia2.after_msaa_nodes + len(report.client_objects.window_msaa.nodes) > max_nodes):
            return failure("native_unavailable", "worker_output_invalid")
        if backend == "chromium-ax" and (report.chromium_accessibility_activation is None or report.focus_watch is None or
                len(report.chromium_accessibility_activation.before_msaa.nodes) + len(report.chromium_accessibility_activation.after_msaa.nodes) > max_nodes or
                report.focus_watch.requested_seconds != watch_focus_seconds):
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
