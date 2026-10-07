"""ADB inspection and a separately gated semantic UI action boundary."""
import asyncio
import re
from app.android.adb import AdbTransport, AdbCommand, AdbError, PACKAGES, parse_devices, DUMP_PATH
from app.android.models import ClientVariant, DeviceInspection, ScreenState, AndroidOutcome
from app.android.tiktok_ui import foreground, inspect_hierarchy, exact_input, observed_variant, package_version
from app.android.diagnostics import CaptureError, save_capture


class ADBAndroidController:
    def __init__(self, transport=None, *, serial=None, physical_enabled=False, profiles=(), actions=None):
        self.transport = transport or AdbTransport()
        self.serial = serial
        self.physical_enabled = physical_enabled
        self.profiles = tuple(profiles)  # No unverified real-device selector guesses are shipped.
        self.actions = actions
        self.lock = asyncio.Lock()
        self.last = DeviceInspection()
        self.closed = False
        self._profile = None
        self._xml = ""

    async def _inspect(self, target=None):
        self._profile, self._xml = None, ""
        if self.closed or self.transport.executable is None:
            self.last = DeviceInspection(reason="adb_unavailable")
            return self.last
        values = dict(adb="AVAILABLE")
        try:
            devices = parse_devices(await self.transport.run(AdbCommand.DEVICES))
            values["devices"] = devices
            selected = next((d for d in devices if d.serial == self.serial), None) if self.serial else None
            if selected is None and self.serial is None and len(devices) == 1: selected = devices[0]
            if selected is None:
                multiple = self.serial is None and len(devices) > 1
                self.last = DeviceInspection(**values, device="MULTIPLE_DEVICES" if multiple else "MISSING",
                    reason="multiple_devices" if multiple else "device_missing")
                return self.last
            values.update(serial=selected.serial, device=selected.state)
            if selected.state != "CONNECTED":
                self.last = DeviceInspection(**values, reason="unauthorized" if selected.state == "UNAUTHORIZED" else "offline")
                return self.last
            listed = await self.transport.run(AdbCommand.PACKAGES, serial=selected.serial)
            installed = {line.removeprefix("package:").strip() for line in listed.splitlines() if line.startswith("package:")}
            before = await self.transport.run(AdbCommand.FOREGROUND, serial=selected.serial)
            package, activity = foreground(before)
            if package not in PACKAGES or package not in installed:
                self.last = DeviceInspection(**values, screen=ScreenState.TIKTOK_NOT_RUNNING, reason="tiktok_not_running")
                return self.last
            values.update(package=package, foreground_activity=activity)
            dumped = await self.transport.run(AdbCommand.DUMP, serial=selected.serial)
            if DUMP_PATH not in dumped: raise AdbError()
            xml = await self.transport.run(AdbCommand.HIERARCHY, serial=selected.serial)
            info = await self.transport.run(AdbCommand.PACKAGE_INFO, serial=selected.serial, package=package)
            version, code = package_version(info)
            after = await self.transport.run(AdbCommand.FOREGROUND, serial=selected.serial)
            if foreground(after) != (package, activity): raise AdbError()
            variant = observed_variant(package, activity)
            values.update(version=version, version_code=code, client_variant=variant)
            profile = None
            matching = [p for p in self.profiles if p.package == package and p.verified and p.version == version
                and (p.client_variant == ClientVariant.UNKNOWN and variant == ClientVariant.UNKNOWN and not p.activities
                     or activity in p.activities and variant in {ClientVariant.UNKNOWN, p.client_variant})]
            if len(matching) == 1:
                profile = matching[0]
                values["client_variant"] = profile.client_variant
            parsed = inspect_hierarchy(xml, package, getattr(target, "account", None), profile)
            self._profile, self._xml = profile, xml
            parsed["interaction_available"] = self.actions is not None
            if parsed["reason"] == "ready" and self.actions is not None: values["device"] = "READY"
            self.last = DeviceInspection(**values, **parsed)
        except (AdbError, ValueError):
            self.last = DeviceInspection(**values, reason="adb_failed")
        return self.last

    async def inspect(self, target=None):
        async with self.lock:
            return await self._inspect(target)

    async def capture(self):
        async with self.lock:
            try:
                state = await self._inspect()
                if state.package is None or not self._xml or state.reason == "adb_failed": raise CaptureError()
                model = (await self.transport.run(AdbCommand.DEVICE_MODEL, serial=state.serial)).strip()
                android = (await self.transport.run(AdbCommand.ANDROID_VERSION, serial=state.serial)).strip()
                if not re.fullmatch(r"[A-Za-z0-9._ ()+-]{1,80}", model): raise CaptureError()
                if not re.fullmatch(r"[A-Za-z0-9._+-]{1,32}", android): raise CaptureError()
                info = await self.transport.run(AdbCommand.PACKAGE_INFO, serial=state.serial, package=state.package)
                after = await self.transport.run(AdbCommand.FOREGROUND, serial=state.serial)
                if (foreground(after) != (state.package, state.foreground_activity)
                        or package_version(info) != (state.version, state.version_code)): raise CaptureError()
                return save_capture(self._xml, state, model, android)
            except (AdbError, OSError, ValueError):
                raise CaptureError() from None

    async def submit(self, request, *, guard=lambda: False):
        if not self.physical_enabled:
            return AndroidOutcome(status="not_sent", reason="physical_delivery_disabled")
        async with self.lock:
            if not guard(): return AndroidOutcome(status="not_sent", reason="rejected")
            inspected = await self._inspect(request.target)
            if getattr(request.target, "device_serial", None) != inspected.serial:
                return AndroidOutcome(status="needs_operator", reason="device_unavailable")
            if inspected.security_state:
                return AndroidOutcome(status="needs_operator", reason="security_ui")
            if not inspected.ready:
                reason = inspected.reason if inspected.reason in {"account_unverified", "live_unverified", "profile_unverified"} else "unknown_screen"
                return AndroidOutcome(status="needs_operator", reason=reason)
            if not guard(): return AndroidOutcome(status="not_sent", reason="rejected")
            if not self.actions.supports(request.text):
                return AndroidOutcome(status="needs_operator", reason="unicode_entry_unavailable")
            profile = self._profile
            entered = await self.actions.prepare_exact(inspected.serial, profile.input_id, request.text)
            if entered != request.text: return AndroidOutcome(status="not_sent", reason="rejected")
            final = await self._inspect(request.target)
            if final.security_state:
                return AndroidOutcome(status="needs_operator", reason="security_ui")
            if (not final.ready or final.serial != inspected.serial or final.package != inspected.package
                    or self._profile != profile or not exact_input(self._xml, profile, request.text)):
                return AndroidOutcome(status="needs_operator", reason="unknown_screen")
            # No await between this authoritative check and the semantic action call.
            if not guard(): return AndroidOutcome(status="not_sent", reason="rejected")
            issued = await self.actions.click_send(final.serial, profile.send_id, guard)
            if issued is False: return AndroidOutcome(status="not_sent", reason="rejected")
            # Exit status is not delivery proof. No verified confirmation reader exists yet.
            return AndroidOutcome(status="unknown", reason="interrupted")

    async def aclose(self):
        self.closed = True
        try:
            await self.transport.aclose()
        finally:
            if self.actions is not None: await self.actions.aclose()
