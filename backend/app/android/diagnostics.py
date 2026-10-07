"""Explicit local diagnostics only. Raw hierarchy never belongs in browser data."""
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import stat
from uuid import uuid4

from app.android.models import CaptureResult

CAPTURE_ROOT = Path("diagnostics/android")
MAX_CAPTURES = 20


class CaptureError(ValueError):
    def __init__(self): super().__init__("UI capture unavailable")


def guard_directory(path):
    for candidate in (path, *path.absolute().parents):
        try: info = candidate.lstat()
        except FileNotFoundError: continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise CaptureError()


def save_capture(xml, state, model, android):
    payload = xml.encode("utf-8")
    if not payload or len(payload) > 524288: raise CaptureError()
    root = CAPTURE_ROOT.absolute()
    guard_directory(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    guard_directory(root)
    for count, _ in enumerate(root.iterdir(), 1):
        if count >= MAX_CAPTURES: raise CaptureError()
    capture_id = uuid4().hex
    directory = root / capture_id
    directory.mkdir(mode=0o700)
    metadata = dict(timestamp=datetime.now(UTC).isoformat(), device_model=model, android_version=android,
        serial_sha256=sha256(state.serial.encode("ascii")).hexdigest(), package=state.package,
        version=state.version, version_code=state.version_code, foreground_activity=state.foreground_activity,
        client_variant=state.client_variant.value, profile_verified=state.profile_verified,
        hierarchy_sha256=sha256(payload).hexdigest())
    try:
        guard_directory(directory)
        with (directory / "hierarchy.xml").open("xb") as stream: stream.write(payload)
        with (directory / "metadata.json").open("x", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False, indent=2)
    except BaseException:
        # Remove only files belonging to this newly created capture, never older diagnostics.
        for name in ("hierarchy.xml", "metadata.json"):
            (directory / name).unlink(missing_ok=True)
        directory.rmdir()
        raise
    return CaptureResult(capture_id=capture_id)
