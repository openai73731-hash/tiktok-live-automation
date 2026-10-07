"""Sanitized Android 13/MIUI foreground regressions; no real ADB or UI actions."""
import asyncio
from itertools import permutations
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.android.adb import AdbCommand, DUMP_PATH
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


@pytest.mark.parametrize("after", [MIUI_INSTALLER, "", LEGACY_COLON,
    TIKTOK_ANDROID_13 + "\n" + f"topResumedActivity: ActivityRecord{{def u0 {PACKAGE}/.DifferentActivity t2}}"])
async def test_foreground_changes_or_becomes_ambiguous_during_inspection_fail_closed(after):
    state = await ADBAndroidController(InspectionTransport(TIKTOK_ANDROID_13, after)).inspect()
    assert state.reason == "adb_failed" and not state.ready
    assert not any((state.account_verified, state.live_verified, state.comment_input_found, state.send_button_found))
