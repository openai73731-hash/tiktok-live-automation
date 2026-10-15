"""Vendor-independent models."""
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from time import monotonic
from dataclasses import dataclass
import threading

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PrivateAttr


_ingestion_lock = threading.Lock()
_ingestion_sequence = 0


def ingestion_boundary(clock=monotonic, *, receipt=False):
    """Order receipt and AUTO transitions even when the monotonic clock ties."""
    global _ingestion_sequence
    with _ingestion_lock:
        if receipt:
            _ingestion_sequence += 1
        return clock(), _ingestion_sequence


@dataclass(frozen=True)
class IngestionReceipt:
    monotonic: float
    sequence: int
    account: str
    session_id: str
    runtime_revision: str
    broadcaster_user_id: str | None
    message_id: str | None


class IngestionLedger:
    """Keep first SDK receipts across APPLY/full reconnect for this backend.

    No eviction that could rejuvenate an old ID. At the bounded cap, new IDs
    retain manual review but cannot gain AUTO authority in this process.
    """
    def __init__(self, capacity=10_000):
        self.receipts = {}
        self.capacity = capacity

    def bind(self, comment, account, session_id, runtime_revision, broadcaster_user_id):
        comment.bind_ingestion(account, session_id, runtime_revision, broadcaster_user_id)
        if not comment.message_id:
            comment._ingestion_receipt = None
            return
        key = account.casefold(), broadcaster_user_id, comment.message_id
        if key in self.receipts:
            comment._ingestion_receipt = self.receipts[key]
        elif len(self.receipts) < self.capacity:
            self.receipts[key] = comment._ingestion_receipt
        else:
            comment._ingestion_receipt = None


class Comment(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)
    # Process-local receipt clock, never serialized or supplied by the browser.
    _received_monotonic: float = PrivateAttr(default_factory=lambda: monotonic())
    _ingestion_sequence: int = PrivateAttr(default=0)
    _ingestion_receipt: IngestionReceipt | None = PrivateAttr(default=None)
    # Only the owned real SDK callback sets this; JSON/AI/browser cannot supply it.
    _sdk_receipt: tuple[object, int] | None = PrivateAttr(default=None)
    _sdk_text_exact: bool = PrivateAttr(default=False)
    username: str = Field(min_length=1)
    user_id: str | None = None
    author_handle: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._]{1,24}$")
    server_created_at: AwareDatetime | None = None
    message_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    text: str = Field(min_length=1)
    # Local receipt time, in UTC; not an inferred TikTok server timestamp.
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    source: Literal["tiktok", "playground"] = "tiktok"

    def model_post_init(self, context):
        self._received_monotonic, self._ingestion_sequence = ingestion_boundary(receipt=True)

    def bind_ingestion(self, account, session_id, runtime_revision, broadcaster_user_id):
        # Called by the owned listener callback BEFORE queueing. Never restamp a
        # reused comment on reconnect, AI retry or reapproval. Not a JSON field.
        receipt = IngestionReceipt(self._received_monotonic, self._ingestion_sequence,
            account, session_id, runtime_revision, broadcaster_user_id, self.message_id)
        if self._ingestion_receipt is None:
            self._ingestion_receipt = receipt
        elif self._ingestion_receipt != receipt:
            raise ValueError("ingestion_binding_changed")


class LiveState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    RETRYING = "reconnecting"  # Compatibility for existing consumers.
    OFFLINE = "offline"
    ENDED = "ended"
    OPERATOR_REQUIRED = "operator_required"
    NEED_OPERATOR = "operator_required"
    STOPPED = "stopped"


class LiveStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    state: LiveState = LiveState.IDLE
    username: str
    mode: Literal["real", "fake", "replay"]
    reason: str | None = None
    terminal_state: LiveState | None = None
    cleanup_state: Literal["not_started", "running", "complete", "failed"] = "not_started"
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
