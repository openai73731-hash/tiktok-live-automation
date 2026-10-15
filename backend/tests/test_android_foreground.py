"""Sanitized Android 13/MIUI foreground regressions; no real ADB or UI actions."""
import asyncio
from itertools import permutations
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.android.adb import AdbCommand, AdbError, AdbTransport, DUMP_PATH
from app.android.controller import ADBAndroidController
from app.android.models import ScreenState
from app.android.tiktok_ui import foreground

PACKAGE = "com.zhiliaoapp.musically"
ACTIVITY = "com.ss.android.ugc.aweme.splash.SplashActivity"
INSTALLER = "com.miui.global.packageinstaller"
TIKTOK_ANDROID_13 = (
    "topResumedActivity=ActivityRecord{ea3bfe4 u0 "
    "com.zhiliaoapp.musically/com.ss.android.ugc.aweme.splash.SplashActivity t189}"
)
MIUI_INSTALLER = (
    "topResumedActivity=ActivityRecord{edbf8c3 u0 com.miui.global.packageinstaller/.activity.ScanActivity t193}\n"
    "ResumedActivity: ActivityRecord{edbf8c3 u0 com.miui.global.packageinstaller/.activity.ScanActivity t193}"
)
LEGACY_COLON = "mResumedActivity: ActivityRecord{abc u0 com.zhiliaoapp.musically/.SomeActivity t1}"
LEGACY_EQUALS = "mResumedActivity=ActivityRecord{abc u0 com.zhiliaoapp.musically/.SomeActivity t1}"


@pytest.fixture(autouse=True)
def no_real_adb(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=AssertionError("Real ADB forbidden")))


@pytest.mark.parametrize("dump,expected", [
    (TIKTOK_ANDROID_13, (PACKAGE, ACTIVITY)),
    (MIUI_INSTALLER, (INSTALLER, ".activity.ScanActivity")),
    (LEGACY_COLON, (PACKAGE, ".SomeActivity")),
    (LEGACY_EQUALS, (PACKAGE, ".SomeActivity")),
])
def test_observed_sanitized_foreground_fixtures(dump, expected):
    assert foreground(dump) == expected


@pytest.mark.parametrize("marker", ["topResumedActivity", "mResumedActivity", "ResumedActivity"])
@pytest.mark.parametrize("separator", ["=", ":"])
@pytest.mark.parametrize("padding", ["", " \t"])
@pytest.mark.parametrize("activity", [ACTIVITY, ".SomeActivity$Nested"])
def test_resumed_formats_whitespace_and_relative_contract(marker, separator, padding, activity):
    dump = f"  {marker}{padding}{separator}{padding}ActivityRecord{{abc u0 {PACKAGE}/{activity} t1}}\t\r\n"
    assert foreground(dump) == (PACKAGE, activity)


PRIORITY_LINES = (
    TIKTOK_ANDROID_13,
    f"mResumedActivity: ActivityRecord{{abc u0 {INSTALLER}/.HistoricalActivity t2}}",
    "ResumedActivity=ActivityRecord{abc u0 com.android.launcher/.Launcher t3}",
)


@pytest.mark.parametrize("lines", list(permutations(PRIORITY_LINES)))
def test_top_marker_takes_priority_independent_of_line_order(lines):
    assert foreground("\n".join(lines)) == (PACKAGE, ACTIVITY)


@pytest.mark.parametrize("lines", [
    (LEGACY_COLON, PRIORITY_LINES[2]), (PRIORITY_LINES[2], LEGACY_COLON),
])
def test_m_marker_takes_priority_over_bare_marker(lines):
    assert foreground("\n".join(lines)) == (PACKAGE, ".SomeActivity")


@pytest.mark.parametrize("marker", ["topResumedActivity", "mResumedActivity", "ResumedActivity"])
def test_duplicate_current_marker_same_component_is_unambiguous(marker):
    dump = f"{marker}=ActivityRecord{{abc u0 {PACKAGE}/.SomeActivity t1}}\n"
    dump += f"{marker}: ActivityRecord{{def u0 {PACKAGE}/.SomeActivity t2}}"
    assert foreground(dump) == (PACKAGE, ".SomeActivity")


@pytest.mark.parametrize("marker", ["topResumedActivity", "mResumedActivity", "ResumedActivity"])
@pytest.mark.parametrize("other", ["null", f"ActivityRecord{{def u0 {INSTALLER}/.OtherActivity t2}}"])
def test_ambiguous_current_marker_cannot_fall_back_to_lower_priority(marker, other):
    dump = f"{marker}=ActivityRecord{{abc u0 {PACKAGE}/.SomeActivity t1}}\n{marker}: {other}"
    if marker != "ResumedActivity": dump += f"\nResumedActivity: ActivityRecord{{abc u0 {PACKAGE}/.SomeActivity t1}}"
    assert foreground(dump) == (None, None)


@pytest.mark.parametrize("marker", ["topResumedActivity", "mResumedActivity"])
def test_malformed_higher_marker_does_not_reuse_history(marker):
    assert foreground(f"{marker}=null\nResumedActivity: ActivityRecord{{abc u0 {PACKAGE}/.SomeActivity t1}}") == (None, None)


@pytest.mark.parametrize("dump", [
    "", "\r\n\t", "unrecognized dumpsys",
    f"ACTIVITY {PACKAGE}/.HistoricalActivity abc",
    f"mLastResumedActivity: ActivityRecord{{abc u0 {PACKAGE}/.HistoricalActivity t1}}",
    f"prefix topResumedActivity=ActivityRecord{{abc u0 {PACKAGE}/.HistoricalActivity t1}}",
    f"ResumedActivityHistory: ActivityRecord{{abc u0 {PACKAGE}/.HistoricalActivity t1}}",
    "topResumedActivity=ActivityRecord{...}",
    f"topResumedActivity=ActivityRecord{{abc u0 {PACKAGE}/.Unclosed t1",
    "topResumedActivity=ActivityRecord{abc u0 unsafe-package/.Activity t1}",
    f"topResumedActivity=ActivityRecord{{abc u0 {PACKAGE}/.Activity;unsafe t1}}",
    f"topResumedActivity=ActivityRecord{{abc u0 {PACKAGE}/.Экран t1}}",
    f"topResumedActivity=ActivityRecord{{abc u0 {PACKAGE}/.Activity/Other t1}}",
    f"topResumedActivity=ActivityRecord{{abc u0 {PACKAGE}/{'A' * 201} t1}}",
    f"topResumedActivity=ActivityRecord{{abc u0 {PACKAGE}/.One {INSTALLER}/.Two t1}}",
    f"topResumedActivity:\nACTIVITY {PACKAGE}/.HistoricalActivity abc",
])
def test_empty_malformed_and_non_resumed_history_fail_closed(dump):
    assert foreground(dump) == (None, None)


class InspectionTransport:
    executable = Path("fixture/adb.exe")

    def __init__(self, before, after):
        self.foregrounds = iter((before, after))
        self.calls = []

    async def run(self, command, **kwargs):
        self.calls.append(command)
        if command == AdbCommand.FOREGROUND: return next(self.foregrounds)
        responses = {
            AdbCommand.DEVICES: "List of devices attached\nUSB_FIXTURE device\n",
            AdbCommand.PACKAGES: "package:" + PACKAGE,
            AdbCommand.PACKAGE_INFO: "versionName=47.0.3 versionCode=2024700030",
            AdbCommand.DUMP: "UI hierarchy dumped to: " + DUMP_PATH,
            # Generic editable/send nodes must not establish TikTok controls without a reviewed profile.
            AdbCommand.HIERARCHY: f'<hierarchy><node package="{PACKAGE}" class="android.widget.EditText"/>'
                f'<node package="{PACKAGE}" content-desc="Send"/></hierarchy>',
        }
        assert command in responses, "Unexpected fixture access"
        return responses[command]

    async def aclose(self): pass


@pytest.mark.parametrize("after", [TIKTOK_ANDROID_13, f"ResumedActivity: ActivityRecord{{abc u0 {PACKAGE}/{ACTIVITY} t2}}"])
async def test_open_tiktok_detected_without_promoting_unknown_profile_to_ready(after):
    transport = InspectionTransport(TIKTOK_ANDROID_13, after)
    controller = ADBAndroidController(transport, serial="USB_FIXTURE")
    state = await controller.inspect()
    assert state.adb == "AVAILABLE" and state.device == "CONNECTED"
    assert state.package == PACKAGE and state.foreground_activity == ACTIVITY
    assert state.screen == ScreenState.UNKNOWN_SCREEN and state.reason == "profile_unverified"
    assert not any((state.ready, state.account_verified, state.live_verified, state.profile_verified,
        state.comment_input_found, state.send_button_found, state.interaction_available))
    assert not controller.physical_enabled
    # RC ships a strictly version/device bound profile, never a wildcard match.
    assert all(p.serial == "903ed867" and p.version == "46.8.3" for p in controller.profiles)
    assert controller._profile is None
    assert transport.calls == [AdbCommand.DEVICES, AdbCommand.PACKAGES, AdbCommand.FOREGROUND,
        AdbCommand.DUMP, AdbCommand.HIERARCHY, AdbCommand.PACKAGE_INFO, AdbCommand.FOREGROUND]


async def test_relative_activity_is_preserved_through_controller_model():
    controller = ADBAndroidController(InspectionTransport(LEGACY_COLON, LEGACY_EQUALS))
    state = await controller.inspect()
    assert state.foreground_activity == ".SomeActivity" and state.screen == ScreenState.UNKNOWN_SCREEN
    assert not state.ready


async def test_package_installer_foreground_is_not_tiktok():
    transport = InspectionTransport(MIUI_INSTALLER, MIUI_INSTALLER)
    state = await ADBAndroidController(transport).inspect()
    assert state.screen == ScreenState.TIKTOK_NOT_RUNNING and not state.ready
    assert state.package is None and not state.account_verified and not state.live_verified
    assert AdbCommand.DUMP not in transport.calls


@pytest.mark.parametrize("after", [MIUI_INSTALLER, ""])
async def test_foreground_changes_or_becomes_ambiguous_during_inspection_fail_closed(after):
    state = await ADBAndroidController(InspectionTransport(TIKTOK_ANDROID_13, after)).inspect()
    assert state.device == "CONNECTED" and state.screen == ScreenState.UNKNOWN_SCREEN
    assert state.reason == "unknown_screen" and not state.ready
    assert not any((state.account_verified, state.live_verified, state.comment_input_found, state.send_button_found))


FOCUSED_TIKTOK = f"mFocusedApp=ActivityRecord{{abc u0 {PACKAGE}/{ACTIVITY} t1}}"
AOD = "mCurrentFocus=Window{abc u0 AOD}"
RESUMED_TIKTOK = f"ResumedActivity: ActivityRecord{{abc u0 {PACKAGE}/{ACTIVITY} t1}}"


@pytest.mark.parametrize("dump", [
    RESUMED_TIKTOK + "\n" + FOCUSED_TIKTOK + "\n" + AOD,
    RESUMED_TIKTOK,
    f"ResumedActivity:\n  {PACKAGE}/{ACTIVITY}\n" + FOCUSED_TIKTOK + "\n" + AOD,
    FOCUSED_TIKTOK + "\n" + AOD,
    f"mCurrentFocus=Window{{abc u0 {PACKAGE}/{ACTIVITY}}}",
])
async def test_miui_aod_or_missing_window_focus_keeps_connected_tiktok_foreground(dump):
    transport = InspectionTransport(dump, dump)
    state = await ADBAndroidController(transport).inspect()
    assert foreground(dump) == (PACKAGE, ACTIVITY)
    assert state.adb == "AVAILABLE" and state.device == "CONNECTED"
    assert state.package == PACKAGE and state.foreground_activity == ACTIVITY
    assert state.screen == ScreenState.UNKNOWN_SCREEN and state.reason == "profile_unverified"
    assert not state.profile_verified and not state.account_verified and not state.ready


@pytest.mark.parametrize("dump", [
    "", AOD, "mCurrentFocus=null",
    RESUMED_TIKTOK + f"\nmFocusedApp=ActivityRecord{{abc u0 {INSTALLER}/.ScanActivity t1}}\n" + AOD,
    f"ResumedActivity: ActivityRecord{{abc u0 {INSTALLER}/.ScanActivity t1}}\n" + FOCUSED_TIKTOK,
    RESUMED_TIKTOK + f"\nmCurrentFocus=Window{{abc u0 {INSTALLER}/.ScanActivity}}",
    FOCUSED_TIKTOK + f"\nmFocusedApp=ActivityRecord{{abc u0 {INSTALLER}/.ScanActivity t2}}",
])
async def test_missing_or_conflicting_foreground_is_unknown_not_disconnected(dump):
    transport = InspectionTransport(dump, dump)
    state = await ADBAndroidController(transport).inspect()
    assert foreground(dump) == (None, None)
    assert state.device == "CONNECTED" and state.screen == ScreenState.UNKNOWN_SCREEN
    assert state.reason == "unknown_screen" and state.package is None
    assert not state.account_verified and not state.profile_verified and not state.ready
    assert AdbCommand.DUMP not in transport.calls


async def test_other_resumed_and_focused_app_never_establish_tiktok_foreground():
    dump = MIUI_INSTALLER + f"\nmFocusedApp=ActivityRecord{{abc u0 {INSTALLER}/.activity.ScanActivity t193}}\n" + AOD
    state = await ADBAndroidController(InspectionTransport(dump, dump)).inspect()
    assert state.device == "CONNECTED" and state.screen == ScreenState.TIKTOK_NOT_RUNNING
    assert state.package is None and not state.account_verified and not state.ready


@pytest.mark.parametrize("failure", ["offline", "missing", "command"])
async def test_device_disconnected_requires_device_or_adb_failure(failure):
    class FailedTransport(InspectionTransport):
        async def run(self, command, **kwargs):
            if command == AdbCommand.DEVICES:
                return "List of devices attached\n" + ("USB_FIXTURE offline\n" if failure == "offline" else "" if failure == "missing" else "USB_FIXTURE device\n")
            raise AdbError()
    state = await ADBAndroidController(FailedTransport("", "")).inspect()
    assert state.screen == ScreenState.DEVICE_DISCONNECTED
    assert state.reason == {"offline":"offline", "missing":"device_missing", "command":"adb_failed"}[failure]
    assert not state.account_verified and not state.ready


@pytest.mark.parametrize("dump", ["", "ERROR: could not get idle state."])
async def test_successful_adb_with_unavailable_hierarchy_has_explicit_reason(dump):
    class NoHierarchyTransport(InspectionTransport):
        async def run(self, command, **kwargs):
            if command == AdbCommand.DUMP: return dump
            return await super().run(command, **kwargs)
    state = await ADBAndroidController(NoHierarchyTransport(RESUMED_TIKTOK, RESUMED_TIKTOK)).inspect()
    assert state.device == "CONNECTED" and state.screen == ScreenState.UNKNOWN_SCREEN
    assert state.reason == "hierarchy_unavailable" and not state.account_verified and not state.ready


@pytest.mark.parametrize("window_code", [0, 1])
async def test_production_adb_foreground_collects_both_fixed_read_only_dumps(tmp_path, monkeypatch, window_code):
    def process(output, code=0):
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        stdout.feed_data(output.encode()); stdout.feed_eof(); stderr.feed_eof()
        return Mock(stdout=stdout, stderr=stderr, returncode=code, wait=AsyncMock(return_value=code))
    spawn = AsyncMock(side_effect=[process(RESUMED_TIKTOK), process(FOCUSED_TIKTOK + "\n" + AOD, window_code)])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    executable = tmp_path / "adb.exe"; executable.touch()
    transport = AdbTransport(executable)
    try:
        if window_code:
            with pytest.raises(AdbError): await transport.run(AdbCommand.FOREGROUND, serial="USB_FIXTURE")
        else:
            output = await transport.run(AdbCommand.FOREGROUND, serial="USB_FIXTURE")
            assert foreground(output) == (PACKAGE, ACTIVITY)
            assert all(signal in output for signal in (RESUMED_TIKTOK, FOCUSED_TIKTOK, AOD))
        assert [call.args[-4:] for call in spawn.call_args_list] == [
            ("shell", "dumpsys", "activity", "activities"), ("shell", "dumpsys", "window", "windows")]
        assert not transport.processes
    finally: await transport.aclose()


@pytest.mark.parametrize("identity", ["exact", "wrong_handle", "live_display_name"])
async def test_miui_aod_preserves_reviewed_profile_and_explicit_exact_handle_verification(identity):
    from test_manual_send_rc import Transport, target, ACCOUNT
    from app.android.tiktok_ui import TIKTOK_YOU_4683, rid
    class ProfileTransport(Transport):
        async def run(self, command, **kwargs):
            if command == AdbCommand.FOREGROUND:
                activity = TIKTOK_YOU_4683.activities[0]
                return f"ResumedActivity: ActivityRecord{{abc u0 {PACKAGE}/{activity} t1}}\nmFocusedApp=ActivityRecord{{abc u0 {PACKAGE}/{activity} t1}}\n" + AOD
            return await super().run(command, **kwargs)
    transport = ProfileTransport()
    if identity == "live_display_name": transport.xml = transport.xml.replace(rid("sxp"), rid("user_name"))
    dest = target(account="other" if identity == "wrong_handle" else ACCOUNT)
    controller = ADBAndroidController(transport, physical_enabled=False)
    state = await controller.inspect(dest)
    assert state.device == "CONNECTED" and state.profile_verified and not state.account_verified
    assert state.version == "46.8.3" and controller._profile is TIKTOK_YOU_4683
    assert await controller.verify_account(dest) is (identity == "exact")
    assert controller.last.account_verified is (identity == "exact")
    assert not controller.last.ready and not controller.physical_enabled
    assert not any(command in {AdbCommand.TEXT, AdbCommand.DELETE, AdbCommand.SELECT_ALL,
        AdbCommand.TAP_SEND, AdbCommand.TAB, AdbCommand.ACTIVATE} for command, _ in transport.calls)


@pytest.mark.parametrize("marker", ["topResumedActivity", "ResumedActivity", "mFocusedApp", "mCurrentFocus"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_activity_alias_namespace_is_not_the_application_package(marker, wrapped):
    component = f"{PACKAGE}/com.windukk.mod.icon.default"
    kind = "Window" if marker == "mCurrentFocus" else "ActivityRecord"
    value = f"{kind}{{abc u0 {component}}}" if wrapped else "\n  " + component
    assert foreground(marker + "=" + value) == (PACKAGE, "com.windukk.mod.icon.default")


@pytest.mark.parametrize("signals", [
    RESUMED_TIKTOK + f"\nmFocusedApp={PACKAGE}/com.windukk.mod.icon.default\n" + AOD,
    f"topResumedActivity={PACKAGE}/com.windukk.mod.icon.default\n" + FOCUSED_TIKTOK,
    RESUMED_TIKTOK + f"\nResumedActivity={PACKAGE}/com.windukk.mod.icon.default\n" + FOCUSED_TIKTOK,
])
async def test_different_activity_classes_in_same_package_are_not_foreground_conflicts(signals):
    assert foreground(signals)[0] == PACKAGE
    state = await ADBAndroidController(InspectionTransport(signals, signals)).inspect()
    assert state.device == "CONNECTED" and state.package == PACKAGE
    assert state.reason == "profile_unverified" and not state.ready


@pytest.mark.parametrize("after", [LEGACY_COLON, f"ResumedActivity={PACKAGE}/com.windukk.mod.icon.default"])
async def test_same_package_activity_transition_during_inspection_is_not_device_failure(after):
    state = await ADBAndroidController(InspectionTransport(TIKTOK_ANDROID_13, after)).inspect()
    assert state.device == "CONNECTED" and state.package == PACKAGE
    assert state.reason == "profile_unverified" and not state.ready
