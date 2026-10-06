"""Regression checks for the pre-delivery boundary; no external connections."""
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from TikTokLive.events import CommentEvent

from app.admin.service import record_for
from app.admin.operator_config import LocalConfigError, OperatorStore
from app.ai.factory import create_processor
from app.ai.knowledge import ProjectConfig, load_yaml_model
from app.config import Settings
from app.delivery.coordinator import DeliveryCoordinator
from app.delivery.journal import SQLiteDeliveryOperationStore, JournalConflict
from app.delivery.models import ApprovalInput, DeliveryContext, DeliveryResult, DeliveryRequest
from app.delivery.suggestions import (SuggestionStore, SuggestionConflict, DeliveryEligibility, digest, intact)
from app.live.listener import LiveListener
from app.live.models import Comment, LiveState, LiveStatus
from app.main import create_app


def test_transport_disconnect_revokes_connected():
    listener = LiveListener(Settings(tiktok_username="own"), asyncio.Queue())
    listener.on_connect(None)
    listener.on_disconnect(None)
    assert listener.status.state == LiveState.DISCONNECTED
    assert listener.status.terminal_state is None
    assert not listener.connected.is_set()


def test_local_yaml_depth_is_static_error(tmp_path):
    store = OperatorStore(load_yaml_model(Path("config/project.yaml"), ProjectConfig), tmp_path / "operator.local.yaml")
    store.path.write_text("ai: " + "[" * 550 + "0" + "]" * 550, encoding="utf-8")
    with pytest.raises(LocalConfigError, match="Invalid local settings"):
        store.load()


@pytest.mark.parametrize("body", ["ai: " + "[0," * 300 + "0" + "]" * 300,
    "ai: &a {model: *a}", "ai: [" + ",".join("0" for _ in range(300)) + "]",
    "#" + "x" * 16_384, "ai: !!str " + "\udfff"])
def test_local_config_limits_and_aliases(tmp_path, body):
    store = OperatorStore(load_yaml_model(Path("config/project.yaml"), ProjectConfig), tmp_path / "operator.local.yaml")
    store.path.write_bytes(body.encode("utf-8", errors="surrogatepass"))
    with pytest.raises(LocalConfigError): store.load()


@pytest.mark.parametrize("phase", ["halt", "connected", "disconnect", "socket", "heartbeat", "http", "signer"])
async def test_repeated_cancel_at_each_sdk_cleanup_phase(settings, phase):
    from app.lifecycle import finish_owned
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []
    listener = LiveListener(settings, asyncio.Queue())
    async def barrier(name):
        if phase == name:
            entered.set(); await release.wait()
        closed.append(name)
    async def owned(name):
        try: await asyncio.Event().wait()
        finally: await barrier(name)
    tasks = {name: asyncio.create_task(owned(name)) for name in ("halt", "connected", "socket", "heartbeat")}
    await asyncio.sleep(0)
    client = SimpleNamespace(disconnect=lambda: barrier("disconnect"),
        web=SimpleNamespace(close=lambda: barrier("http"),
            signer=SimpleNamespace(sdk_client=SimpleNamespace(_async_client=SimpleNamespace(aclose=lambda: barrier("signer"))))),
        _ws=SimpleNamespace(_ping_loop=tasks["heartbeat"]))
    foreign = asyncio.create_task(asyncio.Event().wait())
    async def owner():
        await finish_owned(asyncio.create_task(listener._cleanup(client, tasks["socket"], tasks["halt"], tasks["connected"], None, None)))
    parent = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        parent.cancel(); await asyncio.sleep(0); parent.cancel(); await asyncio.sleep(0)
        assert not parent.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await parent
        assert set(closed) == {"halt", "connected", "disconnect", "socket", "heartbeat", "http", "signer"}
        assert listener.status.cleanup_state == "complete" and all(t.done() for t in tasks.values())
        assert not foreign.done()
    finally:
        release.set(); foreign.cancel(); await asyncio.gather(foreign, return_exceptions=True)


async def test_cleanup_timeout_attempts_both_pools_and_reports_operator(settings):
    listener = LiveListener(settings.model_copy(update={"shutdown_timeout_seconds": 0.01}), asyncio.Queue())
    web, signer = AsyncMock(), AsyncMock()
    async def hangs(): await asyncio.Event().wait()
    client = SimpleNamespace(disconnect=hangs, web=SimpleNamespace(close=web,
        signer=SimpleNamespace(sdk_client=SimpleNamespace(_async_client=SimpleNamespace(aclose=signer)))))
    await asyncio.wait_for(listener._cleanup(client, None, None, None, None, None), 1)
    web.assert_awaited_once(); signer.assert_awaited_once()
    assert listener.status.cleanup_state == "failed"
    assert listener.status.terminal_state == LiveState.OPERATOR_REQUIRED


@pytest.mark.parametrize("phase", ["disconnect", "http", "signer"])
async def test_natural_end_cancel_during_real_session_cleanup(settings, phase):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    listener = LiveListener(settings, asyncio.Queue())
    async def close(name):
        if name == phase: entered.set(); await release.wait()
        calls.append(name)
    async def socket_loop():
        listener.on_connect(None); listener.on_end(None)
        await asyncio.Event().wait()
    socket, heartbeat = None, asyncio.create_task(asyncio.Event().wait())
    async def start():
        nonlocal socket
        socket = asyncio.create_task(socket_loop())
        return socket
    client = SimpleNamespace(add_listener=lambda *args: None, start=start,
        disconnect=lambda: close("disconnect"), _ws=SimpleNamespace(_ping_loop=heartbeat),
        web=SimpleNamespace(close=lambda: close("http"), signer=SimpleNamespace(sdk_client=SimpleNamespace(
            _async_client=SimpleNamespace(aclose=lambda: close("signer"))))))
    listener.client_factory = lambda **kwargs: client
    unrelated = asyncio.create_task(asyncio.Event().wait())
    task = asyncio.create_task(listener.run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert calls == ["disconnect", "http", "signer"]
        assert socket.done() and heartbeat.done() and not unrelated.done()
        assert listener.status.cleanup_state == "complete" and listener.status.terminal_state == LiveState.ENDED
    finally:
        release.set()
        if not task.done(): task.cancel()
        unrelated.cancel(); await asyncio.gather(task, unrelated, return_exceptions=True)


def test_sdk_comment_has_verified_message_identity():
    from app.live.listener import comment_from_event
    event = CommentEvent.from_dict({"common": {"msgId": "987654321"}, "user": {"displayId": "viewer"}, "content": "Hello!"})
    assert comment_from_event(event).message_id == "987654321"


@pytest.fixture
async def suggestion(settings):
    processor = create_processor(settings)
    comment = Comment(username="viewer", text="Is it automatic?", message_id="123")
    result = await processor.process(comment)
    assert result.status == "ready"
    store = SuggestionStore()
    e = store.register(record_for(comment, result, "tiktok", 1, uuid4().hex), comment, processor,
        uuid4().hex, session_id=uuid4().hex, target_account="own")
    context = DeliveryContext(live_session_id=e.live_session_id, target_account=e.target_account,
        active_runtime_revision=e.active_runtime_revision, knowledge_revision=e.knowledge_revision,
        policy_revision=e.policy_revision, connected=True, active=True, delivery_available=True, device_available=True)
    assert intact(e)
    approval = store.approve(e.suggestion_id, ApprovalInput(expected_suggestion_revision=e.suggestion_revision), context)
    yield SimpleNamespace(store=store, e=e, context=context, approval=approval, processor=processor)
    await processor.aclose()


def rehash(e, **changes):
    e = e.model_copy(update=changes)
    return e.model_copy(update={"suggestion_revision": digest(e.model_dump(mode="json", exclude={"suggestion_revision"}))})


@pytest.mark.parametrize("reason", ["eligible", "suggestion_not_ready", "source_not_live", "integrity_mismatch",
    "approval_missing", "approval_stale", "translation_review_missing", "live_session_mismatch",
    "target_account_mismatch", "runtime_revision_mismatch", "knowledge_revision_mismatch", "policy_revision_mismatch",
    "expired", "duplicate_operation", "live_not_connected", "live_inactive", "operator_paused", "delivery_unavailable", "device_unavailable"])
async def test_pure_eligibility_all_reasons(suggestion, reason):
    e, a, ctx = suggestion.e, suggestion.approval, suggestion.context
    duplicate = False
    mapping = {"live_session_mismatch": ("live_session_id", "other"), "target_account_mismatch": ("target_account", "other"),
        "runtime_revision_mismatch": ("active_runtime_revision", "other"), "knowledge_revision_mismatch": ("knowledge_revision", "other"),
        "policy_revision_mismatch": ("policy_revision", "other"), "live_not_connected": ("connected", False),
        "live_inactive": ("active", False), "operator_paused": ("operator_paused", True),
        "delivery_unavailable": ("delivery_available", False), "device_unavailable": ("device_available", False)}
    if reason in mapping:
        key, value = mapping[reason]; ctx = ctx.model_copy(update={key: value})
    elif reason == "suggestion_not_ready": e = rehash(e, status="skipped", should_reply=False)
    elif reason == "source_not_live": e = rehash(e, source="playground")
    elif reason == "integrity_mismatch": e = e.model_copy(update={"suggested_reply": "browser replacement"})
    elif reason == "approval_missing": a = None
    elif reason == "approval_stale": a = a.model_copy(update={"suggestion_revision": "0" * 64})
    elif reason == "translation_review_missing":
        e = rehash(e, translation_requires_review=True)
        a = a.model_copy(update={"suggestion_revision": e.suggestion_revision})
    elif reason == "expired": e = rehash(e, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    elif reason == "duplicate_operation": duplicate = True
    result = DeliveryEligibility.evaluate(e, a, ctx, duplicate=duplicate)
    assert result.reason == reason and result.eligible == (reason == "eligible")


async def test_approval_review_idempotency_and_immutable_text(suggestion):
    rig = suggestion; e = rig.e
    assert rig.store.approve(e.suggestion_id, ApprovalInput(expected_suggestion_revision=e.suggestion_revision), rig.context) == rig.approval
    with pytest.raises(ValidationError): e.suggested_reply = "replacement"
    with pytest.raises(ValidationError): ApprovalInput(expected_suggestion_revision=e.suggestion_revision, text="replacement")
    with pytest.raises(SuggestionConflict): rig.store.approve(e.suggestion_id, ApprovalInput(expected_suggestion_revision="0" * 64), rig.context)
    revised = rehash(e, translation_requires_review=True)
    rig.store.groups["live"][e.suggestion_id] = revised
    with pytest.raises(SuggestionConflict): rig.store.approve(e.suggestion_id, ApprovalInput(expected_suggestion_revision=revised.suggestion_revision), rig.context)
    approved = rig.store.approve(e.suggestion_id, ApprovalInput(expected_suggestion_revision=revised.suggestion_revision, translation_review_confirmed=True), rig.context)
    assert approved.translation_review_confirmed and approved.approval_id != rig.approval.approval_id


@pytest.mark.parametrize("status", ["skipped", "needs_operator"])
async def test_nonready_cannot_be_approved(suggestion, status):
    rig = suggestion
    revised = rehash(rig.e, status=status, should_reply=False)
    rig.store.groups["live"][rig.e.suggestion_id] = revised
    with pytest.raises(SuggestionConflict): rig.store.approve(revised.suggestion_id, ApprovalInput(expected_suggestion_revision=revised.suggestion_revision), rig.context)


@pytest.mark.parametrize("field", ["live_session_id", "active_runtime_revision", "knowledge_revision", "policy_revision", "connected", "active"])
async def test_approval_invalidates_for_server_changes(suggestion, field):
    rig = suggestion
    ctx = rig.context.model_copy(update={field: False if field in {"connected", "active"} else "new"})
    assert rig.store.view(rig.e.suggestion_id, ctx).approval_state == "stale"
    with pytest.raises(SuggestionConflict): rig.store.approve(rig.e.suggestion_id, ApprovalInput(expected_suggestion_revision=rig.e.suggestion_revision), ctx)


def coordinator(rig, journal, sender):
    return DeliveryCoordinator(rig.store, journal, sender, lambda: rig.context)


async def execute(rig, service, key="doubleclick"):
    return await service.execute(rig.e.suggestion_id, rig.approval.approval_id, rig.e.suggestion_revision, key, timeout=0.03)


async def test_two_tabs_restart_and_different_keys_deduplicate(suggestion, tmp_path):
    rig = suggestion; path = tmp_path / "delivery.sqlite3"
    one, two = SQLiteDeliveryOperationStore(path), SQLiteDeliveryOperationStore(path)
    sender = SimpleNamespace(send=AsyncMock(return_value=DeliveryResult(status="confirmed", reason="mock_confirmed")))
    first, second = await asyncio.gather(execute(rig, coordinator(rig, one, sender)), execute(rig, coordinator(rig, two, sender)))
    assert first.status == "confirmed" and second.status in {"unknown", "confirmed"}
    assert sender.send.await_count == 1
    assert sender.send.call_args.args[0].text == rig.e.suggested_reply
    with pytest.raises(JournalConflict): await execute(rig, coordinator(rig, two, sender), "different-key")
    one.close(); two.close()
    restarted = SQLiteDeliveryOperationStore(path)
    assert (await execute(rig, coordinator(rig, restarted, sender))).status == "confirmed"
    assert sender.send.await_count == 1
    columns = {row[1] for row in restarted.connection.execute("PRAGMA table_info(operations)")}
    assert "text" not in columns and "original_text" not in columns
    restarted.close()


@pytest.mark.parametrize("outcome", ["confirmed", "not_sent", "unknown", "needs_operator"])
async def test_terminal_outcome_never_implicitly_retries(suggestion, tmp_path, outcome):
    journal = SQLiteDeliveryOperationStore(tmp_path / "delivery.sqlite3")
    result = DeliveryResult(status=outcome, reason="mock_confirmed" if outcome == "confirmed" else "rejected")
    sender = SimpleNamespace(send=AsyncMock(return_value=result))
    service = coordinator(suggestion, journal, sender)
    assert await execute(suggestion, service) == result
    assert await execute(suggestion, service) == result
    assert sender.send.await_count == 1
    journal.close()


async def test_uncertain_timeout_is_persistent_unknown_without_retry(suggestion, tmp_path):
    async def late_confirmation(request):
        try: await asyncio.sleep(10)
        except asyncio.CancelledError: return DeliveryResult(status="confirmed", reason="mock_confirmed")
    path = tmp_path / "delivery.sqlite3"
    journal = SQLiteDeliveryOperationStore(path)
    sender = SimpleNamespace(send=AsyncMock(side_effect=late_confirmation))
    result = await execute(suggestion, coordinator(suggestion, journal, sender))
    assert result.status == "unknown" and result.reason == "timeout"
    journal.close(); journal = SQLiteDeliveryOperationStore(path)
    assert await execute(suggestion, coordinator(suggestion, journal, sender)) == result
    assert sender.send.await_count == 1
    journal.close()


async def test_crash_reservation_blocks_restart(suggestion, tmp_path):
    rig = suggestion; path = tmp_path / "delivery.sqlite3"
    journal = SQLiteDeliveryOperationStore(path)
    e = rig.e
    request = DeliveryRequest(operation_id=uuid4().hex, idempotency_key="doubleclick", suggestion_id=e.suggestion_id,
        suggestion_revision=e.suggestion_revision, comment_identity=e.comment_identity, approval_id=rig.approval.approval_id,
        suggested_reply_hash=e.suggested_reply_hash, text=e.suggested_reply, live_session_id=e.live_session_id,
        target_account=e.target_account, active_runtime_revision=e.active_runtime_revision, expires_at=e.expires_at)
    journal.reserve(request); journal.close()
    journal = SQLiteDeliveryOperationStore(path)
    sender = SimpleNamespace(send=AsyncMock())
    assert (await execute(rig, coordinator(rig, journal, sender))).status == "unknown"
    with pytest.raises(JournalConflict): await execute(rig, coordinator(rig, journal, sender), "new-key")
    sender.send.assert_not_awaited(); journal.close()


@pytest.fixture
async def panel(settings, tmp_path, monkeypatch):
    path = tmp_path / "project.yaml"; path.write_bytes(settings.project_config_path.read_bytes())
    monkeypatch.setattr("app.main.LiveService", lambda *a, **k: pytest.fail("Unexpected LIVE construction"))
    app = create_app(settings.model_copy(update={"project_config_path": path, "log_dir": tmp_path / "logs"}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            client.headers["X-Admin-CSRF"] = (await client.get("/api/admin/session")).json()["csrf_token"]
            yield app, client


async def test_settings_optimistic_versions_and_pending_approval(panel):
    app, client = panel; runtime = app.state.operator_runtime
    record = (await client.post("/api/admin/playground/analyze", json={"text": "Is it automatic?"})).json()
    e = record["delivery_review"]["envelope"]
    route = f"/api/admin/suggestions/{record['id']}/approve"
    body = {"expected_suggestion_revision": e["suggestion_revision"]}
    assert (await client.post(route, json=body)).status_code == 200
    original = (await client.get("/api/admin/operator/settings")).json()
    save = {"expected_saved_revision": original["saved_config_revision"], "settings": {"ai": {"temperature": 0.2}}}
    result = await client.post("/api/admin/operator/settings", json=save)
    assert result.status_code == 200
    saved = result.json()
    assert saved["active_runtime_revision"] == original["active_runtime_revision"]
    assert saved["saved_config_revision"] != original["saved_config_revision"]
    assert (await client.get(f"/api/admin/suggestions/{record['id']}")).json()["approval_state"] == "approved"
    assert (await client.post("/api/admin/operator/settings", json=save)).status_code == 409
    assert (await client.post("/api/admin/operator/reset", json={"expected_saved_revision": original["saved_config_revision"]})).status_code == 409
    assert (await client.post("/api/admin/operator/apply", json={"expected_saved_revision": original["saved_config_revision"], "expected_active_runtime_revision": original["active_runtime_revision"]})).status_code == 409
    apply = {"expected_saved_revision": saved["saved_config_revision"], "expected_active_runtime_revision": saved["active_runtime_revision"]}
    assert (await client.post("/api/admin/operator/apply", json=apply)).status_code == 200
    assert (await client.post("/api/admin/operator/apply", json=apply)).status_code == 409
    assert (await client.get(f"/api/admin/suggestions/{record['id']}")).json()["approval_state"] == "stale"
    assert (await client.post(route, json=body)).status_code == 409


@pytest.mark.parametrize("route", ["operator/settings", "operator/apply", "operator/reset", "live/disconnect"])
async def test_browser_action_requires_identity_precondition(panel, route):
    app, client = panel
    assert (await client.post("/api/admin/" + route, json={})).status_code == 422


async def test_approval_api_csrf_text_rejection_and_two_tabs(panel):
    app, client = panel
    record = (await client.post("/api/admin/playground/analyze", json={"text": "Is it automatic?"})).json()
    e = record["delivery_review"]["envelope"]; route = f"/api/admin/suggestions/{record['id']}/approve"
    body = {"expected_suggestion_revision": e["suggestion_revision"]}
    assert (await client.post(route, json=body, headers={"X-Admin-CSRF": "wrong"})).status_code == 403
    assert (await client.post(route, json=body | {"text": "replacement"})).status_code == 422
    assert (await client.post(route, json=body | {"expected_suggestion_revision": "0" * 64})).status_code == 409
    results = await asyncio.gather(client.post(route, json=body), client.post(route, json=body))
    assert all(r.status_code == 200 for r in results)
    assert results[0].json()["approval"]["approval_id"] == results[1].json()["approval"]["approval_id"]
    assert results[0].json()["delivery"] == "NOT CONFIGURED"
    for action in ["send", "deliver", "automatic"]:
        assert (await client.post(f"/api/admin/suggestions/{record['id']}/{action}", json=body)).status_code == 404


@pytest.mark.parametrize("phase", ["quality", "processor"])
async def test_apply_repeated_cancellation_keeps_new_processor_working(panel, phase):
    app, _ = panel; runtime = app.state.operator_runtime; pg = app.state.admin.playground
    old_processor, old_quality = pg.processor, app.state.quality
    old_epoch, old_revision = old_quality.run.service_epoch, runtime.active_runtime_revision
    entered, release = asyncio.Event(), asyncio.Event()
    real_close = old_processor.aclose if phase == "processor" else old_quality.aclose
    async def slow_close():
        entered.set(); await release.wait(); await real_close()
    if phase == "processor": old_processor.aclose = slow_close
    else: old_quality.aclose = slow_close
    task = asyncio.create_task(runtime.apply(runtime.saved_config_revision, runtime.active_runtime_revision))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
    assert not task.done() and pg.processor is not old_processor
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert not pg.lock.locked() and not runtime.applying
    assert runtime.active_runtime_revision != old_revision and app.state.quality.run.service_epoch != old_epoch
    assert old_quality.closed
    assert (await pg.analyze("Is it automatic?")).suggestion.status == "ready"


async def test_apply_build_failure_keeps_old_processor_working(panel, monkeypatch):
    app, _ = panel; runtime = app.state.operator_runtime
    processor, quality, revision = app.state.admin.playground.processor, app.state.quality, runtime.active_runtime_revision
    monkeypatch.setattr("app.admin.runtime.QualityService", lambda *a, **k: (_ for _ in ()).throw(ValueError("test")))
    with pytest.raises(ValueError): await runtime.apply(runtime.saved_config_revision, runtime.active_runtime_revision)
    assert app.state.admin.playground.processor is processor and app.state.quality is quality
    assert runtime.active_runtime_revision == revision and not quality.closed
    assert (await app.state.admin.playground.analyze("Is it automatic?")).suggestion.status == "ready"


class MockLive:
    def __init__(self, settings, on_comment, on_status):
        self.queue = asyncio.Queue(); self.on_comment, self.on_status = on_comment, on_status
        self.status = LiveStatus(username=settings.tiktok_username, mode="real")
        self.started = asyncio.Event()
    async def run(self):
        self.on_status(self.status.model_copy(update={"state": LiveState.CONNECTED}))
        self.started.set(); await asyncio.Event().wait()


async def test_session_identity_reconnect_and_stale_disconnect(panel):
    app, client = panel; runtime = app.state.operator_runtime
    runtime.live.factory = MockLive
    first = await client.post("/api/admin/live/connect", json={"username": "own"})
    session = first.json()["live_session_id"]; await runtime.live.service.started.wait()
    record = await runtime.live.service.on_comment(Comment(username="viewer", text="Is it automatic?", message_id="321"))
    body = {"expected_suggestion_revision": record.delivery_review.envelope.suggestion_revision}
    route = f"/api/admin/suggestions/{record.id}/approve"
    assert (await client.post(route, json=body)).status_code == 200
    runtime.admin.on_live_state(runtime.admin.live_status.model_copy(update={"state": LiveState.RECONNECTING}))
    assert runtime.admin.live_view().live_session_id == session
    assert runtime.admin.suggestions.view(record.id, runtime.admin.delivery_context()).approval_state == "stale"
    runtime.admin.on_live_state(runtime.admin.live_status.model_copy(update={"state": LiveState.CONNECTED}))
    assert (await client.post("/api/admin/live/disconnect", json={"expected_live_session_id": session})).status_code == 200
    second = await client.post("/api/admin/live/connect", json={"username": "own"})
    new_session = second.json()["live_session_id"]; assert new_session != session
    await runtime.live.service.started.wait()
    assert (await client.post("/api/admin/live/disconnect", json={"expected_live_session_id": session})).status_code == 409
    assert runtime.live.session_id == new_session and not runtime.live.task.done()
    assert (await client.post(route, json=body)).status_code == 409
    assert (await client.get("/api/admin/live/history")).json()[0]["delivery_review"]["approval_state"] == "stale"


@pytest.mark.parametrize("terminal", [LiveState.ENDED, LiveState.OPERATOR_REQUIRED])
async def test_terminal_session_cannot_revive_approval(panel, terminal):
    app, client = panel; runtime = app.state.operator_runtime
    runtime.live.factory = MockLive
    await runtime.live.connect("own"); await runtime.live.service.started.wait()
    record = await runtime.live.service.on_comment(Comment(username="viewer", text="Is it automatic?"))
    route = f"/api/admin/suggestions/{record.id}/approve"
    body = {"expected_suggestion_revision": record.delivery_review.envelope.suggestion_revision}
    assert (await client.post(route, json=body)).status_code == 200
    app.state.admin.on_live_state(app.state.admin.live_status.model_copy(update={"state": terminal, "terminal_state": terminal}))
    # Even a stale SDK CONNECTED callback cannot resurrect a terminal session.
    app.state.admin.on_live_state(app.state.admin.live_status.model_copy(update={"state": LiveState.CONNECTED, "terminal_state": None}))
    assert app.state.admin.live_view().activity != "active"
    assert (await client.post(route, json=body)).status_code == 409


async def test_disconnect_repeated_cancel_finishes_owned_processor_close(panel):
    app, _ = panel; runtime = app.state.operator_runtime
    runtime.live.factory = MockLive
    await runtime.live.connect("own"); await runtime.live.service.started.wait()
    entered, release = asyncio.Event(), asyncio.Event()
    processor = runtime.live.processor; real_close = processor.aclose
    async def closing(): entered.set(); await release.wait(); await real_close()
    processor.aclose = closing
    task = asyncio.create_task(runtime.live.disconnect(runtime.live.session_id))
    await asyncio.wait_for(entered.wait(), 1)
    assert app.state.admin.live_view().activity != "active"
    task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert runtime.live.service is None and runtime.live.processor_closed and runtime.live.session_id is None
    assert (await app.state.admin.playground.analyze("Is it automatic?")).suggestion.status == "ready"


async def test_owned_callback_timeout_is_visible_and_blocks_new_connect(panel, settings):
    from app.live.service import LiveService
    from app.admin.runtime import RuntimeConflict
    app, _ = panel; runtime = app.state.operator_runtime
    entered, release = asyncio.Event(), asyncio.Event(); holder = {}
    async def callback(comment):
        holder["task"] = asyncio.current_task(); entered.set()
        try: await asyncio.Event().wait()
        except asyncio.CancelledError: await release.wait()
    def factory(config, on_comment, on_status):
        return LiveService(settings.model_copy(update={"fake_repeat": True, "shutdown_timeout_seconds": 0.01}),
                           on_comment=callback, on_status=on_status)
    runtime.live.factory = factory
    try:
        await runtime.live.connect("own"); await entered.wait()
        await asyncio.wait_for(runtime.live.disconnect(runtime.live.session_id), 1)
        assert app.state.admin.live_view().cleanup_state == "failed"
        with pytest.raises(RuntimeConflict): await runtime.live.connect("own")
    finally:
        release.set()
        if "task" in holder:
            holder["task"].cancel(); await asyncio.gather(holder["task"], return_exceptions=True)


async def test_cancel_apply_before_atomic_publication_leaves_old_runtime(panel):
    app, _ = panel; runtime = app.state.operator_runtime
    processor, quality, revision = app.state.admin.playground.processor, app.state.quality, runtime.active_runtime_revision
    async with runtime.lock:
        task = asyncio.create_task(runtime.apply(runtime.saved_config_revision, revision))
        await asyncio.sleep(0); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    assert app.state.admin.playground.processor is processor and app.state.quality is quality
    assert runtime.active_runtime_revision == revision and not quality.closed
    assert (await app.state.admin.playground.analyze("Is it automatic?")).suggestion.status == "ready"


@pytest.mark.parametrize("phase", ["quality", "processor"])
async def test_retirement_error_keeps_new_runtime_working(panel, phase):
    app, _ = panel; runtime = app.state.operator_runtime
    old = app.state.admin.playground.processor
    async def broken(): raise RuntimeError("private error")
    if phase == "quality": app.state.quality.aclose = broken
    else: old.aclose = broken
    with pytest.raises(RuntimeError): await runtime.apply(runtime.saved_config_revision, runtime.active_runtime_revision)
    assert app.state.admin.playground.processor is not old
    assert (await app.state.admin.playground.analyze("Is it automatic?")).suggestion.status == "ready"


async def test_quality_epoch_accepts_low_revision_and_clears_rows(panel):
    import json
    import shutil
    import subprocess
    from app.quality.service import QualityService
    node = shutil.which("node")
    if node is None: pytest.skip("Optional JS regression requires a local Node runtime")
    app, _ = panel
    old = app.state.quality.snapshot().model_dump(mode="json"); old["run"]["revision"] = 155
    new = QualityService(app.state.admin.playground).snapshot().model_dump(mode="json"); new["run"]["revision"] = 1
    lower = json.loads(json.dumps(new)); lower["run"]["revision"] = 0; lower["run"]["state"] = "failed"
    script = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const snapshots=JSON.parse(fs.readFileSync(0,'utf8')), elements=new Map();
const element=()=>({children:[],textContent:'',append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x},setAttribute(){},addEventListener(){}});
const document={getElementById(id){if(!elements.has(id)) elements.set(id,element());return elements.get(id)}};
const window={}; vm.runInNewContext(fs.readFileSync('backend/app/static/admin/quality.js','utf8'),{window,document});
const lab=window.createQualityLab({api:async()=>snapshots.shift(),node:(_,__,value)=>Object.assign(element(),{textContent:value}),replyCard:element,toast(){},state:{connected:true},updateControls(){}});
(async()=>{await lab.refresh();const before=elements.get('quality-cases').children[0];await lab.refresh();assert.notStrictEqual(before,elements.get('quality-cases').children[0]);const status=elements.get('quality-progress').textContent;await lab.refresh();assert.strictEqual(status,elements.get('quality-progress').textContent);})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", script], input=json.dumps([old, new, lower]), text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


async def test_quality_review_requires_explicit_trial(panel):
    app, client = panel
    # No inference is needed to prove the request contract rejects omission.
    response = await client.post("/api/admin/quality/review", json={"run_id": "0" * 32, "case_id": "example", "review": {}})
    assert response.status_code == 422


async def test_cancel_mock_operation_records_unknown(suggestion, tmp_path):
    journal = SQLiteDeliveryOperationStore(tmp_path / "delivery.sqlite3")
    entered = asyncio.Event()
    async def sending(request): entered.set(); await asyncio.Event().wait()
    sender = SimpleNamespace(send=AsyncMock(side_effect=sending))
    task = asyncio.create_task(execute(suggestion, coordinator(suggestion, journal, sender)))
    await entered.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    result = await execute(suggestion, coordinator(suggestion, journal, sender))
    assert result.status == "unknown" and result.reason == "cancelled" and sender.send.await_count == 1
    journal.close()


async def test_journal_capacity_and_conflicting_key_fail_closed(suggestion, tmp_path):
    rig = suggestion; journal = SQLiteDeliveryOperationStore(tmp_path / "delivery.sqlite3", capacity=1)
    sender = SimpleNamespace(send=AsyncMock(return_value=DeliveryResult(status="confirmed", reason="mock_confirmed")))
    service = coordinator(rig, journal, sender)
    await execute(rig, service)
    new = rehash(rig.e, suggestion_id=uuid4().hex, comment_identity="9" * 64)
    rig.store.groups["live"][new.suggestion_id] = new
    approval = rig.store.approve(new.suggestion_id, ApprovalInput(expected_suggestion_revision=new.suggestion_revision), rig.context)
    with pytest.raises(JournalConflict, match="binding conflict"):
        await service.execute(new.suggestion_id, approval.approval_id, new.suggestion_revision, "doubleclick")
    with pytest.raises(JournalConflict, match="journal full"):
        await service.execute(new.suggestion_id, approval.approval_id, new.suggestion_revision, "new-key")
    assert sender.send.await_count == 1
    journal.close()
