"""Small bounded argv-only allowlist around the installed Android Debug Bridge."""
import asyncio
from enum import StrEnum
import os
from pathlib import Path
import re
import shutil
import shlex
from app.android.models import Device, SERIAL_PATTERN
from app.lifecycle import finish_owned

DUMP_PATH = "/data/local/tmp/tiktok-live-hierarchy.xml"
# Shared package; it does not identify an official installation or client variant.
PACKAGES = frozenset({"com.zhiliaoapp.musically"})
ASCII_PATTERN = r"[A-Za-z0-9 .,!?':;()\-]{1,400}"


def ascii_supported(text):
    return isinstance(text, str) and re.fullmatch(ASCII_PATTERN, text) is not None


class AdbCommand(StrEnum):
    DEVICES = "devices"
    PACKAGES = "packages"
    FOREGROUND = "foreground"
    DUMP = "dump"
    HIERARCHY = "hierarchy"
    PACKAGE_INFO = "package_info"
    DEVICE_MODEL = "device_model"
    ANDROID_VERSION = "android_version"
    SELECT_ALL = "select_all"
    DELETE = "delete"
    TEXT = "text"
    TAB = "tab"
    ACTIVATE = "activate"


class AdbError(RuntimeError):
    def __init__(self): super().__init__("Android inspection unavailable")


def parse_devices(output):
    lines = output.splitlines()
    if not lines or lines[0].strip() != "List of devices attached": raise AdbError()
    devices = []
    for line in lines[1:]:
        if not line.strip(): continue
        parts = line.split()
        if len(parts) < 2 or not re.fullmatch(SERIAL_PATTERN, parts[0]): raise AdbError()
        state = {"device": "CONNECTED", "unauthorized": "UNAUTHORIZED", "offline": "OFFLINE"}.get(parts[1])
        if state is None or any(d.serial == parts[0] for d in devices) or len(devices) >= 64: raise AdbError()
        devices.append(Device(serial=parts[0], state=state))
    return tuple(devices)


class AdbTransport:
    def __init__(self, executable=None, *, timeout=10):
        candidate = executable or shutil.which("adb")
        self.executable = Path(candidate).resolve() if candidate else None
        if self.executable and (self.executable.name.lower() not in {"adb", "adb.exe"} or not self.executable.is_file()):
            self.executable = None
        self.timeout = timeout
        self.processes = set()
        self.closed = False

    def argv(self, command, *, serial=None, package=None, text=None):
        if not isinstance(command, AdbCommand) or self.executable is None: raise AdbError()
        if text is not None and command != AdbCommand.TEXT: raise AdbError()
        args = [str(self.executable), "-H", "127.0.0.1", "-P", "5037"]
        if command == AdbCommand.DEVICES:
            if serial is not None or package is not None: raise AdbError()
            return args + ["devices", "-l"]
        if not serial or not re.fullmatch(SERIAL_PATTERN, serial): raise AdbError()
        args += ["-s", serial]
        commands = {
            AdbCommand.PACKAGES: ["shell", "pm", "list", "packages"],
            AdbCommand.FOREGROUND: ["shell", "dumpsys", "activity", "activities"],
            AdbCommand.DUMP: ["shell", "uiautomator", "dump", DUMP_PATH],
            AdbCommand.HIERARCHY: ["exec-out", "cat", DUMP_PATH],
            AdbCommand.DEVICE_MODEL: ["shell", "getprop", "ro.product.model"],
            AdbCommand.ANDROID_VERSION: ["shell", "getprop", "ro.build.version.release"],
            AdbCommand.SELECT_ALL: ["shell", "input", "keycombination", "113", "29"],
            AdbCommand.DELETE: ["shell", "input", "keyevent", "KEYCODE_DEL"],
            AdbCommand.TAB: ["shell", "input", "keyevent", "KEYCODE_TAB"],
            AdbCommand.ACTIVATE: ["shell", "input", "keyevent", "KEYCODE_DPAD_CENTER"],
        }
        if command == AdbCommand.PACKAGE_INFO:
            if package not in PACKAGES: raise AdbError()
            return args + ["shell", "dumpsys", "package", package]
        if package is not None: raise AdbError()
        if command == AdbCommand.TEXT:
            if not ascii_supported(text): raise AdbError()
            # adb shell joins argv on the DEVICE: quote there as well, not just on Windows.
            return args + ["shell", "input", "text", shlex.quote(text.replace(" ", "%s"))]
        return args + commands[command]

    async def run(self, command, *, serial=None, package=None, text=None, guard=None):
        if self.closed: raise AdbError()
        args = self.argv(command, serial=serial, package=package, text=text)
        # Do not forward API keys, proxy settings or remote ADB server overrides.
        permitted = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}
        env = {k: v for k, v in os.environ.items() if k.upper() in permitted}
        process = None
        spawn = None
        readers = []
        try:
            async with asyncio.timeout(self.timeout):
                async def start():
                    if guard is not None and not guard(): raise AdbError()
                    return await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL, env=env,
                        **({"creationflags": 0x08000000} if os.name == "nt" else {}))
                spawn = asyncio.create_task(start())
                process = await asyncio.shield(spawn)
                self.processes.add(process)
                async def read(stream, limit):
                    chunks, total = [], 0
                    while chunk := await stream.read(65536):
                        total += len(chunk)
                        if total > limit: raise AdbError()
                        chunks.append(chunk)
                    return b"".join(chunks)
                readers = [asyncio.create_task(read(process.stdout, 524288)),
                           asyncio.create_task(read(process.stderr, 8192)), asyncio.create_task(process.wait())]
                stdout, _, code = await asyncio.gather(*readers)
                if code != 0: raise AdbError()
                return stdout.decode("utf-8", errors="strict")
        except asyncio.CancelledError:
            raise
        except Exception:
            raise AdbError() from None
        finally:
            if spawn is not None:
                async def cleanup():
                    nonlocal process
                    if process is None:
                        try: process = await spawn
                        except Exception: return
                    if process.returncode is None:
                        try: process.kill()
                        except ProcessLookupError: pass
                    for task in readers:
                        if not task.done(): task.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
                    await process.wait()
                    self.processes.discard(process)
                await finish_owned(asyncio.create_task(cleanup(), name="owned-adb-cleanup"))

    async def aclose(self):
        self.closed = True
        for process in tuple(self.processes):
            if process.returncode is None:
                try: process.kill()
                except ProcessLookupError: pass
            await process.wait()
