"""Transactional, persistent operation reservations. Contains no reply text.

Never wired to a web delivery route. The fixed production location is ignored;
tests inject a temporary location. Retention fails closed at the bounded cap.
"""
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Protocol

from app.delivery.models import DeliveryRequest, DeliveryResult, FrozenModel

JOURNAL_PATH = Path("data/delivery.sqlite3")


class JournalConflict(ValueError):
    pass


class Operation(FrozenModel):
    operation_id: str
    idempotency_key: str
    suggestion_id: str
    suggestion_revision: str
    comment_identity: str
    approval_id: str
    reply_hash: str
    live_session_id: str
    target_account: str
    runtime_revision: str
    created_at: str
    status: str
    reason: str | None


class DeliveryOperationStore(Protocol):
    def reserve(self, request) -> tuple[Operation, bool]: ...
    def finish(self, operation_id: str, result: DeliveryResult) -> Operation: ...
    def has_operation(self, suggestion_id: str) -> bool: ...


class SQLiteDeliveryOperationStore:
    def __init__(self, path=JOURNAL_PATH, *, capacity=10_000):
        if type(capacity) is not int or not 1 <= capacity <= 10_000:
            raise ValueError("Invalid journal capacity")
        self.path, self.capacity = Path(path), capacity
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS operations (
            operation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
            suggestion_id TEXT NOT NULL UNIQUE, suggestion_revision TEXT NOT NULL,
            comment_identity TEXT NOT NULL UNIQUE, approval_id TEXT NOT NULL, reply_hash TEXT NOT NULL,
            live_session_id TEXT NOT NULL, target_account TEXT NOT NULL,
            runtime_revision TEXT NOT NULL, created_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('reserved','confirmed','not_sent','unknown','needs_operator')),
            reason TEXT)""")
        # A reserved row itself blocks retry after restart. Do not rewrite it on
        # connection open: another in-process coordinator could still own it.

    def _operation(self, row):
        return Operation.model_validate(dict(row))

    def has_operation(self, suggestion_id):
        return self.connection.execute("SELECT 1 FROM operations WHERE suggestion_id=?", (suggestion_id,)).fetchone() is not None

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
                    "target_account": request.target_account, "runtime_revision": request.active_runtime_revision}
                if any(row[key] != value for key, value in fields.items()):
                    raise JournalConflict("Idempotency key binding conflict")
                db.execute("COMMIT")
                return self._operation(row), False
            if db.execute("SELECT 1 FROM operations WHERE suggestion_id=? OR comment_identity=?",
                          (request.suggestion_id, request.comment_identity)).fetchone():
                raise JournalConflict("Duplicate operation")
            if db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] >= self.capacity:
                raise JournalConflict("Operation journal full; operator required")
            values = (request.operation_id, request.idempotency_key, request.suggestion_id,
                request.suggestion_revision, request.comment_identity, request.approval_id,
                request.suggested_reply_hash, request.live_session_id, request.target_account,
                request.active_runtime_revision, datetime.now(UTC).isoformat(), "reserved", None)
            db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
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

    def close(self):
        self.connection.close()
