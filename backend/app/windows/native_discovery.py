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
    "msaa_unreadable", "msaa_unavailable", "msaa_location_unavailable", "process_limit",
    "process_metadata_unavailable", "process_not_found", "window_limit", "native_enumeration_limit",
    "native_unavailable", "worker_output_invalid", "timeout", "uia_incomplete"]


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
    owner_kind: Literal["container", "accessible_object"]
    process_id: int | None = Field(gt=0)
    role: int = Field(ge=0)
    state: int = Field(ge=0)
    child_count: int = Field(ge=0, le=2**31 - 1)
    name: str | None = Field(max_length=160)
    semantic_hint: Literal["edit", "text", "button", "document", "list", "listitem", "pane", "other", "protected"]
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


class HwndSection(FrozenModel):
    windows: tuple[HwndNode, ...] = Field(default=(), max_length=2000)


class MsaaSection(FrozenModel):
    nodes: tuple[MsaaNode, ...] = Field(default=(), max_length=2000)


class DiscoveryReport(FrozenModel):
    schema_version: Literal[2] = 2
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    backend: Literal["hwnd", "msaa", "all"]
    process_id: int = Field(gt=0)
    status: Literal["discovered", "window_not_found", "unknown_ui_state", "native_unavailable", "unsupported_platform", "timeout"]
    studio_version: str | None = Field(default=None, pattern=r"^[0-9]+(?:\.[0-9]+){1,3}$")
    names_included: bool = False
    processes: tuple[ProcessMetadata, ...] = Field(default=(), max_length=64)
    uia: ProbeReport | None = None
    hwnd: HwndSection = Field(default_factory=HwndSection)
    msaa: MsaaSection | None = None
    truncated: bool = False
    unreadable: int = Field(default=0, ge=0)
    errors: tuple[ErrorCode, ...] = Field(default=(), max_length=16)
    physical_action: Literal[False] = False

    @model_validator(mode="after")
    def private_tree(self):
        for section in (self.hwnd.windows, self.msaa.nodes if self.msaa else ()):
            if any(n.name is not None and (not self.names_included or n.protected_content) for n in section):
                raise ValueError("Protected names must be absent")
        seen = {}
        for node in self.msaa.nodes if self.msaa else ():
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
    if (backend not in {"hwnd", "msaa", "all"} or type(process_id) is not int or not 1 <= process_id <= 2**31 - 1 or
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
        source = str(Path(__file__).with_name("NativeDiscovery.cs")).replace("'", "''")
        # Path is our bundled source; all other substitutions are validated scalars.
        command = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); "
            "try { Add-Type -Path '" + source + "' -ReferencedAssemblies 'System.dll','Accessibility.dll'; "
            "[LiveStudioDiscovery.Entry]::Run(" + str(process_id) + ",'" + backend + "'," + str(max_hwnd) + "," +
            str(max_nodes) + "," + str(max_depth) + ",$" + str(bool(include_names)).lower() + ") | ConvertTo-Json -Depth 12 -Compress } "
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
