"""ADB inspection and a separately gated semantic UI action boundary."""
import asyncio
import re
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4
from hashlib import sha256
from app.android.adb import AdbTransport, AdbCommand, AdbError, HierarchyUnavailable, PACKAGES, parse_devices, DUMP_PATH
from app.android.models import ClientVariant, DeviceInspection, ScreenState, AndroidOutcome
from app.android.tiktok_ui import (foreground, inspect_hierarchy, exact_input, unique_node, observed_variant, package_version,
    TIKTOK_YOU_4683, profile_account, parse_hierarchy, reviewed_profile_screen, composer_shape)
from app.android.diagnostics import CaptureError, save_capture
from app.android.manual_inspection import ManualInspection


class ADBAndroidController:
    def __init__(self, transport=None, *, serial=None, physical_enabled=False, profiles=None, actions=None, confirmation_interval=0.5):
        self.transport = transport or AdbTransport()
        self.serial = serial
        self.physical_enabled = physical_enabled
        self.profiles = (TIKTOK_YOU_4683,) if profiles is None else tuple(profiles)
        self.actions = actions
        if profiles is None and actions is None:
            from app.android.text_entry import ASCIITextEntryBackend
            self.actions = ASCIITextEntryBackend(self)
        self.confirmation_interval = max(0, min(1, confirmation_interval))
        self.lock = asyncio.Lock()
        self.last = DeviceInspection()
        self.closed = False
        self.on_stage = lambda stage: None
        self._profile = None
        self._xml = ""
        self._opening = None
        self.outgoing = None
        self.invalidate_trust()

    def invalidate_trust(self):
        self._account_scope_guard = None
        self._account_binding = self._send_binding = self._candidate_key = self._candidate_token = None
        self._live_evidence = None
        self._manual_fill_evidence = None
        self._installation = None
        self._host_snapshot = None

    @property
    def current_ready(self):
        if self._account_scope_guard is not None and not self._account_scope_guard(): return False
        if not self.last.ready and not self.opener_ready(): return False
        if self._profile and self._profile.live_markers:
            return bool(self._live_evidence and monotonic() - self._live_evidence[3] < 90
                and self._account_binding == self._send_binding and self._send_binding is not None)
        return True

    def ready_for(self, session_id, account):
        if not self.current_ready: return False
        if self._profile and self._profile.live_markers:
            return self._live_evidence[:3] == (self._installation, account, session_id)
        return True

    def opener_ready(self):
        from app.android.composer_open import resolved_composer_open_point
        if (self._profile != TIKTOK_YOU_4683 or not self.last.account_verified or not self.last.live_verified
                or self.last.security_state or self._send_binding is None or self._send_binding != self._account_binding): return False
        if self.last.screen == ScreenState.COMMENT_INPUT_READY:
            entry = unique_node(self._xml, self._profile.input_id)
            return bool(entry is not None and entry.get('text') == '' and
                all(entry.get(k) == 'true' for k in ('focused','enabled','clickable','focusable')))
        if self.last.screen == ScreenState.LIVE_READY:
            return resolved_composer_open_point(self._xml, self._profile) is not None
        return bool(self.last.screen == ScreenState.HOST_MENU and self._opening
            and monotonic() - self._opening[3] < 20)

    def fill_ready_for(self, session_id, account):
        if self._account_scope_guard is not None and not self._account_scope_guard(): return False
        if self._account_scope_guard is not None and not (self._live_evidence and self.last.live_verified
                and self._live_evidence[:3] == (self._installation, account, session_id)
                and monotonic() - self._live_evidence[3] < 90): return False
        if self._profile and self._profile.live_markers:
            return bool(self.last.manual_fill_candidate and self._manual_fill_evidence
                and monotonic() - self._manual_fill_evidence[3] < 90
                and self._manual_fill_evidence[:3] == (self._installation, account, session_id))
        return self.last.fill_ready

    def manual_inspection(self, target, runtime_revision=None):
        return ManualInspection(self, target, runtime_revision)

    def manual_block_reason(self):
        state = self.last
        if state.reason == "hierarchy_unavailable": return "hierarchy_unavailable"
        if state.security_state: return "security_ui"
        if state.screen == ScreenState.COMMENT_INPUT_READY and not state.account_verified:
            return "account_unverified"
        if state.screen == ScreenState.LIVE_READY and state.live_verified and state.account_verified:
            return "open_phone_composer"
        return "unknown_screen"

    def reviewed_splash_surface(self, xml, root):
        """Observed activity alias, never account authority or a generic UI fallback."""
        if reviewed_profile_screen(xml): return True
        layout = inspect_hierarchy(xml, TIKTOK_YOU_4683.package, None, TIKTOK_YOU_4683)
        if layout["screen"] == ScreenState.LIVE_READY and layout["live_verified"] and not layout["security_state"]:
            return True
        # Recognize the reviewed surface independently of account/LIVE authority,
        # just as for the primary activity. The classifier still requires both
        # bindings for delivery; an alias must not create a circular account gate.
        from app.android.composer_open import resolved_composer_open_point
        return composer_shape(root) or bool(self._opening and resolved_composer_open_point(xml, TIKTOK_YOU_4683, menu=True))

    @staticmethod
    def installation(info):
        values = []
        for name in ("firstInstallTime", "lastUpdateTime"):
            matches = re.findall(r"(?m)^\s*" + name + r"=([^\r\n]+)", info)
            if len(matches) != 1 or not re.fullmatch(r"[0-9T: .+\-]{10,80}", matches[0].strip()): return None
            values.append(matches[0].strip())
        return sha256(repr(values).encode()).hexdigest()

    async def _inspect(self, target=None, *, manual=False, compressed=False):
        self._profile, self._xml = None, ""
        self._manual_fill_evidence = None
        self._hierarchy_unavailable = False
        self._inspection_started = monotonic()
        self._inspection_timestamp = datetime.now(UTC).isoformat()
        self._inspection_trace = dict(adb_device_state=None, foreground_package=None, foreground_activity=None,
            tiktok_package_match=False, hierarchy_acquired=False, hierarchy_sha256=None)
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
                self.invalidate_trust()
                multiple = self.serial is None and len(devices) > 1
                self.last = DeviceInspection(**values, device="MULTIPLE_DEVICES" if multiple else "MISSING",
                    reason="multiple_devices" if multiple else "device_missing")
                return self.last
            values.update(serial=selected.serial, device=selected.state)
            self._inspection_trace["adb_device_state"] = selected.state
            if selected.state != "CONNECTED":
                self.invalidate_trust()
                self.last = DeviceInspection(**values, reason="unauthorized" if selected.state == "UNAUTHORIZED" else "offline")
                return self.last
            listed = await self.transport.run(AdbCommand.PACKAGES, serial=selected.serial)
            installed = {line.removeprefix("package:").strip() for line in listed.splitlines() if line.startswith("package:")}
            before = await self.transport.run(AdbCommand.FOREGROUND, serial=selected.serial)
            package, activity = foreground(before)
            self._inspection_trace.update(foreground_package=package, foreground_activity=activity,
                tiktok_package_match=package in PACKAGES)
            if package is None:
                self.invalidate_trust()
                self.last = DeviceInspection(**values, screen=ScreenState.UNKNOWN_SCREEN, reason="unknown_screen")
                return self.last
            if package not in PACKAGES or package not in installed:
                self.invalidate_trust()
                self.last = DeviceInspection(**values, screen=ScreenState.TIKTOK_NOT_RUNNING, reason="tiktok_not_running")
                return self.last
            values.update(package=package, foreground_activity=activity)
            dumped = await self.transport.run(AdbCommand.DUMP, serial=selected.serial, compressed=compressed)
            if DUMP_PATH not in dumped: raise HierarchyUnavailable()
            xml = await self.transport.run(AdbCommand.HIERARCHY, serial=selected.serial)
            self._inspection_trace["hierarchy_sha256"] = sha256(xml.encode("utf-8")).hexdigest()
            root = parse_hierarchy(xml)
            if len(xml.strip().encode("utf-8")) < 32 or root is None or not len(root):
                raise HierarchyUnavailable()
            self._inspection_trace["hierarchy_acquired"] = True
            info = await self.transport.run(AdbCommand.PACKAGE_INFO, serial=selected.serial, package=package)
            version, code = package_version(info)
            variant = observed_variant(package, activity)
            values.update(version=version, version_code=code, client_variant=variant)
            profile = None
            matching = [p for p in self.profiles if p.package == package and p.verified and p.version == version
                and (p.version_code is None or p.version_code == code) and (p.serial is None or p.serial == selected.serial)
                and (p.client_variant == ClientVariant.UNKNOWN and variant == ClientVariant.UNKNOWN and not p.activities
                     or (activity in p.activities or p == TIKTOK_YOU_4683
                         and activity == "com.ss.android.ugc.aweme.splash.SplashActivity" and self.reviewed_splash_surface(xml, root))
                     and variant in {ClientVariant.UNKNOWN, p.client_variant})]
            if len(matching) == 1:
                profile = matching[0]
                values["client_variant"] = profile.client_variant
            model = android = None
            if profile and profile.live_markers:
                model = (await self.transport.run(AdbCommand.DEVICE_MODEL, serial=selected.serial)).strip()
                android = (await self.transport.run(AdbCommand.ANDROID_VERSION, serial=selected.serial)).strip()
                # Metadata is part of this exact device profile, not just a display hint.
                if model != "M2101K9G" or android != "13": profile = None
            after = await self.transport.run(AdbCommand.FOREGROUND, serial=selected.serial)
            if foreground(after)[0] != package: raise ValueError("Foreground package changed")
            account = getattr(target, "account", None)
            if self._account_binding is not None and self._account_binding[1] != account:
                self.invalidate_trust()
            key = (selected.serial, package, version, code, values["client_variant"], model, android, self.installation(info))
            if self._account_scope_guard is not None and not self._account_scope_guard(): self.invalidate_trust()
            if self._installation is not None and key != self._installation: self.invalidate_trust()
            self._installation = key
            account_bound = bool(key[-1] and self._account_binding == (key, account))
            session = getattr(target, "live_session_id", None)
            live_bound = bool(self._live_evidence and self._live_evidence[:3] == (key, account, session)
                and monotonic() - self._live_evidence[3] < 90)
            parsed = inspect_hierarchy(xml, package, account, profile, account_bound=account_bound,
                live_bound=live_bound, send_confirmed=self._send_binding == (key, account))
            if (self._opening and self._opening[:3] == (key, account, session)
                    and monotonic() - self._opening[3] < 20 and account_bound and live_bound and not parsed['security_state']):
                from app.android.composer_open import resolved_composer_open_point
                if resolved_composer_open_point(xml, profile, menu=True) is not None:
                    parsed.update(screen=ScreenState.HOST_MENU, live_verified=True, reason="input_unavailable")
            if profile and profile.live_markers:
                if parsed["screen"] == ScreenState.LIVE_READY and parsed["live_verified"] and account_bound and session:
                    self._live_evidence = (key, account, session, monotonic())
                    from app.android.feed import host_feed
                    self._host_snapshot = host_feed(xml)
                elif not parsed["live_verified"]: self._live_evidence = None
                if parsed.get("send_control_state") == "SEND_CONTROL_CANDIDATE" and account_bound:
                    candidate_key = (key, account)
                    if self._candidate_key != candidate_key or self._candidate_token is None:
                        self._candidate_key, self._candidate_token = candidate_key, uuid4().hex
                    parsed["send_confirmation_token"] = self._candidate_token
            self._profile, self._xml = profile, xml
            parsed["interaction_available"] = self.actions is not None and profile is not None
            if parsed["reason"] == "ready" and self.actions is not None: values["device"] = "READY"
            self.last = DeviceInspection(**values, **parsed)
            if profile and profile.live_markers and self.last.manual_fill_candidate and session:
                entry = unique_node(xml, profile.input_id, profile.package)
                if (entry is not None and entry.get("class") == "android.widget.EditText"
                        and all(entry.get(k) == "true" for k in ("enabled", "clickable", "focusable", "focused"))
                        and entry.get("password") != "true"):
                    # MANUAL typing relies on the owned listener session (checked
                    # by the delivery guard), exact Profile/install trust and a
                    # fresh reviewed composer. It does not assert UI LIVE proof.
                    self._manual_fill_evidence = (key, account, session, monotonic())
        except AdbError:
            self._live_evidence = None
            self.last = DeviceInspection(**values, reason="adb_failed")
        except HierarchyUnavailable:
            self._hierarchy_unavailable = True
            # A MANUAL operation may preserve the exact Profile binding only
            # across its one immediate read-only recheck. No input is authorized
            # by this incomplete snapshot; all other paths still revoke trust.
            if not manual: self.invalidate_trust()
            self.last = DeviceInspection(**values, screen=ScreenState.UNKNOWN_SCREEN, reason="hierarchy_unavailable")
        except ValueError:
            self.invalidate_trust()
            self.last = DeviceInspection(**values, screen=ScreenState.UNKNOWN_SCREEN, reason="unknown_screen")
        return self.last

    async def inspect(self, target=None):
        async with self.lock:
            return await self.manual_inspection(target).read()

    async def prepare_installation(self, target):
        """Device/install preparation only; no accessibility or account claim."""
        async with self.lock:
            self.invalidate_trust()
            async def binding():
                devices = parse_devices(await self.transport.run(AdbCommand.DEVICES))
                if (len(devices) != 1 or devices[0].state != "CONNECTED" or devices[0].serial != TIKTOK_YOU_4683.serial
                        or self.serial not in {None, devices[0].serial}): raise ValueError("expected_device_required")
                serial = devices[0].serial
                listed = await self.transport.run(AdbCommand.PACKAGES, serial=serial)
                if "package:" + TIKTOK_YOU_4683.package not in listed.splitlines(): raise ValueError("expected_install_required")
                info = await self.transport.run(AdbCommand.PACKAGE_INFO, serial=serial, package=TIKTOK_YOU_4683.package)
                model = (await self.transport.run(AdbCommand.DEVICE_MODEL, serial=serial)).strip()
                android = (await self.transport.run(AdbCommand.ANDROID_VERSION, serial=serial)).strip()
                package, activity = foreground(await self.transport.run(AdbCommand.FOREGROUND, serial=serial))
                version, code = package_version(info)
                fingerprint = self.installation(info)
                if (model != "M2101K9G" or android != "13" or package != TIKTOK_YOU_4683.package
                        or activity not in (*TIKTOK_YOU_4683.activities, "com.ss.android.ugc.aweme.splash.SplashActivity")
                        or (version, code) != (TIKTOK_YOU_4683.version, TIKTOK_YOU_4683.version_code) or not fingerprint):
                    raise ValueError("expected_install_required")
                return (serial, package, version, code, TIKTOK_YOU_4683.client_variant, model, android, fingerprint), devices, activity
            try:
                before, _, _ = await binding()
                state = await self.manual_inspection(target).read()
                if state.security_state or state.reason not in {"hierarchy_unavailable", "unknown_screen", "live_unverified", "account_unverified", "input_unavailable"}:
                    raise ValueError("device_or_security_check_failed")
                after, devices, activity = await binding()
                if before != after: raise ValueError("installation_changed")
                self.serial, self._installation = after[0], after
                self.last = state.model_copy(update={"serial": after[0], "devices": devices, "device": "CONNECTED",
                    "package": after[1], "version": after[2], "version_code": after[3], "client_variant": after[4],
                    "foreground_activity": activity, "account_verified": False})
                return after
            except (AdbError, ValueError):
                self.invalidate_trust()
                self.last = self.last.model_copy(update={"account_verified": False})
                raise ValueError("fresh expected device/install preparation failed") from None

    async def verify_account(self, target):
        async with self.lock:
            self.invalidate_trust()
            inspection = self.manual_inspection(target)
            state = await inspection.read()
            if (state.security_state or self._profile != TIKTOK_YOU_4683 or not self._installation or not self._installation[-1]
                    or state.reason == "hierarchy_unavailable"):
                return False
            if not profile_account(self._xml, target.account):
                self.last = state.model_copy(update={"reason": "profile_mismatch"})
                return False
            model = (await self.transport.run(AdbCommand.DEVICE_MODEL, serial=state.serial)).strip()
            android = (await self.transport.run(AdbCommand.ANDROID_VERSION, serial=state.serial)).strip()
            # Reinspect after device metadata, so Profile cannot change during binding.
            key = self._installation
            await inspection.read()
            if (model != "M2101K9G" or android != "13" or self.last.security_state or self._profile != TIKTOK_YOU_4683
                    or self._installation != key): return False
            if not profile_account(self._xml, target.account):
                self.last = self.last.model_copy(update={"reason": "profile_mismatch"})
                return False
            self._account_binding = (self._installation, target.account)
            self.last = self.last.model_copy(update={"account_verified": True})
            return True

    async def confirm_send(self, target, token):
        async with self.lock:
            state = await self._inspect(target)
            if (not token or token != self._candidate_token or self._candidate_key != (self._installation, target.account)
                    or state.send_control_state != "SEND_CONTROL_CANDIDATE" or not state.account_verified
                    or not state.live_verified or state.security_state): return False
            self._send_binding = self._candidate_key
            self._candidate_token = None
            await self._inspect(target)
            return self.last.ready

    async def capture(self, target=None):
        async with self.lock:
            try:
                state = await self._inspect(target)
                if state.package is None or not self._xml or state.reason == "adb_failed": raise CaptureError()
                model = (await self.transport.run(AdbCommand.DEVICE_MODEL, serial=state.serial)).strip()
                android = (await self.transport.run(AdbCommand.ANDROID_VERSION, serial=state.serial)).strip()
                if not re.fullmatch(r"[A-Za-z0-9._ ()+-]{1,80}", model): raise CaptureError()
                if not re.fullmatch(r"[A-Za-z0-9._+-]{1,32}", android): raise CaptureError()
                info = await self.transport.run(AdbCommand.PACKAGE_INFO, serial=state.serial, package=state.package)
                after = await self.transport.run(AdbCommand.FOREGROUND, serial=state.serial)
                if (foreground(after)[0] != state.package
                        or package_version(info) != (state.version, state.version_code)): raise CaptureError()
                return save_capture(self._xml, state, model, android)
            except (AdbError, OSError, ValueError):
                raise CaptureError() from None

    async def fill(self, request, *, guard=lambda: False, inspection=None):
        """MANUAL preparation only. This path cannot call click_send or an activation."""
        async with self.lock:
            if not guard(): return AndroidOutcome(status="not_sent", reason="rejected")
            inspection = inspection or self.manual_inspection(request.target)
            if inspection.controller is not self or inspection.target != request.target:
                return AndroidOutcome(status="needs_operator", reason="rejected")
            state = await inspection.read()
            if not guard(): return AndroidOutcome(status="not_sent", reason="rejected")
            if request.target.device_serial != state.serial:
                inspection.block("device_unavailable")
                return AndroidOutcome(status="needs_operator", reason="device_unavailable")
            if state.security_state:
                inspection.block("security_ui")
                return AndroidOutcome(status="needs_operator", reason="security_ui")
            if state.screen == ScreenState.TRANSIENT_OVERLAY:
                inspection.block("transient_overlay")
                return AndroidOutcome(status="not_sent", reason="transient_overlay")
            if not self.fill_ready_for(request.target.live_session_id, request.target.account):
                reason = self.manual_block_reason()
                inspection.block(reason)
                return AndroidOutcome(status="needs_operator", reason=reason)
            if not self.actions or not self.actions.supports(request.text):
                return AndroidOutcome(status="needs_operator", reason="unicode_entry_unavailable")
            from app.android.text_entry import ASCIITextEntryBackend, TextEntryError
            if not isinstance(self.actions, ASCIITextEntryBackend):
                # Other adapters need an independently reviewed fill-only contract.
                return AndroidOutcome(status="needs_operator", reason="profile_unverified")
            profile = self._profile
            self.actions.bind(request.target, guard, require_send=False, inspection=inspection)
            try:
                entered = await self.actions.prepare_exact(state.serial, profile.input_id, request.text)
                final = await inspection.read()
            except (TextEntryError, AdbError) as exc:
                self.actions.report(exc.reason if isinstance(exc, TextEntryError) else "adb_failed")
                inspection.block("rejected")
                return AndroidOutcome(status="needs_operator", reason="rejected")
            rejection = ("final_guard_rejected" if not guard() else
                "text_readback_mismatch" if entered != request.text else
                "final_evidence_rejected" if not self.fill_ready_for(request.target.live_session_id, request.target.account) else
                "profile_changed" if self._profile != profile else
                "device_mismatch" if final.serial != state.serial else
                "final_readback_mismatch" if not exact_input(self._xml, profile, request.text) else None)
            if rejection:
                self.actions.report(rejection)
                inspection.block("rejected")
                return AndroidOutcome(status="needs_operator", reason="rejected")
            return AndroidOutcome(status="filled", reason="filled_waiting_user_send")

    async def verify_post_send(self, target, before, reply, guard):
        """Read only, at most four full inspections within four seconds. Never retry action."""
        from app.android.feed import host_feed, newly_confirmed
        try:
            async with asyncio.timeout(4):
                for attempt in range(4):
                    if attempt:
                        await asyncio.sleep(self.confirmation_interval)
                    state = await self._inspect(target)
                    if not guard() or state.security_state or not state.account_verified or not state.profile_verified:
                        return False
                    if state.screen == ScreenState.LIVE_READY and state.live_verified:
                        if newly_confirmed(before, host_feed(self._xml), reply): return True
        except (TimeoutError, AdbError, ValueError):
            pass
        return False

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
            if inspected.screen == ScreenState.TRANSIENT_OVERLAY:
                return AndroidOutcome(status="not_sent", reason="transient_overlay")
            if inspected.screen == ScreenState.LIVE_READY and self.opener_ready():
                from app.android.composer_open import resolved_composer_open_point
                self._opening = (self._installation, request.target.account, request.target.live_session_id, monotonic())
                try:
                    for menu in (False, True):
                        point = resolved_composer_open_point(self._xml, self._profile, menu=menu)
                        if point is None or not guard(): return AndroidOutcome(status="unknown", reason="interrupted")
                        await self.transport.run(AdbCommand.TAP_COMPOSER, serial=inspected.serial, point=point, guard=guard)
                        inspected = await self._inspect(request.target)
                        if not guard() or inspected.security_state: return AndroidOutcome(status="unknown", reason="interrupted")
                        if not menu and inspected.screen != ScreenState.HOST_MENU:
                            return AndroidOutcome(status="unknown", reason="interrupted")
                    entry = unique_node(self._xml, self._profile.input_id) if self._profile else None
                    if (not self.current_ready or entry is None or entry.get('text') != ''
                            or any(entry.get(k) != 'true' for k in ('focused','enabled','focusable'))):
                        return AndroidOutcome(status="unknown", reason="interrupted")
                except (AdbError, ValueError):
                    return AndroidOutcome(status="unknown", reason="interrupted")
                finally: self._opening = None
            if not self.current_ready:
                reason = inspected.reason if inspected.reason in {"account_unverified", "live_unverified", "profile_unverified"} else "unknown_screen"
                return AndroidOutcome(status="needs_operator", reason=reason)
            if not guard(): return AndroidOutcome(status="not_sent", reason="rejected")
            if not self.actions.supports(request.text):
                return AndroidOutcome(status="needs_operator", reason="unicode_entry_unavailable")
            profile = self._profile
            self.on_stage("AUTO SENDING")
            before = self._host_snapshot
            if profile.live_markers:
                from app.android.feed import normalized
                if before is None or (before.host, normalized(request.text)) in before.pairs:
                    if not (self.outgoing and self.outgoing.available(request.target)):
                        return AndroidOutcome(status="not_sent", reason="confirmation_ambiguous")
            from app.android.text_entry import ASCIITextEntryBackend, TextEntryError
            def before_send():
                if self.outgoing: self.outgoing.begin(request.operation_id, request.target, request.text, seconds=20)
            if isinstance(self.actions, ASCIITextEntryBackend): self.actions.bind(request.target, guard, before_send=before_send)
            try:
                entered = await self.actions.prepare_exact(inspected.serial, profile.input_id, request.text)
            except (TextEntryError, AdbError):
                return AndroidOutcome(status="needs_operator", reason="rejected")
            if entered != request.text: return AndroidOutcome(status="not_sent", reason="rejected")
            final = await self._inspect(request.target)
            if final.security_state:
                return AndroidOutcome(status="needs_operator", reason="security_ui")
            if (not final.ready or final.serial != inspected.serial or final.package != inspected.package
                    or self._profile != profile or not exact_input(self._xml, profile, request.text)):
                return AndroidOutcome(status="needs_operator", reason="unknown_screen")
            # No await between this authoritative check and the semantic action call.
            if not guard(): return AndroidOutcome(status="not_sent", reason="rejected")
            try:
                issued = await self.actions.click_send(final.serial, profile.send_id, guard)
            except TextEntryError:
                return AndroidOutcome(status="needs_operator", reason="rejected")
            if issued is False: return AndroidOutcome(status="not_sent", reason="rejected")
            self.on_stage("POST_SEND_VERIFY")
            if self.outgoing and await self.outgoing.wait(request.operation_id, guard):
                return AndroidOutcome(status="confirmed", reason="sdk_echo_confirmed")
            def confirmation_guard():
                pending = self.outgoing.pending if self.outgoing else None
                return guard() and (pending is None or self.outgoing.valid(pending))
            if before is not None and await self.verify_post_send(request.target, before, request.text, confirmation_guard):
                return AndroidOutcome(status="confirmed", reason="feed_confirmed")
            if self.outgoing and await self.outgoing.wait(request.operation_id, guard, seconds=0):
                return AndroidOutcome(status="confirmed", reason="sdk_echo_confirmed")
            # Exit status, blank composer or absence of a row can never prove delivery.
            return AndroidOutcome(status="unknown", reason="interrupted")

    async def aclose(self):
        self.closed = True
        try:
            await self.transport.aclose()
        finally:
            if self.actions is not None: await self.actions.aclose()
