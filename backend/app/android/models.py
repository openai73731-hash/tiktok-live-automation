"""Minimal device contracts independent of AI and TikTokLive."""
from enum import StrEnum
from typing import Literal, Protocol, Callable
from pydantic import BaseModel, ConfigDict, Field

SERIAL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"


class DeviceModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScreenState(StrEnum):
    DEVICE_DISCONNECTED = "DEVICE_DISCONNECTED"
    TIKTOK_NOT_RUNNING = "TIKTOK_NOT_RUNNING"
    LIVE_READY = "LIVE_READY"
    COMMENT_INPUT_READY = "COMMENT_INPUT_READY"
    CAPTCHA = "CAPTCHA"
    VERIFY = "VERIFY"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    SECURITY_CHALLENGE = "SECURITY_CHALLENGE"
    UNKNOWN_SCREEN = "UNKNOWN_SCREEN"


class Device(DeviceModel):
    serial: str = Field(pattern=SERIAL_PATTERN)
    state: Literal["CONNECTED", "UNAUTHORIZED", "OFFLINE"]


class DeviceInspection(DeviceModel):
    adb: Literal["AVAILABLE", "UNAVAILABLE"] = "UNAVAILABLE"
    device: Literal["CONNECTED", "UNAUTHORIZED", "OFFLINE", "MISSING", "MULTIPLE_DEVICES", "READY"] = "MISSING"
    devices: tuple[Device, ...] = ()
    serial: str | None = Field(default=None, pattern=SERIAL_PATTERN)
    package: Literal["com.zhiliaoapp.musically"] | None = None
    foreground_activity: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9._$]+$")
    screen: ScreenState = ScreenState.DEVICE_DISCONNECTED
    comment_input_found: bool = False
    send_button_found: bool = False
    security_state: bool = False
    account_verified: bool = False
    live_verified: bool = False
    profile_verified: bool = False
    interaction_available: bool = False
    reason: Literal["not_checked", "adb_unavailable", "device_missing", "multiple_devices", "unauthorized",
        "offline", "adb_failed", "tiktok_not_running", "security_ui", "unknown_screen", "profile_unverified",
        "account_unverified", "live_unverified", "input_unavailable", "ready"] = "not_checked"

    @property
    def ready(self):
        return (self.device == "READY" and self.screen == ScreenState.COMMENT_INPUT_READY and
                self.account_verified and self.live_verified and self.profile_verified and
                self.interaction_available and not self.security_state)


class UIActionBackend(Protocol):
    """Future reviewed accessibility/native adapter; never installed automatically.

    prepare_exact must target a semantic node, return a freshly read exact field
    value, and never press Send. click_send may perform ONE action, no retries.
    No production backend/profile is shipped without device verification.
    """
    def supports(self, text: str) -> bool: ...
    async def prepare_exact(self, serial: str, resource_id: str, text: str) -> str: ...
    # Recheck guard after all backend awaits, immediately before ONE native action.
    # False means the action was never issued. No retry or coordinate fallback.
    async def click_send(self, serial: str, resource_id: str, guard: Callable[[], bool]) -> bool: ...
    async def aclose(self) -> None: ...


class AndroidOutcome(DeviceModel):
    status: Literal["not_sent", "unknown", "needs_operator"]
    reason: Literal["physical_delivery_disabled", "device_unavailable", "security_ui", "unknown_screen",
        "account_unverified", "live_unverified", "profile_unverified", "unicode_entry_unavailable",
        "rejected", "interrupted"]
