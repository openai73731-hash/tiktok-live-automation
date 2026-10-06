"""Local UI contract, safe input, bounded realtime and production AI ownership.

All provider traffic is MockTransport; no TikTok, paid API or local model server.
"""
import asyncio
from datetime import UTC, datetime
from html.parser import HTMLParser
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import httpx2
import openai
import pytest
from pydantic import SecretStr
import yaml
from fastapi import HTTPException

from app.admin.events import AdminEventBus
from app.admin.models import CommentView
from app.admin.router import events as event_route
from app.admin.service import PRESETS, PlaygroundBusy
from app.live.models import Comment, LiveState, LiveStatus
from app.main import create_app

PRIVATE = "PRIVATE_BODY_KEY_COOKIE_URL_NEVER_RETURN"
ANALYZE = "/api/admin/playground/analyze"
NOW = datetime.now(UTC)


@pytest.fixture
def config(settings, tmp_path):
    return settings.model_copy(update={"log_dir": tmp_path / "logs", "openai_api_key": SecretStr(PRIVATE)})


@pytest.fixture
async def admin_client(config):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:8000") as client:
            token = (await client.get("/api/admin/session")).json()["csrf_token"]
            client.headers["X-Admin-CSRF"] = token
            yield app, client


def provider_config(config, tmp_path, provider):
    data = yaml.safe_load(config.project_config_path.read_text(encoding="utf-8"))
    data["ai"]["provider"] = provider
    if provider == "lmstudio":
        data["ai"]["model"] = "fixture/model"
    path = tmp_path / (provider + ".yaml")
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return config.model_copy(update={"project_config_path": path})


def selection():
    return dict(detected_language="en", operator_translation="Бот торгует автоматически?",
                should_reply=True, intent="facts", fact_ids=["automatic_analysis", "automatic_trades"],
                reason="approved_facts")


def openai_body():
    return dict(id="resp_fixture", object="response", created_at=0, status="completed", model="fixture",
                output=[dict(id="msg_fixture", type="message", role="assistant", status="completed",
                content=[dict(type="output_text", text=json.dumps(selection()), annotations=[])])])


async def test_default_web_start_does_not_construct_listener_or_ai_client(config, monkeypatch):
    monkeypatch.setattr("app.main.LiveService", lambda *a, **k: pytest.fail("implicit LIVE"))
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda *a, **k: pytest.fail("implicit paid client"))
    app = create_app(config.model_copy(update={"live_mode": "real", "tiktok_username": "own"}))
    async with app.router.lifespan_context(app):
        assert app.state.live_service is app.state.live_task is None
        assert app.state.admin.live_view().activity == "unknown"
        assert app.state.admin.playground.processed == 0
        assert app.state.admin.playground.processor.engine.name == "fake"
    assert app.state.admin.events.closed


async def test_dashboard_settings_health_and_history_do_not_expose_secrets(admin_client):
    app, client = admin_client
    for url in ("/health", "/api/admin/status", "/api/admin/settings", "/api/admin/ai/status",
                "/api/admin/live/history", "/api/admin/playground/history"):
        response = await client.get(url)
        assert response.status_code == 200
        assert PRIVATE not in response.text
        assert "openai_api_key" not in response.text and "log_dir" not in response.text
        assert response.headers["cache-control"] == "no-store"
    value = (await client.get("/api/admin/status")).json()
    assert value["reply_mode"] == "MANUAL" and value["automatic_enabled"] is False
    assert value["delivery"] == "NOT CONFIGURED"
    assert value["live"]["state"] == "disconnected" and value["live"]["activity"] == "unknown"
    assert value["metrics"] == dict(processed=0, ready=0, skipped=0, needs_operator=0)
    assert app.state.admin.playground.processed == 0


@pytest.mark.parametrize("url,kind", [("/ui/", "text/html"),
    ("/ui/assets/styles.css", "text/css"), ("/ui/assets/app.js", "javascript")])
async def test_assets_same_backend_and_safe_headers(admin_client, url, kind):
    app, client = admin_client
    response = await client.get(url)
    assert response.status_code == 200 and kind in response.headers["content-type"]
    assert PRIVATE not in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (await client.get("/", follow_redirects=False)).headers["location"] == "/ui/"
    assert (await client.get("/.env")).status_code == 404
    assert (await client.get("/ui/assets/../../.env")).status_code == 404
    assert (await client.get("/docs")).status_code == 404


async def test_ui_loads_assets_only_from_same_server(admin_client):
    _, client = admin_client
    resources = []

    class Resources(HTMLParser):
        def handle_starttag(self, tag, attrs):
            for key, value in attrs:
                if key == "src" or (tag == "link" and key == "href"):
                    resources.append(value)

    Resources().feed((await client.get("/ui/")).text)
    assert resources
    for path in resources:
        assert path.startswith("/ui/assets/") and not path.startswith("//")
        assert (await client.get(path)).status_code == 200


@pytest.mark.parametrize("text,status", [("How does the bot work?", "ready"),
    ("Hello!", "skipped"), ("Unsupported arbitrary input", "needs_operator")])
async def test_playground_uses_real_fake_processor_and_stays_out_of_live(admin_client, text, status):
    app, client = admin_client
    before = app.state.admin.playground.processor
    response = await client.post(ANALYZE, json={"text": text})
    assert response.status_code == 200
    value = response.json()
    assert value["source"] == "playground" and value["duration_ms"] >= 0
    suggestion = value["suggestion"]
    assert suggestion["original_text"] == text and suggestion["username"] == "playground"
    assert suggestion["status"] == status and suggestion["provider"] == "fake"
    assert suggestion["requires_approval"] is True and suggestion["auto_send_eligible"] is False
    assert app.state.admin.playground.processor is before
    assert len((await client.get("/api/admin/playground/history")).json()) == 1
    assert (await client.get("/api/admin/live/history")).json() == []
    assert (await client.get("/api/admin/status")).json()["metrics"]["processed"] == 0
    if status == "ready":
        assert suggestion["fact_ids"] == ["automatic_analysis", "automatic_trades"]
        assert "автоматически" in suggestion["reply_translation"]


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": " \n "}, {"text": 3},
    {"text": "x" * 1001}, {"text": "x" * 10001}, {"text": "x", "url": PRIVATE},
    {"text": "x", "model": PRIVATE}, {"text": "x", "allow_paid": "true"}])
async def test_bad_input_and_browser_provider_overrides_rejected_without_echo(admin_client, body):
    app, client = admin_client
    assert (await client.post(ANALYZE, json=body)).status_code == 422
    assert PRIVATE not in (await client.post(ANALYZE, json=body)).text
    assert app.state.admin.playground.processed == 0


async def test_request_body_limit_before_provider(admin_client):
    app, client = admin_client
    response = await client.post(ANALYZE, json={"text": "x" * 70000})
    assert response.status_code == 413
    assert len(response.content) < 200
    assert app.state.admin.playground.processed == 0


@pytest.mark.parametrize("host", ["attacker.invalid", "192.168.1.1:8000", "127.0.0.1.attacker.invalid",
    "127.0.0.1:0", "user@127.0.0.1", "127.0.0.1:65536", "[::1", "127.0.0.1/path"])
async def test_host_guard(admin_client, host):
    _, client = admin_client
    assert (await client.get("/api/admin/status", headers={"Host": host})).status_code == 403


@pytest.mark.parametrize("origin", ["https://attacker.invalid", "null", "http://127.0.0.1:8001",
    "http://localhost:8000", "https://127.0.0.1:8000", "http://[::1", "http://127.0.0.1:8000/path"])
async def test_foreign_or_malformed_origin_rejected(admin_client, origin):
    app, client = admin_client
    response = await client.post(ANALYZE, json={"text": "How does the bot work?"}, headers={"Origin": origin})
    assert response.status_code == 403
    assert app.state.admin.playground.processed == 0


async def test_same_origin_accepted_and_remote_peer_rejected(admin_client):
    app, client = admin_client
    assert (await client.post(ANALYZE, json={"text": "Hello!"},
                             headers={"Origin": "http://127.0.0.1:8000"})).status_code == 200
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.168.1.5", 123)),
                                base_url="http://127.0.0.1:8000") as remote:
        assert (await remote.get("/ui/")).status_code == 403


@pytest.mark.parametrize("token", ["", "wrong", "\u00e9"])
async def test_csrf_guard_including_non_ascii(admin_client, token):
    app, client = admin_client
    response = await client.post(ANALYZE, json={"text": "Hello!"},
                                 headers=[(b"x-admin-csrf", token.encode("latin-1"))])
    assert response.status_code == 403
    assert app.state.admin.playground.processed == 0


async def test_form_and_duplicate_header_requests_rejected(admin_client):
    _, client = admin_client
    assert (await client.post(ANALYZE, content="text=Hello", headers={"Content-Type": "text/plain"})).status_code == 415
    assert (await client.get("/health", headers=[("Host", "127.0.0.1"), ("Host", "localhost")])).status_code == 403


async def test_presets_readonly_and_delivery_controls_absent(admin_client):
    app, client = admin_client
    assert len((await client.get("/api/admin/playground/presets")).json()) == len(PRESETS) == 9
    assert app.state.admin.playground.processed == 0
    for route in ("/api/admin/send", "/api/admin/automatic", "/api/admin/settings",
                  "/api/admin/live/start", "/api/admin/live/restart"):
        assert (await client.post(route, json={})).status_code in {404, 405}


async def test_lmstudio_status_and_generation_reuse_one_client_without_live(config, tmp_path, monkeypatch):
    configured = provider_config(config, tmp_path, "lmstudio")
    calls = []
    state = {"models": {"data": [{"id": "fixture/model"}]}}

    async def respond(request):
        calls.append(request)
        assert "authorization" not in request.headers
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=state["models"])
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(selection())}}]})

    monkeypatch.setattr("app.main.LiveService", lambda *a, **k: pytest.fail("LIVE in playground"))
    app = create_app(configured)
    async with app.router.lifespan_context(app):
        engine = app.state.admin.playground.processor.engine
        engine._transport = httpx.MockTransport(respond)
        assert engine._client is None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            client.headers["X-Admin-CSRF"] = app.state.admin.csrf_token
            assert (await client.get("/api/admin/ai/status")).json()["state"] == "not_checked"
            assert calls == []
            for _ in range(2):
                value = (await client.post("/api/admin/ai/check", json={})).json()
                assert value["available"] is True and value["model_found"] is True
            assert len(calls) == 1 and calls[0].method == "GET"
            owner = engine._client
            for _ in range(2):
                result = (await client.post(ANALYZE, json={"text": "Is bot trading automatically?"})).json()
                assert result["suggestion"]["status"] == "ready"
            assert engine._client is owner
            assert [r.method for r in calls] == ["GET", "POST", "POST"]
            assert (await client.get("/api/admin/live/history")).json() == []
            state["models"] = {"data": [{"id": "other/model", "private": PRIVATE}]}
            app.state.admin.last_checked = 0
            value = (await client.post("/api/admin/ai/check", json={})).json()
            assert value["available"] is True and value["model_found"] is False
            assert value["error_category"] == "model_not_found" and PRIVATE not in json.dumps(value)
    assert owner.is_closed


async def test_openai_explicit_confirmation_only_and_no_generation_for_status(config, tmp_path, monkeypatch):
    configured = provider_config(config, tmp_path, "openai")
    calls, clients = [], []
    original = openai.DefaultAsyncHttpxClient

    def respond(request):
        calls.append(request)
        return httpx2.Response(200, json=openai_body())

    def mocked_client(**kwargs):
        client = original(transport=httpx2.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", mocked_client)
    app = create_app(configured)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            client.headers["X-Admin-CSRF"] = app.state.admin.csrf_token
            for url in ("/ui/", "/health", "/api/admin/status", "/api/admin/settings", "/api/admin/ai/status"):
                assert (await client.get(url)).status_code == 200
            assert (await client.post("/api/admin/ai/check", json={})).json()["state"] == "manual_check_required"
            assert (await client.post(ANALYZE, json={"text": "Is bot trading automatically?"})).status_code == 403
            assert calls == clients == []
            for _ in range(2):
                result = await client.post(ANALYZE, json={"text": "Is bot trading automatically?", "allow_paid": True})
                assert result.json()["suggestion"]["status"] == "ready"
            assert len(calls) == 2 and len(clients) == 1
    assert clients[0].is_closed


async def test_provider_exception_body_never_reaches_browser(config, tmp_path):
    app = create_app(provider_config(config, tmp_path, "lmstudio"))
    broken = True

    def respond(request):
        if broken:
            return httpx.Response(500, text=PRIVATE)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture/model"}]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(selection())}}]})

    async with app.router.lifespan_context(app):
        engine = app.state.admin.playground.processor.engine
        engine._transport = httpx.MockTransport(respond)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            client.headers["X-Admin-CSRF"] = app.state.admin.csrf_token
            value = await client.post("/api/admin/ai/check", json={})
            assert value.json()["state"] == "unavailable" and PRIVATE not in value.text
            value = await client.post(ANALYZE, json={"text": "Question?"})
            assert value.json()["suggestion"]["status"] == "needs_operator" and PRIVATE not in value.text
            owner = engine._client
            broken = False
            value = await client.post(ANALYZE, json={"text": "Is bot trading automatically?"})
            assert value.json()["suggestion"]["status"] == "ready"
            assert engine._client is owner


async def test_internal_failure_response_redacts_body_and_has_security_headers(admin_client, monkeypatch):
    app, _ = admin_client
    monkeypatch.setattr(app.state.admin, "public_settings", lambda: (_ for _ in ()).throw(RuntimeError(PRIVATE)))
    # Uncaught exceptions must not propagate to uvicorn's raw traceback logger.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
                                base_url="http://127.0.0.1") as client:
        response = await client.get("/api/admin/settings")
        assert response.status_code == 500 and PRIVATE not in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


async def test_concurrent_playground_work_rejected_and_shutdown_cancels_only_owned_task(config, monkeypatch):
    app = create_app(config)
    started = asyncio.Event()
    unrelated = asyncio.create_task(asyncio.Event().wait(), name="unrelated-test-task")
    request = None
    try:
        async with app.router.lifespan_context(app):
            pg = app.state.admin.playground

            async def slow(comment):
                started.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(pg.processor, "process", slow)
            close = AsyncMock()
            monkeypatch.setattr(pg.processor, "aclose", close)
            request = asyncio.create_task(pg.analyze("Question?"))
            await asyncio.wait_for(started.wait(), 1)
            with pytest.raises(PlaygroundBusy):
                await pg.analyze("Another question?")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                response = await client.post(ANALYZE, json={"text": "Question?"},
                    headers={"X-Admin-CSRF": app.state.admin.csrf_token})
                assert response.status_code == 409
        with pytest.raises(asyncio.CancelledError):
            await request
        close.assert_awaited_once()
        assert not unrelated.done() and not pg.history
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "playground-ai"]
    finally:
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)


async def test_history_limits_and_sanitized_live_projection(admin_client):
    app, client = admin_client
    pg = app.state.admin.playground
    for _ in range(105):
        await pg.analyze("Hello!")
    assert len(pg.history) == 100 and pg.processed == 105
    processor = pg.processor
    for _ in range(55):
        await app.state.admin.handle_live(Comment(username="viewer", user_id=PRIVATE, text="Hello!"), processor)
    assert len(app.state.admin.live_history) == 50 and app.state.admin.metrics.processed == 55
    assert PRIVATE not in (await client.get("/api/admin/live/history")).text
    assert all(r.source == "fake" for r in app.state.admin.live_history)
    assert all(r.source == "playground" for r in pg.history)
    app.state.admin.live_service = SimpleNamespace(queue=asyncio.Queue())
    app.state.admin.on_live_state(LiveStatus(username=PRIVATE, mode="real", state=LiveState.CONNECTED, reason=PRIVATE))
    assert app.state.admin.live_view().activity == "active"
    app.state.admin.on_live_state(LiveStatus(username=PRIVATE, mode="real", state=LiveState.STOPPED,
                                          terminal_state=LiveState.ENDED, reason=PRIVATE))
    assert app.state.admin.live_view().activity == "inactive"
    assert PRIVATE not in app.state.admin.live_view().model_dump_json()


async def test_opt_in_fake_live_separate_processors_and_events(config, monkeypatch):
    from app.live.service import LiveService

    created = []

    def service(*args, **kwargs):
        result = LiveService(*args, **kwargs)
        created.append(result)
        return result

    monkeypatch.setattr("app.main.LiveService", service)
    app = create_app(config.model_copy(update={"admin_live_enabled": True}))
    async with app.router.lifespan_context(app):
        assert app.state.comment_processor is not app.state.admin.playground.processor
        subscriber = app.state.admin.events.subscribe()
        await asyncio.wait_for(app.state.live_task, 1)
        assert len(app.state.admin.live_history) == 3
        assert app.state.admin.playground.processed == 0 and not app.state.admin.playground.history
        kinds = []
        while not subscriber.queue.empty():
            kinds.append(subscriber.queue.get_nowait().name)
        assert "live_state" in kinds and kinds.count("comment_received") == kinds.count("reply_suggestion") == 3
        assert app.state.admin.live_view().state == "stopped"
        await app.state.admin.playground.analyze("Hello!")
        assert created == [app.state.live_service]


async def test_shutdown_cancels_pending_model_check_and_closes_shared_client(config, tmp_path):
    app = create_app(provider_config(config, tmp_path, "lmstudio"))
    started = asyncio.Event()

    async def respond(request):
        started.set()
        await asyncio.Event().wait()

    async with app.router.lifespan_context(app):
        engine = app.state.admin.playground.processor.engine
        engine._transport = httpx.MockTransport(respond)
        check = asyncio.create_task(app.state.admin.check_ai())
        await asyncio.wait_for(started.wait(), 1)
        owner = engine._client
    with pytest.raises(asyncio.CancelledError):
        await check
    assert owner.is_closed
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "admin-ai-check"]


def event(id="one", text="line\ndata: secret\nevent: forged"):
    return CommentView(id=id, source="tiktok", username="viewer", text=text, timestamp=NOW)


async def test_bounded_fanout_slow_subscriber_and_frame_encoding():
    bus = AdminEventBus(queue_size=2)
    slow = bus.subscribe()
    fast = bus.subscribe()
    for i in range(50):
        bus.publish("comment_received", event(str(i)))
        assert (await fast.queue.get()).id == i + 1
    assert slow.queue.qsize() == 2 and slow.overflowed
    assert fast.queue.empty() and not fast.overflowed
    last = slow.queue.get_nowait()
    assert last.id == 49
    assert "\nevent: forged" not in last.encode()
    assert json.loads(last.data)["text"] == event().text
    with pytest.raises(ValueError):
        bus.publish("unknown", event())
    with pytest.raises(ValueError):
        bus.publish("comment_received", {"raw": PRIVATE})
    bus.close()
    assert await fast.queue.get() is None and await slow.queue.get() is None
    assert not bus.subscribers
    with pytest.raises(RuntimeError):
        bus.subscribe()


async def test_sse_snapshot_resync_event_cleanup_and_subscriber_limit(admin_client):
    app, _ = admin_client
    app.state.admin.events = AdminEventBus(queue_size=1, max_subscribers=1)
    request = SimpleNamespace(app=app, is_disconnected=AsyncMock(return_value=False))
    response = await event_route(request)
    stream = response.body_iterator
    assert response.media_type == "text/event-stream"
    first = await anext(stream)
    assert "event: snapshot" in first and PRIVATE not in first
    with pytest.raises(HTTPException) as exc:
        await event_route(request)
    assert exc.value.status_code == 503
    app.state.admin.events.publish("comment_received", event())
    app.state.admin.events.publish("comment_received", event("two"))
    assert "event: resync" in await anext(stream)
    assert "event: comment_received" in await anext(stream)
    await stream.aclose()
    assert not app.state.admin.events.subscribers


async def test_real_asgi_sse_disconnect_releases_subscriber(admin_client):
    app, _ = admin_client
    disconnected, snapshot = asyncio.Event(), asyncio.Event()
    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and b"event: snapshot" in message.get("body", b""):
            snapshot.set()

    scope = dict(type="http", asgi={"version": "3.0"}, method="GET", scheme="http", path="/api/admin/events",
                 raw_path=b"/api/admin/events", query_string=b"", root_path="", http_version="1.1",
                 client=("127.0.0.1", 123), server=("127.0.0.1", 8000), headers=[(b"host", b"127.0.0.1:8000")])
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(snapshot.wait(), 1)
        assert len(app.state.admin.events.subscribers) == 1
        disconnected.set()
        await asyncio.wait_for(task, 1)
        assert not app.state.admin.events.subscribers
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
