"""One transactional journal for new deliveries; legacy reservations stay consumed.

Read-only legacy consultation needs no destructive migration and never creates a
database on status. All transports reserve against the same unique stable key.
An unreadable existing legacy journal fails closed. Old backends must be stopped.
"""
from contextlib import contextmanager
import sqlite3

from app.delivery.journal import SQLiteDeliveryOperationStore

REGISTRY_NAME = "unified-delivery.sqlite3"


@contextmanager
def read_store(path):
    if not path.exists():
        yield None
        return
    reader = SQLiteDeliveryOperationStore.__new__(SQLiteDeliveryOperationStore)
    reader.connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)
    reader.connection.row_factory = sqlite3.Row
    try: yield reader
    finally: reader.connection.close()


class UnifiedDeliveryOperationStore(SQLiteDeliveryOperationStore):
    def __init__(self, state_dir):
        self.legacy_paths = (state_dir / "delivery.sqlite3", state_dir / "windows-delivery.sqlite3")
        super().__init__(state_dir / REGISTRY_NAME)

    def _duplicate(self, *args, **kwargs):
        if super()._duplicate(*args, **kwargs): return True
        for path in self.legacy_paths:
            with read_store(path) as reader:
                if reader and reader._duplicate(*args, **kwargs): return True
        return False

    def unresolved(self, *, excluding=None):
        if super().unresolved(excluding=excluding): return True
        for path in self.legacy_paths:
            with read_store(path) as reader:
                if reader and reader.unresolved(): return True
        return False


def consumed(state_dir, envelope):
    for name in (REGISTRY_NAME, "delivery.sqlite3", "windows-delivery.sqlite3"):
        with read_store(state_dir / name) as reader:
            if reader and reader.has_envelope_operation(envelope): return True
    return False
