"""Explicit bounded read-only UIA diagnostic, isolated from the web/AI runtime."""
import argparse
import base64
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from app.delivery.models import FrozenModel

MAX_REPORT_BYTES = 2_000_000


class ProbeNode(FrozenModel):
    node_id: int = Field(ge=1, le=2000)
    parent_id: int | None
    window_id: int = Field(ge=1, le=8)
    depth: int = Field(ge=0, le=20)
    process_id: int = Field(gt=0)
    native_handle: int
    control_type: str = Field(max_length=160)
    automation_id: str = Field(max_length=160)
    name: str | None = Field(max_length=160)
    class_name: str = Field(max_length=160)
    enabled: bool
    focusable: bool
    focused: bool
    offscreen: bool
    password: bool
    bounds: tuple[int, int, int, int] | None
    supported_patterns: tuple[str, ...] = Field(max_length=16)
    candidate_edit: bool
    candidate_button: bool
    candidate_chat_region: bool


class ProbeProcess(FrozenModel):
    process_id: int = Field(gt=0)
    name: str = Field(max_length=160)


class ProbeReport(FrozenModel):
    schema_version: Literal[1] = 1
    backend: Literal["uia"] = "uia"
    studio_version: str | None = None
    errors: tuple[Literal["uia_unavailable", "uia_incomplete", "timeout"], ...] = Field(default=(), max_length=16)
    physical_action: Literal[False] = False
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: Literal["discovered", "window_not_found", "unknown_ui_state", "uia_unavailable",
                    "unsupported_platform", "timeout"]
    tree_view: Literal["control", "raw"] = "control"
    names_included: bool = False
    processes: tuple[ProbeProcess, ...] = Field(default=(), max_length=64)
    windows: int = Field(default=0, ge=0, le=8)
    nodes: tuple[ProbeNode, ...] = Field(default=(), max_length=2000)
    truncated: bool = False
    unreadable: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def private_names(self):
        if any(node.name is not None and (node.password or not self.names_included) for node in self.nodes):
            raise ValueError("Protected names must be absent")
        if self.status == "discovered" and (not self.nodes or not self.windows or self.truncated or self.unreadable):
            raise ValueError("Incomplete discovery cannot be reported as complete")
        seen = {}
        for node in self.nodes:
            if node.node_id in seen:
                raise ValueError("Duplicate diagnostic node")
            if node.parent_id is not None:
                parent = seen.get(node.parent_id)
                if parent is None or parent.password or parent.window_id != node.window_id:
                    raise ValueError("Invalid diagnostic parent")
            seen[node.node_id] = node
        return self


def probe(*, process_id=0, max_nodes=500, max_depth=12, include_names=False,
          tree_view="control", timeout=20, runner=None) -> ProbeReport:
    """The child owns only UIA reads. Timeout terminates that child, never Studio."""
    if not (0 <= process_id <= 2**31 - 1 and 1 <= max_nodes <= 2000 and
            1 <= max_depth <= 20 and 1 <= timeout <= 60 and tree_view in {"control", "raw"}):
        raise ValueError("Invalid probe limits")
    def failure(status):
        return ProbeReport(status=status, tree_view=tree_view, names_included=include_names,
                           errors=(status,) if status in {"uia_unavailable", "timeout"} else ())
    if sys.platform != "win32":
        return failure("unsupported_platform")
    try:
        if not os.environ.get("SystemRoot"):
            return failure("uia_unavailable")
        executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        # Run only this bundled source and validated scalars. No generic command
        # parameter, downloaded code or execution-policy change is exposed.
        source = Path(__file__).with_name("probe_uia.ps1").read_text(encoding="utf-8")
        command = ("& {\n" + source + "\n} -TargetProcessId " + str(process_id) +
            " -MaxNodes " + str(max_nodes) + " -MaxDepth " + str(max_depth) +
            " -TreeView " + tree_view + " -IncludeNames:$" + str(bool(include_names)).lower())
        argv = [str(executable), "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand",
                base64.b64encode(command.encode("utf-16-le")).decode("ascii")]
        result = (runner or subprocess.run)(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode or len(result.stdout) > MAX_REPORT_BYTES:
            return failure("uia_unavailable")
        report = ProbeReport.model_validate_json(result.stdout.decode("utf-8-sig"))
        if report.status in {"uia_unavailable", "timeout"}:
            return failure(report.status)
        if report.names_included != include_names and report.nodes:
            return failure("uia_unavailable")
        if report.status == "unknown_ui_state":
            report = report.model_copy(update={"errors": ("uia_incomplete",)})
        return report
    except subprocess.TimeoutExpired:
        return failure("timeout")
    except (OSError, ValueError):
        return failure("uia_unavailable")


def save_report(report, instance_id: str, *, root=Path("diagnostics/windows")) -> Path:
    """Private local diagnostic, never a web projection or an ordinary runtime log."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", instance_id):
        raise ValueError("Invalid instance identifier")
    directory = root.absolute() / instance_id
    for part in (directory, *directory.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("Diagnostic directory must not be a link")
    directory.mkdir(parents=True, exist_ok=True)
    if len(tuple(directory.iterdir())) >= 20:
        raise ValueError("Diagnostic report limit reached")
    serialized = report.model_dump_json(indent=2)
    if len(serialized.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("report_size_limit")
    path = directory / (uuid4().hex + ".json")
    with path.open("x", encoding="utf-8") as stream:
        stream.write(serialized)
    return path


def cli(argv=None):
    def bounded(lower, upper, convert=int):
        def validate(value):
            try:
                parsed = convert(value)
                if lower <= parsed <= upper: return parsed
            except ValueError: pass
            raise argparse.ArgumentTypeError(f"invalid_argument: expected {lower}..{upper}")
        return validate
    parser = argparse.ArgumentParser(description="Read-only LIVE Studio accessibility discovery; never types or submits.")
    parser.add_argument("--backend", choices=("uia", "hwnd", "msaa", "all", "ia2"), default="uia")
    parser.add_argument("--process-id", type=bounded(1, 2**31 - 1), default=0, help="Your current Studio PID; required for hwnd/msaa/all/ia2.")
    parser.add_argument("--max-nodes", type=bounded(1, 2000), default=500)
    parser.add_argument("--max-hwnd", type=bounded(1, 2000), default=256)
    parser.add_argument("--max-depth", type=bounded(1, 20), default=12)
    parser.add_argument("--timeout", type=bounded(1, 60, float), default=20)
    parser.add_argument("--tree-view", choices=("control", "raw"), default="control")
    parser.add_argument("--include-names", action="store_true", help="Include private accessible names; redact before sharing.")
    parser.add_argument("--instance-id", default="default")
    args = parser.parse_args(argv)
    if args.backend != "uia" and args.process_id == 0:
        parser.error("invalid_argument: --process-id is required for hwnd/msaa/all/ia2")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", args.instance_id):
        parser.error("invalid_argument: invalid --instance-id")
    try:
        if args.backend == "uia":
            report = probe(process_id=args.process_id, max_nodes=args.max_nodes, max_depth=args.max_depth,
                           include_names=args.include_names, tree_view=args.tree_view, timeout=args.timeout)
        else:
            from app.windows.native_discovery import collect
            report = collect(backend=args.backend, process_id=args.process_id, max_hwnd=args.max_hwnd,
                max_nodes=args.max_nodes, max_depth=args.max_depth, include_names=args.include_names,
                tree_view=args.tree_view, timeout=args.timeout)
        path = save_report(report, args.instance_id)
    except (OSError, ValueError):
        print(json.dumps({"event": "live_studio_probe_failed", "reason": "probe_or_report_unavailable"}))
        return 2
    # Only bounded metadata reaches stdout. Raw names/IDs/tree stay in ignored files.
    uia = report if args.backend == "uia" else report.uia
    hwnd = () if args.backend == "uia" else report.hwnd.windows
    msaa = () if args.backend == "uia" or report.msaa is None else report.msaa.nodes
    print(json.dumps({"event": "live_studio_probe", "instance_id": args.instance_id, "backend": args.backend,
        "status": report.status, "windows": uia.windows if uia else sum(n.depth == 0 for n in hwnd),
        "nodes": (len(uia.nodes) if uia else 0) + len(msaa), "uia_nodes": len(uia.nodes) if uia else 0,
        "hwnd_count": len(hwnd), "msaa_nodes": len(msaa),
        "candidate_edits": sum(n.candidate_edit for n in uia.nodes) if uia else 0,
        "candidate_buttons": sum(n.candidate_button for n in uia.nodes) if uia else 0,
        "truncated": report.truncated, "unreadable": report.unreadable,
        "report": str(path), "physical_action": False}))
    return 0 if report.status == "discovered" else 2


if __name__ == "__main__":
    raise SystemExit(cli())
