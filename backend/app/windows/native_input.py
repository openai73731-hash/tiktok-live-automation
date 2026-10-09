"""Lazy Win32 boundary. Construction/startup is inert; tests inject all native calls.

External WinEvents only. SendInput contains keyboard events exclusively, with a
guard before every Unicode scalar/Enter batch. No clipboard/focus/mouse API exists.
"""
import asyncio
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import sys
import threading

from app.windows.target import DesktopSnapshot, Window, TargetRejected

DWORD, LONG, WORD, PTR = C.c_uint32, C.c_int32, C.c_uint16, C.c_void_p


class Keyboard(C.Structure):
    _fields_ = [("vk", WORD), ("scan", WORD), ("flags", DWORD), ("time", DWORD), ("extra", C.c_size_t)]


class MouseLayout(C.Structure):
    # Union layout only, never populated or sent.
    _fields_ = [("x", LONG), ("y", LONG), ("data", DWORD), ("flags", DWORD), ("time", DWORD), ("extra", C.c_size_t)]


class Payload(C.Union):
    _fields_ = [("keyboard", Keyboard), ("layout", MouseLayout)]


class Input(C.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("kind", DWORD), ("payload", Payload)]


class Rect(C.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class Gui(C.Structure):
    _fields_ = [("size", DWORD), ("flags", DWORD), ("active", PTR), ("focus", PTR),
                ("capture", PTR), ("menu", PTR), ("move", PTR), ("caret", PTR), ("rect", Rect)]


def unicode_events(text):
    if not text or len(text) > 400 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise TargetRejected("invalid_text")
    # Strict encoder rejects lone surrogates. Each pair of UTF-16 surrogate units
    # stays in one guarded scalar batch, preserving their order.
    for scalar in text:
        raw = scalar.encode("utf-16-le", errors="strict")
        events = []
        for i in range(0, len(raw), 2):
            unit = int.from_bytes(raw[i:i + 2], "little")
            for flag in (4, 4 | 2):
                event = Input(kind=1); event.keyboard = Keyboard(0, unit, flag, 0, 0); events.append(event)
        yield events


class NativeInput:
    def __init__(self, config):
        self.config = config
        self.loaded = False

    def _load(self):
        if self.loaded: return
        if sys.platform != "win32" or self.config.executable_path is None: raise TargetRejected("not_configured")
        self.u = C.WinDLL("user32", use_last_error=True)
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.d = C.WinDLL("dwmapi", use_last_error=True)
        specs = {
            "GetWindowThreadProcessId": ([PTR, C.POINTER(DWORD)], DWORD), "GetAncestor": ([PTR, DWORD], PTR),
            "GetWindow": ([PTR, DWORD], PTR), "IsWindowVisible": ([PTR], C.c_int), "IsWindowEnabled": ([PTR], C.c_int),
            "GetClassNameW": ([PTR, W.LPWSTR, C.c_int], C.c_int), "GetWindowLongW": ([PTR, C.c_int], LONG),
            "GetForegroundWindow": ([], PTR), "GetGUIThreadInfo": ([DWORD, C.POINTER(Gui)], C.c_int),
            "GetAsyncKeyState": ([C.c_int], C.c_int16), "SendInput": ([DWORD, C.POINTER(Input), C.c_int], DWORD),
            "SetWinEventHook": ([DWORD, DWORD, PTR, PTR, DWORD, DWORD, DWORD], PTR), "UnhookWinEvent": ([PTR], C.c_int),
            "PeekMessageW": ([C.POINTER(W.MSG), PTR, DWORD, DWORD, DWORD], C.c_int), "DispatchMessageW": ([C.POINTER(W.MSG)], C.c_ssize_t),
        }
        for name, (args, result) in specs.items():
            fn = getattr(self.u, name); fn.argtypes, fn.restype = args, result
        self.enum_type = C.WINFUNCTYPE(C.c_int, PTR, C.c_ssize_t)
        self.event_type = C.WINFUNCTYPE(None, PTR, DWORD, PTR, LONG, LONG, DWORD, DWORD)
        self.u.EnumWindows.argtypes, self.u.EnumWindows.restype = [self.enum_type, C.c_ssize_t], C.c_int
        for name, args, result in (
            ("OpenProcess", [DWORD, C.c_int, DWORD], PTR), ("CloseHandle", [PTR], C.c_int),
            ("QueryFullProcessImageNameW", [PTR, DWORD, W.LPWSTR, C.POINTER(DWORD)], C.c_int),
            ("GetProcessTimes", [PTR, C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME)], C.c_int),
            ("CreateMutexW", [PTR, C.c_int, W.LPCWSTR], PTR), ("ReleaseMutex", [PTR], C.c_int)):
            fn = getattr(self.k, name); fn.argtypes, fn.restype = args, result
        self.d.DwmGetWindowAttribute.argtypes = [PTR, DWORD, PTR, DWORD]
        self.d.DwmGetWindowAttribute.restype = LONG
        self.loaded = True

    def pid(self, hwnd):
        pid = DWORD(); self.u.GetWindowThreadProcessId(hwnd, C.byref(pid)); return pid.value

    def identity(self, pid):
        handle = self.k.OpenProcess(0x1000, False, pid)
        if not handle: raise TargetRejected("process_unavailable")
        try:
            path, length = C.create_unicode_buffer(32768), DWORD(32768)
            times = [W.FILETIME() for _ in range(4)]
            if not self.k.QueryFullProcessImageNameW(handle, 0, path, C.byref(length)) or not self.k.GetProcessTimes(handle, *(C.byref(t) for t in times)):
                raise TargetRejected("process_unavailable")
            if str(Path(path.value).resolve()).casefold() != str(self.config.executable_path.resolve()).casefold():
                raise TargetRejected("process_mismatch")
            return f"{pid}-{times[0].dwHighDateTime}-{times[0].dwLowDateTime}"
        finally: self.k.CloseHandle(handle)

    def snapshot(self):
        self._load()
        handles, scanned = [], 0
        def visit(hwnd, unused):
            nonlocal scanned
            scanned += 1
            if scanned > 512: return False
            handles.append(int(hwnd)); return True
        callback = self.enum_type(visit)
        if not self.u.EnumWindows(callback, 0): raise TargetRejected("window_scan_incomplete")
        pids = {self.pid(h) for h in handles}
        candidates = []
        for pid in pids:
            if pid == 0 or self.config.process_id and pid != self.config.process_id: continue
            try: candidates.append((pid, self.identity(pid)))
            except TargetRejected: continue
        if len(candidates) != 1: raise TargetRejected("ambiguous_process")
        pid, instance = candidates[0]
        windows = []
        def row(hwnd, parent=0):
            cls = C.create_unicode_buffer(161)
            if not self.u.GetClassNameW(hwnd, cls, 161): raise TargetRejected("window_destroyed")
            cloaked = DWORD()
            cloak = cloaked.value if self.d.DwmGetWindowAttribute(hwnd, 14, C.byref(cloaked), 4) == 0 else None
            return Window(hwnd, self.pid(hwnd), cls.value, bool(self.u.IsWindowVisible(hwnd)), bool(self.u.IsWindowEnabled(hwnd)),
                          cloak, int(self.u.GetWindow(hwnd, 4) or 0), parent, bool(self.u.GetWindowLongW(hwnd, -20) & 8))
        for hwnd in handles:
            if self.pid(hwnd) != pid: continue
            windows.append(row(hwnd))
            child, seen = self.u.GetWindow(hwnd, 5), set()
            while child:
                child = int(child)
                if child in seen or len(seen) >= 128: raise TargetRejected("child_scan_incomplete")
                seen.add(child); windows.append(row(child, hwnd)); child = self.u.GetWindow(child, 2)
        fg = int(self.u.GetForegroundWindow() or 0)
        active = focus = 0
        if fg and self.pid(fg) == pid:
            owner = DWORD(); thread = self.u.GetWindowThreadProcessId(fg, C.byref(owner)); gui = Gui(size=C.sizeof(Gui))
            if not self.u.GetGUIThreadInfo(thread, C.byref(gui)): raise TargetRejected("focus_unavailable")
            active = int(self.u.GetAncestor(gui.active, 2) or 0) if gui.active and self.pid(gui.active) == pid else 0
            focus = int(self.u.GetAncestor(gui.focus, 2) or 0) if gui.focus and self.pid(gui.focus) == pid else 0
        foreground = int(self.u.GetAncestor(fg, 2) or 0) if fg and self.pid(fg) == pid else 0
        clear = not any(self.u.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0x0D))
        return DesktopSnapshot(pid, instance, tuple(windows), foreground, active, focus, clear)

    def emit(self, events):
        array = (Input * len(events))(*events)
        if self.u.SendInput(len(events), array, C.sizeof(Input)) != len(events): raise TargetRejected("partial_input")

    async def fill(self, text, guard):
        batches = list(unicode_events(text))  # Validate the entire text before input.
        for batch in batches:
            if not guard(): raise TargetRejected("guard_failed")
            self.emit(batch)
            await asyncio.sleep(0)

    async def enter(self, guard):
        if not guard(): raise TargetRejected("guard_failed")
        events = []
        for flag in (0, 2):
            event = Input(kind=1); event.keyboard = Keyboard(0x0D, 0, flag, 0, 0); events.append(event)
        self.emit(events)  # Single activation, no retry even on partial/zero return.

    def watch(self, target, notify, invalidate):
        self._load()
        return NativeWatch(self, target, notify, invalidate)


class NativeWatch:
    def __init__(self, native, target, notify, invalidate):
        self.stop = threading.Event(); self.ready = threading.Event(); self.failed = False
        def run():
            hooks, mutex = [], None
            def event(hook, kind, hwnd, obj, child, thread, stamp):
                if self.stop.is_set(): return
                destroyed = kind == 0x8001 and obj == 0 and child == 0 and int(hwnd or 0) in {target.root, target.renderer}
                # Cross-thread latch prevents input before the event loop handles loss.
                if destroyed or kind == 3 and int(hwnd or 0) != target.root: invalidate.set()
                # Chromium may expose only top-level focus. A fresh foreground
                # activation is also intent evidence, after GUI-root reinspection.
                gained = kind == 0x8005 or kind == 3 and int(hwnd or 0) == target.root
                notify("destroyed" if destroyed else "focus" if gained else "changed")
            callback = native.event_type(event)
            try:
                C.set_last_error(0)
                mutex = native.k.CreateMutexW(None, True, "Local\\TikTokLiveStudioSender-" + target.instance)
                if not mutex or C.get_last_error() == 183: raise TargetRejected("instance_busy")
                for first, last, pid in ((3, 3, 0), (0x8000, 0x800B, target.pid)):
                    hook = native.u.SetWinEventHook(first, last, None, C.cast(callback, PTR), pid, 0, 0)
                    if not hook: raise TargetRejected("hook_unavailable")
                    hooks.append(hook)
                self.ready.set()
                while not self.stop.wait(0.02):
                    msg = W.MSG(); count = 0
                    while count < 64 and native.u.PeekMessageW(C.byref(msg), None, 0, 0, 1):
                        native.u.DispatchMessageW(C.byref(msg)); count += 1
            except BaseException:
                self.failed = True; invalidate.set(); notify("failed")
            finally:
                self.ready.set()
                for hook in hooks:
                    if not native.u.UnhookWinEvent(hook): self.failed = True
                if mutex:
                    if not self.failed: native.k.ReleaseMutex(mutex)
                    native.k.CloseHandle(mutex)
        self.thread = threading.Thread(target=run, name="windows-external-focus-watch", daemon=True); self.thread.start()

    async def start(self):
        try:
            if not await asyncio.to_thread(self.ready.wait, 2) or self.failed: raise TargetRejected("watch_failed")
        except BaseException:
            await self.close(); raise

    async def close(self):
        self.stop.set()
        await asyncio.to_thread(self.thread.join, 2)
        if self.thread.is_alive() or self.failed: raise TargetRejected("watch_cleanup_failed")
