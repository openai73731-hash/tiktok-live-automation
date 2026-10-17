"""Android-only durable receipts and acknowledgements; delivery rows never change."""
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import json
import sqlite3

from app.delivery.registry import REGISTRY_NAME, read_store

NAME = "android-delivery-evidence.sqlite3"


def state_directory(service):
    # Injected journals stay isolated beside their own database. The production
    # factory always uses settings.state_dir for the unified journal and sidecar.
    if service.journal is not None and service.journal.path.parent.resolve() != service.settings.state_dir.resolve():
        return service.journal.path.parent
    return service.settings.state_dir


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def evidence_db(state_dir, *, write=False):
    path = state_dir / NAME
    if not write and not path.exists():
        yield None
        return
    if write: path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path if write else path.resolve().as_uri() + "?mode=ro", uri=not write,
        isolation_level=None, timeout=.1)
    try:
        if write:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS acknowledgements (store TEXT, operation_id TEXT, row_hash TEXT NOT NULL, acknowledged_at TEXT NOT NULL, PRIMARY KEY(store,operation_id))")
            db.execute("CREATE TABLE IF NOT EXISTS echo_receipts (identity TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE)")
        yield db
    finally: db.close()


def rows(service):
    result, paths = [], set()
    with ExitStack() as stack:
        stores = []
        if service.journal is not None:
            stores.append((service.journal.path.name, service.journal))
            paths.add(service.journal.path.resolve())
        for name in (REGISTRY_NAME, "delivery.sqlite3", "windows-delivery.sqlite3"):
            path = state_directory(service) / name
            if path.resolve() in paths: continue
            reader = stack.enter_context(read_store(path))
            if reader is not None: stores.append((name, reader))
        for name, store in stores:
            for row in store.connection.execute("SELECT * FROM operations"):
                value = dict(row)
                result.append((name, value, digest(value)))
    return result


def acknowledgements(service):
    with evidence_db(state_directory(service)) as db:
        return {(name, operation): row_hash for name, operation, row_hash in
            db.execute("SELECT store,operation_id,row_hash FROM acknowledgements")} if db else {}


def unresolved(service, *, allow_rejected=False):
    accepted = acknowledgements(service)
    for name, row, row_hash in rows(service):
        status = row.get("status")
        if status in {"confirmed", "not_sent"}: continue
        if status == "unknown" and accepted.get((name, row["operation_id"])) == row_hash: continue
        if (allow_rejected and status == "needs_operator" and row.get("mode") == "MANUAL"
                and row.get("reason") == "rejected" and row.get("device_serial") == (service.controller.serial or service.controller.last.serial)): continue
        return True
    return False


def unknown_snapshot(service):
    unknown = [(name, row, row_hash) for name, row, row_hash in rows(service) if row.get("status") == "unknown"]
    return unknown, digest([(name, row["operation_id"], row_hash) for name, row, row_hash in unknown])


def acknowledge(service, expected_revision, account, serial):
    unknown, revision = unknown_snapshot(service)
    if not unknown or revision != expected_revision: raise ValueError("UNKNOWN snapshot changed")
    for _, row, _ in unknown:
        if row.get("device_serial") != serial or row.get("target_account", "").casefold() != account:
            raise ValueError("UNKNOWN belongs to another binding")
    # Refuse any live/crash reservation or unresolved fill, including legacy stores.
    if any(row.get("status") in {"reserved", "filled"} for _, row, _ in rows(service)):
        raise ValueError("Active or unresolved delivery")
    with evidence_db(state_directory(service), write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for name, row, row_hash in unknown:
                db.execute("INSERT INTO acknowledgements VALUES (?,?,?,?) ON CONFLICT(store,operation_id) DO UPDATE SET row_hash=excluded.row_hash,acknowledged_at=excluded.acknowledged_at",
                    (name, row["operation_id"], row_hash, datetime.now(UTC).isoformat()))
            if unknown_snapshot(service)[1] != revision: raise ValueError("UNKNOWN changed during acknowledgement")
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction: db.execute("ROLLBACK")
            raise


def claim_echo(service, account, message_id, operation_id):
    with evidence_db(state_directory(service), write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT count(*) FROM echo_receipts").fetchone()[0] >= 10_000:
                raise ValueError("Echo receipt journal full")
            db.execute("INSERT INTO echo_receipts VALUES (?,?)", (digest((account, message_id)), operation_id))
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction: db.execute("ROLLBACK")
            raise
