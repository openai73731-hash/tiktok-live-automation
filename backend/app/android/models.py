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
    HOST_MENU = "HOST_MENU"
    COMMENT_INPUT_READY = "COMMENT_INPUT_READY"
    TRANSIENT_OVERLAY = "TRANSIENT_OVERLAY"
    CAPTCHA = "CAPTCHA"
    VERIFY = "VERIFY"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    SECURITY_CHALLENGE = "SECURITY_CHALLENGE"
    UNKNOWN_SCREEN = "UNKNOWN_SCREEN"


class ClientVariant(StrEnum):
    UNKNOWN = "UNKNOWN"
    OFFICIAL = "OFFICIAL"
    TIKTOK_YOU = "TIKTOK_YOU"


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
    client_variant: ClientVariant = ClientVariant.UNKNOWN
    version: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
    version_code: str | None = Field(default=None, pattern=r"^[0-9]{1,12}$")
    screen: ScreenState = ScreenState.DEVICE_DISCONNECTED
    comment_input_found: bool = False
    send_button_found: bool = False
    security_state: bool = False
    account_verified: bool = False
    live_verified: bool = False
    profile_verified: bool = False
    interaction_available: bool = False
    send_control_state: Literal["UNAVAILABLE", "SEND_CONTROL_CANDIDATE", "VERIFIED"] = "UNAVAILABLE"
    send_confirmation_token: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    reason: Literal["not_checked", "adb_unavailable", "device_missing", "multiple_devices", "unauthorized",
        "offline", "adb_failed", "tiktok_not_running", "security_ui", "unknown_screen", "profile_unverified", "hierarchy_unavailable", "profile_mismatch",
        "account_unverified", "live_unverified", "input_unavailable", "send_control_unverified", "transient_overlay", "ready"] = "not_checked"

    @property
    def ready(self):
        return (self.device == "READY" and self.screen == ScreenState.COMMENT_INPUT_READY and
                self.account_verified and self.live_verified and self.profile_verified and
                self.interaction_available and not self.security_state)


    @property
    def fill_ready(self):
        return (self.screen == ScreenState.COMMENT_INPUT_READY and self.comment_input_found and
                self.account_verified and self.live_verified and self.profile_verified and
                self.interaction_available and not self.security_state)

    @property
    def manual_fill_candidate(self):
        # Field recognition is not programmatic SEND/LIVE authority. The
        # controller additionally binds this candidate to the current session.
        return (self.device in {"CONNECTED", "READY"} and self.screen == ScreenState.COMMENT_INPUT_READY
                and self.comment_input_found and self.account_verified and self.profile_verified
                and self.interaction_available and not self.security_state)


class TextEntryBackend(Protocol):
    """Explicit character capability and exact readback, including future Unicode adapters."""
    def supports(self, text: str) -> bool: ...
    async def prepare_exact(self, serial: str, resource_id: str, text: str) -> str: ...


class UIActionBackend(TextEntryBackend, Protocol):
    """Reviewed UI adapter; never installed automatically.

    prepare_exact must target a semantic node, return a freshly read exact field
    value, and never press Send. click_send may perform ONE action, no retries.
    ASCII entry uses a manually focused verified field and selector-resolved activation.
    """
    def supports(self, text: str) -> bool: ...
    async def prepare_exact(self, serial: str, resource_id: str, text: str) -> str: ...
    # Recheck guard after all backend awaits, immediately before ONE native action.
    # False means the action was never issued. No retry or guessed-coordinate fallback.
    async def click_send(self, serial: str, resource_id: str, guard: Callable[[], bool]) -> bool: ...
    async def aclose(self) -> None: ...


class AndroidOutcome(DeviceModel):
    status: Literal["filled", "confirmed", "not_sent", "unknown", "needs_operator"]
    reason: Literal["physical_delivery_disabled", "device_unavailable", "security_ui", "unknown_screen",
        "hierarchy_unavailable",
        "account_unverified", "live_unverified", "profile_unverified", "unicode_entry_unavailable",
        "rejected", "interrupted", "filled_waiting_user_send", "feed_confirmed", "sdk_echo_confirmed",
        "open_phone_composer", "transient_overlay", "confirmation_ambiguous"]


class CaptureResult(DeviceModel):
    status: Literal["captured"] = "captured"
    capture_id: str = Field(pattern=r"^[a-f0-9]{32}$")
