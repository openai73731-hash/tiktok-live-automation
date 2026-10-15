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
# Allow the device's idle-state diagnostic to finish before our acquisition bound.
HIERARCHY_TIMEOUT = 12
# Shared package; it does not identify an official installation or client variant.
PACKAGES = frozenset({"com.zhiliaoapp.musically"})
ASCII_PATTERN = r"[A-Za-z0-9 .,!?':;()\-]{1,400}"


def ascii_supported(text):
    return isinstance(text, str) and re.fullmatch(ASCII_PATTERN, text) is not None


class AdbCommand(StrEnum):
    DEVICES = "devices"
    PACKAGES = "packages"
    FOREGROUND = "foreground"
    FOREGROUND_WINDOW = "foreground_window"
    DUMP = "dump"
    CLEAR_HIERARCHY = "clear_hierarchy"
    HIERARCHY = "hierarchy"
    PACKAGE_INFO = "package_info"
    DEVICE_MODEL = "device_model"
    ANDROID_VERSION = "android_version"
    SELECT_ALL = "select_all"
    DELETE = "delete"
    TEXT = "text"
    TAB = "tab"
    ACTIVATE = "activate"  # Retired keyboard path; never accepted by production argv.
    TAP_SEND = "tap_send"


class AdbError(RuntimeError):
    def __init__(self): super().__init__("Android inspection unavailable")


class HierarchyUnavailable(ValueError):
    """Transport worked, but no usable current UI hierarchy was acquired."""


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
    def __init__(self, executable=None, *, timeout=10, hierarchy_timeout=HIERARCHY_TIMEOUT):
        candidate = executable or shutil.which("adb")
        self.executable = Path(candidate).resolve() if candidate else None
        if self.executable and (self.executable.name.lower() not in {"adb", "adb.exe"} or not self.executable.is_file()):
            self.executable = None
        self.timeout = timeout
        self.hierarchy_timeout = hierarchy_timeout
        self.processes = set()
        self.closed = False
        self.last_command_result = None

    def argv(self, command, *, serial=None, package=None, text=None, point=None, compressed=False):
        if not isinstance(command, AdbCommand) or self.executable is None: raise AdbError()
        if type(compressed) is not bool or compressed and command != AdbCommand.DUMP: raise AdbError()
        if text is not None and command != AdbCommand.TEXT: raise AdbError()
        if point is not None and command != AdbCommand.TAP_SEND: raise AdbError()
        args = [str(self.executable), "-H", "127.0.0.1", "-P", "5037"]
        if command == AdbCommand.DEVICES:
            if serial is not None or package is not None: raise AdbError()
            return args + ["devices", "-l"]
        if not serial or not re.fullmatch(SERIAL_PATTERN, serial): raise AdbError()
        args += ["-s", serial]
        commands = {
            AdbCommand.PACKAGES: ["shell", "pm", "list", "packages"],
            AdbCommand.FOREGROUND: ["shell", "dumpsys", "activity", "activities"],
            AdbCommand.FOREGROUND_WINDOW: ["shell", "dumpsys", "window", "windows"],
            AdbCommand.DUMP: ["shell", "uiautomator", "dump", *(["--compressed"] if compressed else []), DUMP_PATH],
            AdbCommand.CLEAR_HIERARCHY: ["shell", "rm", "-f", DUMP_PATH],
            AdbCommand.HIERARCHY: ["exec-out", "cat", DUMP_PATH],
            AdbCommand.DEVICE_MODEL: ["shell", "getprop", "ro.product.model"],
            AdbCommand.ANDROID_VERSION: ["shell", "getprop", "ro.build.version.release"],
            AdbCommand.SELECT_ALL: ["shell", "input", "keycombination", "113", "29"],
            AdbCommand.DELETE: ["shell", "input", "keyevent", "KEYCODE_DEL"],
        }
        if command == AdbCommand.PACKAGE_INFO:
            if package not in PACKAGES: raise AdbError()
            return args + ["shell", "dumpsys", "package", package]
        if package is not None: raise AdbError()
        if command == AdbCommand.TEXT:
            if not ascii_supported(text): raise AdbError()
            # adb shell joins argv on the DEVICE: quote there as well, not just on Windows.
            return args + ["shell", "input", "text", shlex.quote(text.replace(" ", "%s"))]
        if command == AdbCommand.TAP_SEND:
            if (not isinstance(point, tuple) or len(point) != 2 or
                    any(type(v) is not int or not 0 <= v <= 8192 for v in point)): raise AdbError()
            return args + ["shell", "input", "tap", *(str(v) for v in point)]
        if command not in commands: raise AdbError()
        return args + commands[command]

    async def run(self, command, *, serial=None, package=None, text=None, point=None, guard=None, compressed=False):
        result = dict(command=command.value if isinstance(command, AdbCommand) else "invalid",
            category="transport_error", exit_code=None, stdout_present=False, stderr_present=False,
            stderr_miui_uiautomator_noise=False)
        self.last_command_result = result
        if self.closed: raise AdbError()
        args = self.argv(command, serial=serial, package=package, text=text, point=point, compressed=compressed)
        # Do not forward API keys, proxy settings or remote ADB server overrides.
        permitted = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}
        env = {k: v for k, v in os.environ.items() if k.upper() in permitted}
        process = None
        spawn = None
        readers = []
        try:
            async with asyncio.timeout(self.hierarchy_timeout if command in {AdbCommand.DUMP, AdbCommand.HIERARCHY} else self.timeout):
                if command == AdbCommand.DUMP:
                    # Only our fixed ephemeral output. A zero-exit false success
                    # can never read a hierarchy left by an earlier attempt.
                    await self.run(AdbCommand.CLEAR_HIERARCHY, serial=serial)
                    self.last_command_result = result
                async def start():
                    if guard is not None and not guard():
                        result["category"] = "guard_rejected"
                        raise AdbError()
                    return await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL, env=env,
                        **({"creationflags": 0x08000000} if os.name == "nt" else {}))
                spawn = asyncio.create_task(start())
                process = await asyncio.shield(spawn)
                self.processes.add(process)
                async def read(stream, limit, name):
                    chunks, total = [], 0
                    while chunk := await stream.read(65536):
                        result[name + "_present"] = True
                        total += len(chunk)
                        if total > limit:
                            if command == AdbCommand.HIERARCHY and name == "stdout": raise HierarchyUnavailable()
                            raise AdbError()
                        chunks.append(chunk)
                    return b"".join(chunks)
                readers = [asyncio.create_task(read(process.stdout, 524288, "stdout")),
                           asyncio.create_task(read(process.stderr, 8192, "stderr")), asyncio.create_task(process.wait())]
                stdout, stderr, code = await asyncio.gather(*readers)
                result.update(exit_code=code, stdout_present=bool(stdout), stderr_present=bool(stderr))
                # Recognize only linker WARNING lines in uiautomator diagnostics.
                # This label never overrides exit status or exact UI readback.
                lines = stderr.decode("utf-8", errors="replace").splitlines()
                result["stderr_miui_uiautomator_noise"] = (command == AdbCommand.DUMP and bool(lines)
                    and all(line.startswith("WARNING: linker:") for line in lines))
                if (command == AdbCommand.DUMP and b"could not get idle state" in (stdout + stderr).lower()
                        or command == AdbCommand.HIERARCHY and code != 0 and DUMP_PATH.encode() in stderr
                        and b"no such file" in stderr.lower()):
                    result["category"] = "hierarchy_unavailable"
                    raise HierarchyUnavailable()
                if code != 0:
                    result["category"] = "exit_nonzero"
                    raise AdbError()
                result["category"] = "output_invalid"
                try: output = stdout.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    if command == AdbCommand.HIERARCHY:
                        result["category"] = "hierarchy_unavailable"
                        raise HierarchyUnavailable() from None
                    raise
                result["category"] = "success"
                if command == AdbCommand.FOREGROUND:
                    # MIUI's focused window can be AOD while the resumed/focused
                    # application is TikTok. Collect both within this timeout.
                    output += "\n" + await self.run(AdbCommand.FOREGROUND_WINDOW, serial=serial)
                return output
        except asyncio.CancelledError:
            result["category"] = "cancelled"
            raise
        except TimeoutError:
            result["category"] = "timeout"
            if command in {AdbCommand.DUMP, AdbCommand.HIERARCHY}:
                if process is not None and process.returncode is None:
                    try: process.kill()
                    except ProcessLookupError: pass
                    await process.wait()
                # A UI idle timeout is not evidence that ADB disconnected. A
                # separate bounded transport probe must still prove the device.
                devices = parse_devices(await self.run(AdbCommand.DEVICES))
                if any(d.serial == serial and d.state == "CONNECTED" for d in devices):
                    result["category"] = "hierarchy_unavailable"
                    self.last_command_result = result
                    raise HierarchyUnavailable() from None
            raise AdbError() from None
        except HierarchyUnavailable:
            result["category"] = "hierarchy_unavailable"
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
                    result["exit_code"] = process.returncode
                    self.processes.discard(process)
                await finish_owned(asyncio.create_task(cleanup(), name="owned-adb-cleanup"))

    async def aclose(self):
        self.closed = True
        for process in tuple(self.processes):
            if process.returncode is None:
                try: process.kill()
                except ProcessLookupError: pass
            await process.wait()
