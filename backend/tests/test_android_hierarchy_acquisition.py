"""Real ADB transport/controller/API; only subprocess/device output is fake."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.android.adb import AdbCommand, AdbTransport, DUMP_PATH
from app.android.controller import ADBAndroidController
from app.android.models import ScreenState
from app.android.tiktok_ui import TIKTOK_YOU_4683, rid
from test_manual_send_rc import ACCOUNT, Transport, target, xml
from test_unified_transports import manual_fill_api, no_physical

SPLASH = "com.ss.android.ugc.aweme.splash.SplashActivity"
INPUT = {"input", "tap", "keyevent", "keycombination"}


class Process:
    def __init__(self, stdout=b"", stderr=b"", code=0):
        self.returncode = code
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(stdout); self.stdout.feed_eof()
        self.stderr.feed_data(stderr); self.stderr.feed_eof()
    async def wait(self): return self.returncode


class FakeDevice:
    def __init__(self, snapshots, *, activity=SPLASH, offline=False):
        self.snapshots = list(snapshots)
        self.activity, self.offline = activity, offline
        self.files = {DUMP_PATH: xml("profile").encode()}  # stale file must be removed, never accepted
        self.calls, self.modes = [], []
    async def spawn(self, *args, **kwargs):
        self.calls.append(args)
        assert not INPUT & set(args), "Physical input forbidden"
        if args[-2:] == ("devices", "-l"):
            return Process(b"List of devices attached\n903ed867 " + (b"offline\n" if self.offline else b"device\n"))
        if "rm" in args:
            assert args[-4:] == ("shell", "rm", "-f", DUMP_PATH)
            self.files.pop(DUMP_PATH, None)
            return Process()
        if "uiautomator" in args:
            self.modes.append("--compressed" in args)
            snapshot = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
            if snapshot == "idle": return Process(stderr=b"ERROR: could not get idle state.")
            if snapshot != "absent": self.files[DUMP_PATH] = snapshot if isinstance(snapshot, bytes) else snapshot.encode()
            return Process(("UI hierarchy dumped to: " + DUMP_PATH).encode())
        if args[-3:] == ("exec-out", "cat", DUMP_PATH):
            if DUMP_PATH not in self.files:
                return Process(stderr=("cat: " + DUMP_PATH + ": No such file or directory").encode(), code=1)
            return Process(self.files[DUMP_PATH])
        if args[-4:] == ("shell", "pm", "list", "packages"):
            return Process(b"package:com.zhiliaoapp.musically\n")
        component = "com.zhiliaoapp.musically/" + self.activity
        if args[-3:] == ("dumpsys", "activity", "activities"):
            return Process(("topResumedActivity = " + component).encode())
        if args[-3:] == ("dumpsys", "window", "windows"):
            return Process(("mCurrentFocus=Window{x u0 " + component + "}\nmFocusedApp=ActivityRecord{x u0 " + component + " t1}").encode())
        if args[-3:] == ("dumpsys", "package", "com.zhiliaoapp.musically"):
            return Process(Transport().info.encode())
        if args[-2:] == ("getprop", "ro.product.model"): return Process(b"M2101K9G")
        if args[-2:] == ("getprop", "ro.build.version.release"): return Process(b"13")
        pytest.fail("Unplanned device command")


def native_controller(tmp_path, monkeypatch, snapshots, **kwargs):
    device = FakeDevice(snapshots, **kwargs)
    path = tmp_path / "adb.exe"; path.touch()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=device.spawn))
    controller = ADBAndroidController(AdbTransport(path), physical_enabled=False)
    return controller, device


@pytest.mark.parametrize("failure", ["idle", "absent", "", "<broken", "<profile><node/></profile>", "<hierarchy/>", b"\xff"])
async def test_zero_exit_without_fresh_usable_xml_is_hierarchy_unavailable(tmp_path, monkeypatch, failure):
    c, device = native_controller(tmp_path, monkeypatch, [failure])
    assert not await c.verify_account(target())
    assert device.modes == [False, True]
    assert c.last.reason == "hierarchy_unavailable" and c.last.device == "CONNECTED"
    assert c.last.screen == ScreenState.UNKNOWN_SCREEN and not c.last.account_verified
    assert not c.last.profile_verified and c._account_binding is None and c._xml == ""
    assert not c.transport.processes
    assert len([a for a in device.calls if "rm" in a]) == 2


@pytest.mark.parametrize("failure", ["idle", "absent", "", "<broken", "<hierarchy/>", b"\xff"])
async def test_one_alternate_mode_retry_restores_fresh_profile_verification(tmp_path, monkeypatch, failure):
    c, device = native_controller(tmp_path, monkeypatch, [failure, xml("profile")])
    assert await c.verify_account(target()), (c.last, c._inspection_trace, device.modes)
    assert device.modes == [False, True, False]  # one retry, then the existing final fresh Profile check
    assert c.last.account_verified and c.last.profile_verified and not c.last.live_verified
    assert c.last.foreground_activity == SPLASH and c._profile == TIKTOK_YOU_4683
    assert not c.current_ready and not c.physical_enabled


@pytest.mark.parametrize("identity", ["exact", "foreign", "display_name", "changed_final"])
async def test_valid_fresh_profile_still_requires_exact_handle_twice(tmp_path, monkeypatch, identity):
    valid = xml("profile")
    wrong = valid.replace("@" + ACCOUNT, "@foreign")
    snapshots = [valid, wrong] if identity == "changed_final" else [wrong if identity == "foreign"
        else valid.replace(rid("sxp"), rid("user_name")) if identity == "display_name" else valid]
    c, device = native_controller(tmp_path, monkeypatch, snapshots)
    assert await c.verify_account(target()) is (identity == "exact")
    assert c.last.account_verified is (identity == "exact")
    if identity in {"foreign", "changed_final"}: assert c.last.reason == "profile_mismatch"
    assert True not in device.modes  # a parsed mismatch is never acquisition failure/retry


async def test_final_profile_inspection_can_recheck_once_but_not_twice(tmp_path, monkeypatch):
    c, device = native_controller(tmp_path, monkeypatch, [xml("profile"), "absent", xml("profile")])
    assert await c.verify_account(target())
    assert device.modes == [False, False, True]
    c, device = native_controller(tmp_path, monkeypatch, ["absent", xml("profile"), "absent"])
    assert not await c.verify_account(target())
    assert device.modes == [False, True, False] and c.last.reason == "hierarchy_unavailable"
    assert c._account_binding is None


async def test_old_diagnostics_and_old_trust_never_replace_current_xml(manual_fill_api, tmp_path, monkeypatch):
    app, client, service, _, _, _ = manual_fill_api
    await app.state.operator_runtime.live.disconnect(app.state.operator_runtime.live.session_id)
    old = tmp_path / "diagnostics/android/old"; old.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures/android/reviewed_manual_fill_4683/hierarchy.xml"
    (old / "hierarchy.xml").write_bytes(fixture.read_bytes())
    monkeypatch.setattr("app.android.diagnostics.CAPTURE_ROOT", old.parent)
    original = Path.open
    def forbid_cached(path, *args, **kwargs):
        if old.parent in path.parents: pytest.fail("Old diagnostic used as current proof")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", forbid_cached)
    c, device = native_controller(tmp_path, monkeypatch, ["idle"])
    c._account_binding = service.controller._account_binding
    c._installation = service.controller._installation
    c._xml = xml("profile")
    service.controller = c
    response = await client.post("/api/admin/device/verify-account", json={})
    assert response.status_code == 409
    assert "hierarchy_unavailable" in response.json()["detail"]
    assert "Profile hierarchy unavailable" in response.json()["detail"]
    assert "Откройте собственный Profile с точным" not in response.text and "profile_mismatch" not in response.text
    state = (await client.get("/api/admin/delivery/status")).json()["inspection"]
    assert state["reason"] == "hierarchy_unavailable" and not state["account_verified"]
    assert service._verified_runtime_revision is None and device.modes == [False, True]


async def test_api_fresh_profile_mismatch_is_distinct(manual_fill_api, tmp_path, monkeypatch):
    app, client, service, _, _, _ = manual_fill_api
    await app.state.operator_runtime.live.disconnect(app.state.operator_runtime.live.session_id)
    c, device = native_controller(tmp_path, monkeypatch, [xml("profile").replace("@" + ACCOUNT, "@foreign")])
    service.controller = c
    response = await client.post("/api/admin/device/verify-account", json={})
    assert response.status_code == 409 and "profile_mismatch" in response.json()["detail"]
    assert "hierarchy_unavailable" not in response.text and device.modes == [False]


async def test_splash_preparation_is_device_only_profile_verification_remains_optional(manual_fill_api, tmp_path, monkeypatch):
    app, client, service, _, _, _ = manual_fill_api
    await app.state.operator_runtime.live.disconnect(app.state.operator_runtime.live.session_id)
    c, device = native_controller(tmp_path, monkeypatch, [xml("profile")])
    service.controller = c
    initial = await client.post("/api/admin/delivery/prepare", json={})
    assert initial.status_code == 200 and initial.json()["mobile_prepared"]
    assert not initial.json()["inspection"]["account_verified"]
    verified = await client.post("/api/admin/device/verify-account", json={})
    assert verified.status_code == 200 and verified.json()["inspection"]["account_verified"]
    assert verified.json()["inspection"]["foreground_activity"] == SPLASH
    revision = app.state.operator_runtime.active_runtime_revision
    prepared = await client.post("/api/admin/delivery/prepare", json={})
    assert prepared.status_code == 200 and not prepared.json()["paused"]
    assert prepared.json()["active_runtime_revision"] != revision
    assert prepared.json()["auto_state"] == "AUTO OFF" and True not in device.modes
    assert not prepared.json()["inspection"]["account_verified"]


async def test_splash_composer_recognition_never_substitutes_for_account_or_live_proof(tmp_path, monkeypatch):
    c, _ = native_controller(tmp_path, monkeypatch, [xml("composer")])
    assert not await c.verify_account(target())
    assert c._profile == TIKTOK_YOU_4683 and c.last.screen == ScreenState.COMMENT_INPUT_READY
    assert not c.last.account_verified and not c.last.live_verified and not c.current_ready
    assert c._account_binding is None and c._live_evidence is None
    assert not c.fill_ready_for(target().live_session_id, target().account)
    assert TIKTOK_YOU_4683.activities == ("com.windukk.mod.icon.default",)


async def test_real_transport_offline_stays_device_disconnected(tmp_path, monkeypatch):
    c, device = native_controller(tmp_path, monkeypatch, ["idle"], offline=True)
    assert not await c.verify_account(target())
    assert c.last.screen == ScreenState.DEVICE_DISCONNECTED and c.last.reason == "offline"
    assert not device.modes


def test_dump_modes_and_cleanup_are_fixed_argv_only(tmp_path):
    path = tmp_path / "adb.exe"; path.touch()
    t = AdbTransport(path)
    assert t.argv(AdbCommand.DUMP, serial="903ed867", compressed=True)[-5:] == (['shell','uiautomator','dump','--compressed',DUMP_PATH])
    assert t.argv(AdbCommand.CLEAR_HIERARCHY, serial="903ed867")[-4:] == ['shell','rm','-f',DUMP_PATH]
