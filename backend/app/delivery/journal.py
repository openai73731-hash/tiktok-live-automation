"""Transactional, persistent operation reservations. Contains no reply text.

Created lazily for explicit manual fill or separately armed automatic delivery. The fixed production location is ignored;
tests inject a temporary location. Retention fails closed at the bounded cap.
"""
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Protocol, Literal
from pydantic import Field

from app.delivery.models import (DeliveryRequest, DeliveryResult, FrozenModel, DeliveryReason,
    OpaqueId, Revision, Account)
from app.delivery.suggestions import digest

JOURNAL_PATH = Path("data/delivery.sqlite3")


class JournalConflict(ValueError):
    pass


class Operation(FrozenModel):
    operation_id: OpaqueId
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    suggestion_id: OpaqueId
    suggestion_revision: Revision
    comment_identity: Revision
    approval_id: OpaqueId
    reply_hash: Revision
    live_session_id: OpaqueId
    target_account: Account
    runtime_revision: OpaqueId
    created_at: str = Field(max_length=40)
    status: Literal["reserved", "filled", "confirmed", "not_sent", "unknown", "needs_operator"]
    reason: DeliveryReason | None
    device_serial: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    mode: Literal["MANUAL", "AUTOMATIC", "AUTO"] = "MANUAL"
    durable_delivery_key: Revision | None = None


class DeliveryOperationStore(Protocol):
    def reserve(self, request) -> tuple[Operation, bool]: ...
    def finish(self, operation_id: str, result: DeliveryResult) -> Operation: ...
    def has_operation(self, suggestion_id: str, comment_identity: str | None = None) -> bool: ...
    def lookup(self, key: str) -> Operation | None: ...
    def has_envelope_operation(self, envelope) -> bool: ...


class SQLiteDeliveryOperationStore:
    def __init__(self, path=JOURNAL_PATH, *, capacity=10_000):
        if type(capacity) is not int or not 1 <= capacity <= 10_000:
            raise ValueError("Invalid journal capacity")
        self.path, self.capacity = Path(path), capacity
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=0.1)
        try:
            self._initialize()
        except BaseException:
            self.connection.close()
            raise

    def _initialize(self):
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS operations (
            operation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
            suggestion_id TEXT NOT NULL UNIQUE, suggestion_revision TEXT NOT NULL,
            comment_identity TEXT NOT NULL UNIQUE, approval_id TEXT NOT NULL, reply_hash TEXT NOT NULL,
            live_session_id TEXT NOT NULL, target_account TEXT NOT NULL,
            runtime_revision TEXT NOT NULL, created_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('reserved','filled','confirmed','not_sent','unknown','needs_operator')),
            reason TEXT)""")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            columns = {r[1] for r in self.connection.execute("PRAGMA table_info(operations)")}
            if "device_serial" not in columns:
                self.connection.execute("ALTER TABLE operations ADD COLUMN device_serial TEXT")
            if "mode" not in columns:
                self.connection.execute("ALTER TABLE operations ADD COLUMN mode TEXT NOT NULL DEFAULT 'MANUAL'")
            schema = self.connection.execute("SELECT sql FROM sqlite_master WHERE name='operations'").fetchone()[0]
            if "'filled'" not in schema:
                # SQLite cannot widen CHECK in place. Copy every bounded metadata row
                # transactionally, preserving all three unique dedup identities.
                self.connection.execute("ALTER TABLE operations RENAME TO operations_stage4b3")
                self.connection.execute(schema.replace(
                    "'reserved','confirmed'", "'reserved','filled','confirmed'"))
                migrated_columns = ",".join(r[1] for r in self.connection.execute("PRAGMA table_info(operations_stage4b3)"))
                self.connection.execute(f"INSERT INTO operations ({migrated_columns}) SELECT {migrated_columns} FROM operations_stage4b3")
                self.connection.execute("DROP TABLE operations_stage4b3")
            if "durable_delivery_key" not in columns:
                self.connection.execute("ALTER TABLE operations ADD COLUMN durable_delivery_key TEXT")
            self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS durable_delivery_key_unique ON operations(durable_delivery_key)")
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction: self.connection.execute("ROLLBACK")
            raise
        # A reserved row itself blocks retry after restart. Do not rewrite it on
        # connection open: another in-process coordinator could still own it.

    def _operation(self, row):
        return Operation.model_validate(dict(row))

    def has_operation(self, suggestion_id, comment_identity=None):
        return self.connection.execute("SELECT 1 FROM operations WHERE suggestion_id=? OR comment_identity=?", (suggestion_id, comment_identity)).fetchone() is not None

    def _duplicate(self, suggestion_id, comment_identity, durable_key=None, *, source=None, message_id=None, account=None):
        columns = {r[1] for r in self.connection.execute("PRAGMA table_info(operations)")}
        durable = "durable_delivery_key" in columns
        query = "SELECT 1 FROM operations WHERE suggestion_id=? OR comment_identity=?"
        args = (suggestion_id, comment_identity)
        if durable:
            query += " OR durable_delivery_key=?"
            args += (durable_key,)
        if self.connection.execute(query, args).fetchone(): return True
        if source == "tiktok" and message_id and account:
            # v1 stored session/account and the hash, but not msg_id. Recompute
            # possible v1 keys without deleting/unlocking any legacy reservation.
            # At most 10,000 metadata rows; no source IDs/text are persisted.
            rows = self.connection.execute("SELECT comment_identity,live_session_id,target_account FROM operations WHERE " +
                ("durable_delivery_key IS NULL AND " if durable else "") + "lower(target_account)=?",
                (account.casefold(),))
            for row in rows:
                legacy = digest({"source": source, "session": row['live_session_id'],
                    "account": row['target_account'], "id": message_id, "fallback": None})
                if legacy == row['comment_identity']: return True
        return False

    def has_envelope_operation(self, envelope):
        return self._duplicate(envelope.suggestion_id, envelope.comment_identity, envelope.durable_delivery_key,
            source=envelope.source, message_id=envelope._source_message_id, account=envelope.target_account)

    def reserve(self, request):
        request = DeliveryRequest.model_validate(request.model_dump())
        db = self.connection
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM operations WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
            if row is not None:
                fields = {"suggestion_id": request.suggestion_id, "suggestion_revision": request.suggestion_revision,
                    "comment_identity": request.comment_identity, "approval_id": request.approval_id,
                    "reply_hash": request.suggested_reply_hash, "live_session_id": request.live_session_id,
                    "target_account": request.target_account, "runtime_revision": request.active_runtime_revision,
                    "device_serial": request.device_serial, "mode": request.mode,
                    "durable_delivery_key": request.durable_delivery_key}
                if row["durable_delivery_key"] is None: fields.pop("durable_delivery_key")
                if any(row[key] != value for key, value in fields.items()):
                    raise JournalConflict("Idempotency key binding conflict")
                db.execute("COMMIT")
                return self._operation(row), False
            if self._duplicate(request.suggestion_id, request.comment_identity, request.durable_delivery_key,
                    source=request.source_kind, message_id=request.source_message_id, account=request.target_account):
                raise JournalConflict("Duplicate operation")
            if db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] >= self.capacity:
                raise JournalConflict("Operation journal full; operator required")
            values = (request.operation_id, request.idempotency_key, request.suggestion_id,
                request.suggestion_revision, request.comment_identity, request.approval_id,
                request.suggested_reply_hash, request.live_session_id, request.target_account,
                request.active_runtime_revision, datetime.now(UTC).isoformat(), "reserved", None, request.device_serial, request.mode)
            db.execute("""INSERT INTO operations (operation_id,idempotency_key,suggestion_id,suggestion_revision,
                comment_identity,approval_id,reply_hash,live_session_id,target_account,runtime_revision,
                created_at,status,reason,device_serial,mode,durable_delivery_key)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values + (request.durable_delivery_key,))
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (request.operation_id,)).fetchone()
            db.execute("COMMIT")
            return self._operation(row), True
        except BaseException:
            if db.in_transaction: db.execute("ROLLBACK")
            raise

    def finish(self, operation_id, result):
        result = DeliveryResult.model_validate(result.model_dump())
        self.connection.execute("UPDATE operations SET status=?, reason=? WHERE operation_id=? AND status='reserved'",
                                (result.status, result.reason, operation_id))
        row = self.connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None: raise JournalConflict("Operation unavailable")
        return self._operation(row)

    def settle_fill(self, operation_id, result):
        result = DeliveryResult.model_validate(result.model_dump())
        if result.status not in {"confirmed", "unknown"}: raise JournalConflict("Invalid fill settlement")
        updated = self.connection.execute("UPDATE operations SET status=?, reason=? WHERE operation_id=? AND status='filled'",
            (result.status, result.reason, operation_id))
        if updated.rowcount != 1: raise JournalConflict("Fill unavailable")

    def close(self):
        self.connection.close()

    def recent(self):
        return tuple(self._operation(r) for r in self.connection.execute(
            "SELECT * FROM operations ORDER BY rowid DESC LIMIT 50"))

    def lookup(self, key):
        row = self.connection.execute("SELECT * FROM operations WHERE idempotency_key=?", (key,)).fetchone()
        return self._operation(row) if row is not None else None

    def unresolved(self, *, excluding=None):
        return self.connection.execute("""SELECT 1 FROM operations
            WHERE status IN ('reserved','filled','unknown','needs_operator')
            AND (? IS NULL OR operation_id != ?) LIMIT 1""", (excluding, excluding)).fetchone() is not None
