"""Operator actions with mocked LIVE/provider boundaries. Never real network."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
import yaml

from app.admin.operator_config import OperatorOverlay, OperatorStore, LocalConfigError, effective
from app.ai.factory import create_processor
from app.ai.knowledge import ProjectConfig, load_yaml_model
from app.live.models import LiveState, LiveStatus, Comment
from app.main import create_app

BASE = "/api/admin/operator/"


async def post_action(c, path, *, json):
    """Existing action tests now explicitly send the currently observed version."""
    if path.startswith(BASE):
        view = (await c.get(BASE + "settings")).json()
        if path.endswith("settings"):
            json = {"settings": json, "expected_saved_revision": view["saved_config_revision"]}
        elif path.endswith("apply"):
            json = {"expected_saved_revision": view["saved_config_revision"], "expected_active_runtime_revision": view["active_runtime_revision"]}
        elif path.endswith("reset"):
            json = {"expected_saved_revision": view["saved_config_revision"]}
    elif path.endswith("live/disconnect"):
        live = (await c.get("/api/admin/status")).json()["live"]
        # Repeated disconnect uses the same previously observed session ID.
        observed = live["live_session_id"] or getattr(c, "observed_session", None)
        if observed is not None: c.observed_session = observed
        json = {"expected_live_session_id": observed}
    return await c.post(path, json=json)


@pytest.fixture
def runtime_config(settings, tmp_path):
    path = tmp_path / "project.yaml"
    path.write_bytes(settings.project_config_path.read_bytes())
    return settings.model_copy(update={"project_config_path": path, "log_dir": tmp_path / "logs"})


@pytest.fixture
def store(runtime_config):
    return OperatorStore(load_yaml_model(runtime_config.project_config_path, ProjectConfig),
                         runtime_config.project_config_path.parent / "operator.local.yaml")


@pytest.fixture
async def client(runtime_config, monkeypatch):
    monkeypatch.setattr("app.main.LiveService", lambda *a, **k: pytest.fail("unexpected LIVE construction"))
    app = create_app(runtime_config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as c:
            token = (await c.get("/api/admin/session")).json()["csrf_token"]
            c.headers["X-Admin-CSRF"] = token
            yield app, c


def overlay(**changes):
    values = dict(tiktok={"username": " @own.account "},
        ai={"provider": "lmstudio", "model": "qwen/qwen3-8b", "temperature": 0.0, "timeout_seconds": 15},
        reply={"mode": "friendly", "max_sentences": 3, "max_chars": 260})
    values.update(changes)
    return OperatorOverlay.model_validate(values)


def test_absent_overlay_defaults(store):
    assert store.validate(store.load()) == store.base
    assert not store.path.exists()


def test_overlay_and_atomic_save_reset(store, monkeypatch):
    before = store.base.model_dump()
    saved = overlay()
    store.save(saved)
    assert store.load() == saved and store.load().tiktok.username == "own.account"
    project = store.validate(saved)
    assert project.ai.temperature == 0 and project.ai.reply_policy == "friendly"
    assert project.ai.provider == "lmstudio" and project.ai.model == "qwen/qwen3-8b"
    assert project.ai.lmstudio_base_url == store.base.ai.lmstudio_base_url
    assert store.base.model_dump() == before
    contents = store.path.read_bytes()
    def broken(*args):
        raise OSError("private exception body")
    monkeypatch.setattr("app.admin.operator_config.os.replace", broken)
    with pytest.raises(OSError): store.save(OperatorOverlay())
    assert store.path.read_bytes() == contents
    assert not list(store.path.parent.glob(".operator-*.tmp"))
    store.reset()
    assert not store.path.exists() and store.load() == OperatorOverlay()


@pytest.mark.parametrize("body", [
    {"password": "secret"}, {"tiktok": {"cookies": "secret"}}, {"tiktok": {"sessionid": "secret"}},
    {"ai": {"provider": "openai"}}, {"ai": {"api_key": "secret"}}, {"ai": {"lmstudio_base_url": "http://remote/v1"}},
    {"ai": {"temperature": -1}}, {"ai": {"temperature": 1.1}}, {"ai": {"temperature": True}},
    {"ai": {"timeout_seconds": 0}}, {"ai": {"timeout_seconds": 121}}, {"ai": {"model": "evil\nmodel"}},
    {"reply": {"disable_safety": True}}, {"reply": {"allow_profit_promises": True}},
    {"reply": {"allow_win_rate": True}}, {"reply": {"disable_grounding": True}},
    {"reply": {"mode": "unrestricted"}}, {"reply": {"max_sentences": 4}}, {"reply": {"max_chars": 401}},
    {"forbidden_claims": []}, {"knowledge": {}}, {"path": "any.yaml"}, {"captcha_policy": "bypass"},
    {"tiktok": {"username": "https://tiktok.com/@own"}}, {"tiktok": {"username": "x" * 25}},
])
async def test_unsafe_settings_rejected_without_echo(client, body):
    app, c = client
    r = await post_action(c, BASE + "settings", json=body)
    assert r.status_code == 422
    assert "secret" not in r.text and "remote" not in r.text
    assert not app.state.operator_runtime.store.path.exists()


@pytest.mark.parametrize("content", ["[broken", "[]", "null", "!!python/object/apply:os.system ['x']",
    "ai:\n  provider: fake\n  provider: lmstudio\n", "ai:\n  api_key: secret\n", "reply:\n  max_sentences: 3\n"])
def test_invalid_local_yaml_fails_closed(store, content):
    store.path.write_text(content, encoding="utf-8")
    with pytest.raises(LocalConfigError): store.load()


async def test_save_apply_reload_reset_and_no_implicit_requests(client, runtime_config):
    app, c = client
    initial = app.state.admin.playground.processor
    for path in ("/ui/", "/api/admin/status", BASE + "settings", "/api/admin/quality"):
        assert (await c.get(path)).status_code == 200
    assert app.state.live_service is None
    body = overlay().model_dump(mode="json")
    saved = (await post_action(c, BASE + "settings", json=body)).json()
    assert saved["pending"] and saved["active"]["provider"] == "fake"
    assert app.state.admin.playground.processor is initial
    applied = await post_action(c, BASE + "apply", json={})
    assert applied.status_code == 200 and not applied.json()["pending"]
    new = app.state.admin.playground.processor
    assert new is not initial and new.engine._client is None
    assert new.project.ai.temperature == 0 and new.project.ai.max_reply_sentences == 3
    assert not app.state.quality.run.enabled and "FOCUSED" in app.state.quality.run.disabled_reason
    assert (await c.post("/api/admin/quality/start", json={"runs": 3})).status_code == 403
    assert new.engine._client is None
    restarted = create_app(runtime_config)
    async with restarted.router.lifespan_context(restarted):
        assert restarted.state.admin.playground.processor.project == new.project
        assert restarted.state.operator_runtime.username == "own.account"
        assert restarted.state.live_service is None
    assert (await post_action(c, BASE + "reset", json={})).json()["pending"]
    assert (await post_action(c, BASE + "apply", json={})).status_code == 200
    assert app.state.admin.playground.processor.project.ai.reply_policy == "focused"
    assert not app.state.operator_runtime.store.path.exists()
    assert app.state.quality.run.enabled


async def test_invalid_file_visible_and_recoverable(runtime_config, store):
    store.path.write_text("ai: {api_key: PRIVATE}", encoding="utf-8")
    app = create_app(runtime_config)
    async with app.router.lifespan_context(app):
        runtime = app.state.operator_runtime
        assert runtime.view().config_error == "invalid_local_config"
        assert "PRIVATE" not in runtime.view().model_dump_json()
        assert app.state.live_service is None
        await runtime.reset(runtime.saved_config_revision)
        assert runtime.view().config_error is None


class MockLive:
    instances = None

    def __init__(self, settings, on_comment, on_status):
        self.settings, self.on_comment, self.on_status = settings, on_comment, on_status
        self.queue = asyncio.Queue()
        self.status = LiveStatus(username=settings.tiktok_username, mode=settings.live_mode)
        self.started, self.released = asyncio.Event(), asyncio.Event()
        self.security = False
        self.instances.append(self)

    def state(self, state, terminal=None):
        self.status = self.status.model_copy(update={"state": state, "terminal_state": terminal})
        self.on_status(self.status)

    async def run(self):
        try:
            self.state(LiveState.CONNECTED)
            self.started.set()
            if self.security:
                self.state(LiveState.OPERATOR_REQUIRED)
                self.state(LiveState.STOPPED, LiveState.OPERATOR_REQUIRED)
            else:
                await asyncio.Event().wait()
        finally:
            self.released.set()


@pytest.fixture
def live_mock(client):
    app, c = client
    created = []
    MockLive.instances = created
    app.state.operator_runtime.live.factory = MockLive
    return app, c, created


async def test_connect_disconnect_single_listener_and_playground_survives(live_mock):
    app, c, created = live_mock
    pg = app.state.admin.playground.processor
    unrelated = asyncio.create_task(asyncio.Event().wait())
    try:
        response = await c.post("/api/admin/live/connect", json={"username": " @own.account "})
        assert response.status_code == 202
        await created[0].started.wait()
        assert created[0].settings.live_mode == "real" and created[0].settings.tiktok_username == "own.account"
        assert (await c.post("/api/admin/live/connect", json={"username": "second"})).status_code == 409
        assert len(created) == 1 and app.state.comment_processor is not pg
        assert not app.state.quality.run.enabled
        assert (await post_action(c, BASE + "settings", json={"tiktok": {"username": "other"}})).status_code == 409
        assert (await post_action(c, BASE + "apply", json={})).status_code == 409
        assert (await post_action(c, BASE + "reset", json={})).status_code == 409
        await created[0].on_comment(Comment(username="viewer", text="Hello!"))
        assert app.state.admin.live_history[0].source == "tiktok"
        assert (await post_action(c, "/api/admin/live/disconnect", json={})).status_code == 200
        assert created[0].released.is_set() and not unrelated.done()
        assert app.state.live_service is app.state.live_task is None
        assert app.state.admin.playground.processor is pg
        assert (await c.get("/health")).json()["status"] == "ok"
        result = await c.post("/api/admin/playground/analyze", json={"text": "How does the bot work?"})
        assert result.status_code == 200 and result.json()["suggestion"]["status"] == "ready"
        assert app.state.quality.run.enabled
        assert (await post_action(c, "/api/admin/live/disconnect", json={})).status_code == 200
        assert (await c.post("/api/admin/live/connect", json={"username": "other"})).status_code == 202
        assert len(created) == 2
    finally:
        unrelated.cancel(); await asyncio.gather(unrelated, return_exceptions=True)


async def test_security_terminal_requires_explicit_disconnect(live_mock):
    app, c, created = live_mock
    factory = app.state.operator_runtime.live.factory
    def security(*a, **k):
        service = factory(*a, **k)
        service.security = True
        return service
    app.state.operator_runtime.live.factory = security
    assert (await c.post("/api/admin/live/connect", json={"username": "own"})).status_code == 202
    await app.state.live_task
    view = (await c.get("/api/admin/status")).json()["live"]
    assert view["terminal_state"] == "operator_required"
    assert (await c.post("/api/admin/live/connect", json={"username": "own"})).status_code == 409
    assert len(created) == 1
    assert (await post_action(c, "/api/admin/live/disconnect", json={})).status_code == 200


async def test_cancel_before_runner_entry_closes_processor(live_mock):
    app, c, _ = live_mock
    controller = app.state.operator_runtime.live
    await controller.connect("own")
    processor = controller.processor
    processor.aclose = AsyncMock()
    await controller.disconnect(controller.session_id)
    processor.aclose.assert_awaited_once()


@pytest.mark.parametrize("action", ["settings", "apply", "reset"])
async def test_runtime_update_blocked_during_inference(client, action):
    app, c = client
    await app.state.admin.playground.lock.acquire()
    try:
        assert (await post_action(c, BASE + action, json={})).status_code == 409
        assert (await c.post("/api/admin/live/connect", json={"username": "own"})).status_code == 409
    finally:
        app.state.admin.playground.lock.release()


@pytest.mark.parametrize("username", ["", "@", "https://tiktok.com/@own", "name with space", "x" * 25])
async def test_bad_username_no_listener(client, username):
    _, c = client
    assert (await c.post("/api/admin/live/connect", json={"username": username})).status_code == 422


@pytest.mark.parametrize("route", [BASE + "settings", BASE + "apply", BASE + "reset",
    "/api/admin/live/connect", "/api/admin/live/disconnect"])
async def test_new_mutations_keep_csrf_origin_guards(client, route):
    _, c = client
    assert (await c.post(route, json={}, headers={"X-Admin-CSRF": "wrong"})).status_code == 403
    assert (await c.post(route, json={}, headers={"Origin": "http://evil.invalid"})).status_code == 403


async def test_no_send_or_automatic_routes(client):
    app, c = client
    for path in ("/api/admin/send", "/api/admin/live/send", "/api/admin/automatic"):
        assert (await c.post(path, json={})).status_code == 404
    status = (await c.get("/api/admin/status")).json()
    assert status["reply_mode"] == "MANUAL" and not status["automatic_enabled"]


async def test_ui_connect_forbids_openai_even_with_configured_key(runtime_config):
    data = yaml.safe_load(runtime_config.project_config_path.read_text(encoding="utf-8"))
    data["ai"]["provider"] = "openai"
    runtime_config.project_config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    app = create_app(runtime_config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            c.headers["X-Admin-CSRF"] = (await c.get("/api/admin/session")).json()["csrf_token"]
            assert (await c.post("/api/admin/live/connect", json={"username": "own"})).status_code == 409
            assert (await c.post("/api/admin/playground/analyze", json={"text": "Hello!"})).status_code == 403
            assert app.state.admin.playground.processor.engine._client is None
            assert app.state.live_service is None


async def test_inference_publishes_lock_and_unlock_to_other_tabs(client):
    app, c = client
    pg = app.state.admin.playground
    started, finish = asyncio.Event(), asyncio.Event()
    original = pg.processor.process
    async def process(comment):
        started.set()
        await finish.wait()
        return await original(comment)
    pg.processor.process = process
    subscriber = app.state.admin.events.subscribe()
    task = asyncio.create_task(pg.analyze("Hello!"))
    await started.wait()
    try:
        assert app.state.operator_runtime.view().locked
        assert (await post_action(c, BASE + "apply", json={})).status_code == 409
        finish.set(); await task
        assert not app.state.operator_runtime.view().locked
        import json
        locks = []
        while not subscriber.queue.empty():
            event = subscriber.queue.get_nowait()
            if event.name == "operator_settings": locks.append(json.loads(event.data)["locked"])
        assert locks == [True, False]
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        app.state.admin.events.unsubscribe(subscriber)
