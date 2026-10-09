"""Pure structural targeting; no geometry, title matching or platform I/O."""
from dataclasses import dataclass


class TargetRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class Window:
    hwnd: int
    pid: int
    class_name: str
    visible: bool = True
    enabled: bool = True
    cloaked: int | None = 0
    owner: int = 0
    parent: int = 0
    topmost: bool = False


@dataclass(frozen=True)
class DesktopSnapshot:
    pid: int
    instance: str
    windows: tuple[Window, ...]
    foreground_root: int = 0
    active_root: int = 0
    focus_root: int = 0
    modifiers_clear: bool = True


@dataclass(frozen=True)
class Target:
    pid: int
    instance: str
    primary: int
    root: int
    renderer: int


def resolve(snapshot: DesktopSnapshot) -> Target:
    roots = [w for w in snapshot.windows if not w.parent and w.visible]
    if len(roots) != 2 or any(w.pid != snapshot.pid or w.class_name != "Chrome_WidgetWin_1" or
                             not w.enabled or w.cloaked != 0 for w in roots):
        raise TargetRejected("unknown_topology")
    owned = [w for w in roots if any(other.owner == w.hwnd for other in roots if other != w)]
    main = owned if len(owned) == 1 else [w for w in roots if not w.topmost]
    if len(main) != 1: raise TargetRejected("ambiguous_primary")
    secondary = next(w for w in roots if w != main[0])
    renderers = [w for w in snapshot.windows if w.parent == secondary.hwnd and w.visible and
                 w.class_name == "Chrome_RenderWidgetHostHWND"]
    if len(renderers) != 1 or renderers[0].pid != snapshot.pid or not renderers[0].enabled:
        raise TargetRejected("ambiguous_renderer")
    return Target(snapshot.pid, snapshot.instance, main[0].hwnd, secondary.hwnd, renderers[0].hwnd)


def focused(snapshot: DesktopSnapshot, target: Target) -> bool:
    return (resolve(snapshot) == target and snapshot.modifiers_clear and
            snapshot.foreground_root == snapshot.active_root == snapshot.focus_root == target.root)
