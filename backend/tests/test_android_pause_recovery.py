"""Real Admin/suggestions/SQLite/controller, fake listener and native ADB only."""
import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest

from app.android.adb import AdbCommand, AdbError, AdbTransport
from app.delivery.journal import SQLiteDeliveryOperationStore
from app.delivery.models import DeliveryResult
from app.live.models import Comment
from app.logging_config import LOGGER_NAME
from test_manual_send_rc import ACCOUNT, xml
from test_unified_transports import manual_fill_api, no_physical, request

INPUT = {AdbCommand.SELECT_ALL, AdbCommand.DELETE, AdbCommand.TEXT, AdbCommand.TAP_SEND, AdbCommand.TAB, AdbCommand.ACTIVATE}


async def rejected(fixture):
    _, client, service, transport, envelope, body = fixture
    transport.mismatch = True
    response = await client.post("/api/admin/delivery/fill", json=body)
    assert response.json() == {"status":"needs_operator", "reason":"rejected"}
    assert service.paused and service._pause_reason == "manual_rejected"
    assert service.journal.lookup(body["idempotency_key"]).status == "needs_operator"
    assert service.consumed(envelope)
    transport.mismatch = False
    return service.journal.lookup(body["idempotency_key"])


async def disconnect_and_verify(fixture):
    app, client, service, transport, _, _ = fixture
    runtime = app.state.operator_runtime
    await runtime.live.disconnect(runtime.live.session_id)
    transport.xml = xml("profile")
    result = await client.post("/api/admin/device/verify-account", json={})
    assert result.status_code == 200 and result.json()["inspection"]["account_verified"]
    assert service._verified_runtime_revision == runtime.active_runtime_revision


async def new_reply(app, client, msg):
    live = app.state.operator_runtime.live.service
    comment = Comment(username="viewer", text="Is it automatic?", message_id=msg)
    live.on_receipt(comment)
    record = await live.on_comment(comment)
    envelope = app.state.admin.suggestions.get(record.id)
    approved = await client.post(f"/api/admin/suggestions/{record.id}/approve",
        json={"expected_suggestion_revision":envelope.suggestion_revision})
    assert approved.status_code == 200
    body = dict(suggestion_id=record.id, expected_suggestion_revision=envelope.suggestion_revision,
        approval_id=approved.json()["approval"]["approval_id"], idempotency_key="fresh_"+msg)
    return envelope, body


async def test_rejected_pause_is_not_cleared_by_comment_profile_or_reconnect(manual_fill_api):
    app, client, service, transport, _, _ = manual_fill_api
    await rejected(manual_fill_api)
    before = len([c for c, _ in transport.calls if c in INPUT])
    e, body = await new_reply(app, client, "4322")
    review = (await client.get(f"/api/admin/suggestions/{e.suggestion_id}")).json()
    assert review["eligibility"]["reason"] == "operator_paused"
    assert (await client.post("/api/admin/delivery/fill", json=body)).json()["reason"] == "delivery_paused"
    await disconnect_and_verify(manual_fill_api)
    assert service.paused
    assert (await client.post("/api/admin/live/connect", json={"username":ACCOUNT})).status_code == 202
    await app.state.operator_runtime.live.service.started.wait()
    assert service.paused
    status = (await client.get("/api/admin/delivery/status")).json()
    assert status["delivery"] == "DELIVERY PAUSED — RE-PREPARE REQUIRED" and status["auto_state"] == "AUTO OFF"
    assert len([c for c, _ in transport.calls if c in INPUT]) == before


async def test_explicit_prepare_recovers_only_new_cycle_and_preserves_consumed(manual_fill_api):
    from test_tiktok_you import empty_reviewed_composer
    import xml.etree.ElementTree as ET
    app, client, service, transport, old, old_body = manual_fill_api
    operation = await rejected(manual_fill_api)
    await disconnect_and_verify(manual_fill_api)
    runtime = app.state.operator_runtime
    before_revision = runtime.active_runtime_revision
    before_row = tuple(service.journal.connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone())
    service.acceptance_spent = True
    transport.calls.clear()
    prepared = await client.post("/api/admin/delivery/prepare", json={})
    assert prepared.status_code == 200, prepared.text
    assert not prepared.json()["paused"] and not prepared.json()["busy"]
    assert runtime.active_runtime_revision != before_revision and prepared.json()["active_runtime_revision"] == runtime.active_runtime_revision
    assert prepared.json()["auto_state"] == "AUTO OFF" and service.mode == "MANUAL" and service.armed_binding is None
    assert service.acceptance_spent and not INPUT & {c for c, _ in transport.calls}
    assert tuple(service.journal.connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()) == before_row
    assert service.consumed(old)
    assert (await client.post(f"/api/admin/suggestions/{old.suggestion_id}/approve",
        json={"expected_suggestion_revision":old.suggestion_revision})).status_code == 409
    assert (await client.post("/api/admin/delivery/fill", json=old_body)).status_code == 409
    # Optional strict Profile proof still works; preparation itself proves no account.
    assert not prepared.json()["inspection"]["account_verified"]
    assert (await client.post("/api/admin/device/verify-account", json={})).status_code == 200
    assert (await client.post("/api/admin/live/connect", json={"username":ACCOUNT})).status_code == 202
    await runtime.live.service.started.wait()
    repeated = Comment(username="viewer", text="Is it automatic?", message_id="4321")
    runtime.live.service.on_receipt(repeated)
    repeated_record = await runtime.live.service.on_comment(repeated)
    repeated_envelope = app.state.admin.suggestions.get(repeated_record.id)
    assert service.consumed(repeated_envelope)
    assert (await client.post(f"/api/admin/suggestions/{repeated_record.id}/approve",
        json={"expected_suggestion_revision":repeated_envelope.suggestion_revision})).status_code == 409
    envelope, body = await new_reply(app, client, "4323")
    transport.xml = ET.tostring(empty_reviewed_composer(), encoding="unicode")
    assert (await client.post("/api/admin/device/inspect", json={})).json()["fill_ready"]
    transport.calls.clear()
    response = await client.post("/api/admin/delivery/fill", json=body)
    assert response.json() == {"status":"filled", "reason":"filled_waiting_user_send"}
    assert [a["text"] for c, a in transport.calls if c == AdbCommand.TEXT] == [envelope.suggested_reply]
    assert not {AdbCommand.TAP_SEND, AdbCommand.TAB, AdbCommand.ACTIVATE} & {c for c, _ in transport.calls}
    assert service.mode == "MANUAL" and service.consumed(old)


@pytest.mark.parametrize("failure", ["listener", "auto", "account", "device", "install", "security",
    "pending", "coordinator", "native", "reserved", "filled", "unknown", "other_pause", "legacy_unknown", "legacy_unreadable",
    "legacy_windows_rejected", "runtime_race", "multiple"])
async def test_prepare_refuses_unsafe_recovery_without_false_pass(manual_fill_api, failure):
    app, client, service, transport, envelope, body = manual_fill_api
    operation = await rejected(manual_fill_api)
    if failure != "listener": await disconnect_and_verify(manual_fill_api)
    runtime = app.state.operator_runtime
    if failure == "auto": service.mode = "AUTOMATIC"
    elif failure == "account":
        context = service.context
        service.context = lambda: context().model_copy(update={"target_account":"foreign"})
    elif failure == "device": transport.serial = "foreign"
    elif failure == "install":
        async def change_install(command):
            if command == AdbCommand.DUMP: transport.info = transport.info.replace("10:00:00", "11:00:00")
        transport.after = change_install
    elif failure == "security": transport.xml = transport.xml.replace("Добавить описание", "captcha")
    elif failure == "pending": service.pending_fill = (body, None, None, None, operation.operation_id)
    elif failure == "coordinator": service.coordinator.closed = False
    elif failure == "native": transport.processes = {object()}
    elif failure in {"reserved", "filled", "unknown"}:
        service.journal.connection.execute("UPDATE operations SET status=? WHERE operation_id=?", (failure, operation.operation_id))
    elif failure == "other_pause":
        service.paused = True
        service._verified_runtime_revision = runtime.active_runtime_revision
    elif failure in {"legacy_unknown", "legacy_windows_rejected"}:
        legacy = SQLiteDeliveryOperationStore(service.settings.state_dir / "delivery.sqlite3")
        approval = app.state.admin.suggestions.approvals[envelope.suggestion_id]
        try:
            legacy_request = request(envelope, approval, key="legacy_unknown")
            if failure == "legacy_windows_rejected": legacy_request = legacy_request.model_copy(update={"device_serial":None})
            older, reserved = legacy.reserve(legacy_request)
            assert reserved
            legacy.finish(older.operation_id, DeliveryResult(status="needs_operator", reason="rejected") if failure == "legacy_windows_rejected"
                else DeliveryResult(status="unknown", reason="timeout"))
        finally: legacy.close()
    elif failure == "legacy_unreadable":
        (service.settings.state_dir / "delivery.sqlite3").write_bytes(b"private invalid sqlite")
    elif failure == "runtime_race":
        async def change_runtime(command):
            if command == AdbCommand.DUMP: runtime.active_runtime_revision = "e" * 32
        transport.after = change_runtime
    elif failure == "multiple":
        original = transport.run
        async def multiple(command, **kwargs):
            if command == AdbCommand.DEVICES: return "List of devices attached\n903ed867 device\nforeign device\n"
            return await original(command, **kwargs)
        transport.run = multiple
    revision = runtime.active_runtime_revision
    transport.calls.clear()
    result = await client.post("/api/admin/delivery/prepare", json={})
    assert result.status_code == 409, result.text
    assert service.paused and runtime.active_runtime_revision == ("e" * 32 if failure == "runtime_race" else revision)
    assert not INPUT & {c for c, _ in transport.calls}
    assert service.consumed(envelope)
    assert "private invalid" not in result.text
    if failure == "coordinator": service.coordinator.closed = True


async def test_lazy_journal_rejected_pause_requires_explicit_recovery(manual_fill_api):
    _, client, service, _, old, _ = manual_fill_api
    await rejected(manual_fill_api)
    await disconnect_and_verify(manual_fill_api)
    service.journal.close()
    service.journal = None
    service.paused = False  # Simulate a newly constructed adapter before lazy journal load.
    service.prepare(fill=True)
    assert service.paused and service._pause_reason == "manual_rejected"
    result = await client.post("/api/admin/delivery/prepare", json={})
    assert result.status_code == 200 and not result.json()["paused"]
    assert result.json()["mobile_prepared"] and not result.json()["inspection"]["account_verified"]
    assert service.consumed(old) and service.mode == "MANUAL"


async def test_manual_rejection_logs_internal_reason_without_reply(manual_fill_api, caplog, monkeypatch):
    _, _, _, _, envelope, _ = manual_fill_api
    logger = logging.getLogger(LOGGER_NAME + ".android")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    caplog.set_level(logging.INFO)
    await rejected(manual_fill_api)
    records = [r.fields for r in caplog.records if r.getMessage() == "android_manual_text_entry"]
    assert records and any(r["rejection_reason"] == "text_readback_mismatch" for r in records)
    assert any(r["post_readback_result"] == "mismatch" for r in records)
    for record in records:
        assert record["text_entry_method"] == "adb_ascii_focused"
        assert record["input_target_resource_id"].endswith(":id/h4k")
        assert {"target_focused_before_input", "category", "exit_code", "stdout_present", "stderr_present", "stderr_miui_uiautomator_noise"} <= record.keys()
        assert envelope.suggested_reply not in json.dumps(record) and "corrupt" not in json.dumps(record)


@pytest.mark.parametrize("code,stderr,category,noise", [(0,b"", "success",False),
    (0,b"arbitrary warning with private content", "success",False),
    (0,b"WARNING: linker: unused DT entry", "success",True),
    (1,b"WARNING: linker: unused DT entry", "exit_nonzero",True)])
@pytest.mark.parametrize("command", [AdbCommand.DUMP, AdbCommand.TEXT])
async def test_adb_result_diagnostics_do_not_reclassify_stderr(tmp_path, monkeypatch, code, stderr, category, noise, command):
    class Process:
        returncode = code
        def __init__(self, reset=False):
            self.returncode = 0 if reset else code
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.stdout.feed_data(b"" if reset else b"UI hierarchy dumped to: /data/local/tmp/tiktok-live-hierarchy.xml")
            self.stderr.feed_data(b"" if reset else stderr)
            self.stdout.feed_eof(); self.stderr.feed_eof()
        async def wait(self): return self.returncode
    path = tmp_path / "adb.exe"; path.touch()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *args, **kwargs: Process(reset="rm" in args)))
    transport = AdbTransport(path)
    args = {"text":"Private approved reply"} if command == AdbCommand.TEXT else {}
    if code:
        with pytest.raises(AdbError): await transport.run(command, serial="fixture", **args)
    else: assert "UI hierarchy" in await transport.run(command, serial="fixture", **args)
    result = transport.last_command_result
    assert result == dict(command=command.value,category=category,exit_code=code,stdout_present=True,
        stderr_present=bool(stderr),stderr_miui_uiautomator_noise=noise and command == AdbCommand.DUMP)
    assert "private content" not in json.dumps(result) and "Private approved reply" not in json.dumps(result) and not transport.processes
