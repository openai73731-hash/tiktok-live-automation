"""Stage 3A contracts: installed SDK, mocked transports, no real LIVE."""
import asyncio
import importlib.metadata
import json
from pathlib import Path
import signal
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from TikTokLive import TikTokLiveClient
from TikTokLive.client.errors import UserNotFoundError, UserOfflineError, UnexpectedSignatureError
from TikTokLive.events import (
    AccessControlEvent, CommentEvent, ConnectEvent, ControlEvent, LiveEndEvent,
    PerceptionEvent, RoomVerifyEvent, UnauthorizedMemberEvent, UnknownEvent,
)
from TikTokLive.proto import ControlAction, ProtoMessageFetchResult
from websockets.frames import Close
from websockets.exceptions import ConnectionClosedError
from websockets.legacy.exceptions import InvalidStatusCode

from app.ai.factory import create_processor
from app.ai.fake_engine import FakeAIReplyEngine
from app.config import Settings
from app.live.listener import AmbiguousSDKState, LiveListener, create_sdk_client
from app.live.models import LiveState
from app.logging_config import configure_logging
from app.main import cli, run_listener


def config(**kwargs):
    return Settings(_env_file=None, live_mode="real", tiktok_username="owner",
                    diagnostic=True, **kwargs)


def records(path):
    return [json.loads(line) for line in (path / "app.jsonl").read_text(
        encoding="utf-8",
    ).splitlines()]


def test_diagnostic_requires_real_and_bounded_retry_settings():
    for values in ({"diagnostic": True}, {"reconnect_max_attempts": 0},
                   {"reconnect_max_attempts": 101}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **values)


def test_diagnostic_forces_fake_provider_without_editing_yaml(tmp_path):
    path = tmp_path / "project.yaml"
    path.write_text(Path("config/project.yaml").read_text(encoding="utf-8").replace(
        "provider: fake", "provider: openai",
    ), encoding="utf-8")
    processor = create_processor(config(project_config_path=path))
    assert isinstance(processor.engine, FakeAIReplyEngine)
    assert processor.project.ai.provider == "fake"
    assert "openai" in path.read_text(encoding="utf-8")


async def test_cli_pipeline_from_installed_sdk_events(monkeypatch, tmp_path, capsys):
    client = create_sdk_client(unique_id="owner")
    closed = AsyncMock(wraps=client.web.close)
    monkeypatch.setattr(client.web, "close", closed)
    disconnected = asyncio.Event()

    async def socket_loop():
        client.emit(ConnectEvent.get_type(), ConnectEvent(unique_id="owner", room_id=1))
        for text in ("Hello", "Is this bot trading automatically?"):
            client.emit(CommentEvent.get_type(), CommentEvent.from_dict({
                "user": {"displayId": "john92", "id": "123"}, "content": text,
            }))
        client.emit(LiveEndEvent.get_type(), LiveEndEvent())
        await disconnected.wait()

    async def start():
        client._event_loop_task = asyncio.create_task(socket_loop())
        return client._event_loop_task

    monkeypatch.setattr(client, "start", start)
    monkeypatch.setattr(client._ws, "disconnect", AsyncMock(side_effect=disconnected.set))
    monkeypatch.setattr("app.live.listener.create_sdk_client", lambda **_: client)
    settings = config(log_dir=tmp_path)
    configure_logging(settings)
    assert await run_listener(settings) == 0
    closed.assert_awaited_once()
    output = capsys.readouterr().out
    for value in ("STARTING @owner", "CONNECTING @owner", "CONNECTED @owner",
                  "ENDED @owner", "STOPPED @owner", "@john92:", "ReplySuggestion",
                  "status: ready", "Этот бот торгует автоматически?",
                  "requires_approval: true", "reply_translation:", "provider: fake"):
        assert value in output
    items = records(tmp_path)
    suggestions = [r["suggestion"] for r in items if r["event"] == "reply_suggestion"]
    assert [s["status"] for s in suggestions] == ["skipped", "ready"]
    assert suggestions[1]["fact_ids"] == ["automatic_analysis", "automatic_trades"]
    assert all(s["requires_approval"] for s in suggestions)
    states = [r["state"] for r in items if r["event"] == "live_state"]
    assert states == ["starting", "connecting", "connected", "ended", "stopped"]


async def test_reconnect_transitions_and_safe_attempt_logging(monkeypatch, tmp_path):
    settings = config(log_dir=tmp_path, reconnect_initial_seconds=0.001)
    configure_logging(settings)
    listener = LiveListener(settings, asyncio.Queue())
    calls = 0

    async def session():
        nonlocal calls
        calls += 1
        listener.on_connect(None)
        if calls == 1:
            raise httpx.ConnectError("sessionid=SECRET https://signed.invalid/?token=SECRET")
        listener.on_end(None)

    monkeypatch.setattr(listener, "_session", session)
    await listener.run()
    items = records(tmp_path)
    assert [r["state"] for r in items if r["event"] == "live_state"] == [
        "starting", "connecting", "connected", "reconnecting", "connecting",
        "connected", "ended", "stopped",
    ]
    retry = next(r for r in items if r["event"] == "reconnect_scheduled")
    assert (retry["attempt"], retry["delay_seconds"], retry["error_type"]) == (1, 0.001, "ConnectError")
    assert "SECRET" not in (tmp_path / "app.jsonl").read_text(encoding="utf-8")


async def test_retry_budget_exhaustion_stops(monkeypatch):
    listener = LiveListener(config(reconnect_max_attempts=2, reconnect_initial_seconds=0.001),
                            asyncio.Queue())
    session = AsyncMock(side_effect=UserOfflineError())
    monkeypatch.setattr(listener, "_session", session)
    await listener.run()
    assert session.await_count == 3  # Initial connection plus two retries.
    assert listener.status.state == LiveState.STOPPED
    assert listener.status.terminal_state == LiveState.NEED_OPERATOR


@pytest.mark.parametrize("event", [AccessControlEvent(), RoomVerifyEvent(), PerceptionEvent(),
                                  UnauthorizedMemberEvent(), UnknownEvent()])
async def test_security_events_stop_without_retry(event, monkeypatch):
    listener = LiveListener(config(), asyncio.Queue())

    async def session():
        listener.on_security(event)
        listener.on_connect(None)
        listener.on_end(None)

    mock = AsyncMock(side_effect=session)
    monkeypatch.setattr(listener, "_session", mock)
    await listener.run()
    mock.assert_awaited_once()
    assert listener.status.terminal_state == LiveState.NEED_OPERATOR
    assert listener.status.state == LiveState.STOPPED


@pytest.mark.parametrize("error", [UserNotFoundError("owner"), UnexpectedSignatureError("SECRET"),
                                  RuntimeError("CAPTCHA login verification SECRET")])
async def test_need_operator_exit_code_and_no_secret_output(error, monkeypatch, tmp_path, capsys):
    session = AsyncMock(side_effect=error)
    monkeypatch.setattr(LiveListener, "_session", session)
    settings = config(log_dir=tmp_path)
    configure_logging(settings)
    assert await run_listener(settings) == 2
    session.assert_awaited_once()
    output = capsys.readouterr().out
    assert "NEED_OPERATOR" in output and "STOPPED" in output
    assert "SECRET" not in output
    assert "SECRET" not in (tmp_path / "app.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize("status, retry", [(403, False), (429, False), (503, True)])
def test_websocket_handshake_policy(status, retry):
    assert LiveListener.retryable(InvalidStatusCode(status, {})) is retry


@pytest.mark.parametrize("code, retry", [(1008, False), (4003, False), (1011, True)])
def test_websocket_close_policy(code, retry):
    assert LiveListener.retryable(ConnectionClosedError(Close(code, "SECRET"), None)) is retry


@pytest.mark.parametrize("status", [403, 429, 302])
async def test_installed_sdk_http_guard_has_no_fallback(status, monkeypatch):
    assert importlib.metadata.version("TikTokLive") == "7.0.1"
    client = create_sdk_client(unique_id="owner")
    request = httpx.Request("GET", "https://example.invalid/?secret=SECRET")
    response = httpx.Response(status, request=request)
    hook = client.web.httpx_client.event_hooks["response"][0]

    async def fetch(_):
        await hook(response)

    fallback = AsyncMock(side_effect=AssertionError("must never fall back"))
    monkeypatch.setattr(client.web, "fetch_room_id_from_html", fetch)
    monkeypatch.setattr(client.web, "fetch_room_id_from_api", fallback)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.start()
        fallback.assert_not_awaited()
    finally:
        await client.web.close()


async def test_adapter_start_contract_and_cancellation(monkeypatch):
    client = create_sdk_client(unique_id="owner")
    monkeypatch.setattr(client.web, "fetch_room_id_from_html", AsyncMock(return_value="123"))
    alive = AsyncMock(return_value=[True])
    monkeypatch.setattr(client.web.fetch_is_live, "fetch_is_live_room_ids", alive)
    socket_task = asyncio.create_task(asyncio.sleep(0))
    start = AsyncMock(return_value=socket_task)
    monkeypatch.setattr(TikTokLiveClient, "start", start)
    try:
        assert await client.start() is socket_task
        start.assert_awaited_once_with(room_id=123, fetch_live_check=False)
        alive.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await client.start()
    finally:
        await socket_task
        await client.web.close()


@pytest.mark.parametrize("action", [ControlAction.STREAM_ENDED, ControlAction.STREAM_SUSPENDED,
                                   ControlAction.FALLBACK_UNKNOWN])
async def test_control_event_no_untracked_disconnect_task(action, monkeypatch):
    client = create_sdk_client(unique_id="owner")
    disconnect = AsyncMock(side_effect=AssertionError("listener owns shutdown"))
    monkeypatch.setattr(client, "disconnect", disconnect)
    event = ControlEvent(action=action)
    try:
        if action == ControlAction.STREAM_ENDED:
            result = await client.handle_custom_event(SimpleNamespace(payload=bytes(event)), event)
            assert isinstance(result, LiveEndEvent)
        else:
            with pytest.raises(AmbiguousSDKState):
                await client.handle_custom_event(SimpleNamespace(payload=bytes(event)), event)
        await asyncio.sleep(0)
        disconnect.assert_not_awaited()
    finally:
        await client.web.close()


async def test_unparseable_sdk_event_fails_closed_without_raw_output(capsys):
    client = create_sdk_client(unique_id="owner")
    try:
        with pytest.raises(AmbiguousSDKState):
            await client._parse_webcast_response_message(
                webcast_response=ProtoMessageFetchResult(),
                webcast_response_message=SimpleNamespace(method="WebcastChatMessage", payload=b"\x32\x01\xffSECRET"),
            )
        output = capsys.readouterr()
        assert "SECRET" not in output.out + output.err
    finally:
        await client.web.close()


async def test_sdk_logs_are_suppressed_and_heartbeat_is_owned(monkeypatch, tmp_path, capsys):
    client = create_sdk_client(unique_id="owner")
    settings = config(log_dir=tmp_path)
    configure_logging(settings)
    listener = LiveListener(settings, asyncio.Queue(), client_factory=lambda **_: client)
    unrelated = asyncio.create_task(asyncio.Event().wait())
    heartbeat = asyncio.create_task(asyncio.Event().wait())
    client._ws._ping_loop = heartbeat
    signing_http = client.web.signer.sdk_client.get_async_httpx_client()

    async def start():
        client.logger.error("cookies=SECRET raw=SECRET token=SECRET")
        raise RuntimeError("SECRET")

    monkeypatch.setattr(client, "start", start)
    try:
        await listener.run()
        assert heartbeat.done()
        assert not unrelated.done()
        output = capsys.readouterr()
        assert "SECRET" not in output.out + output.err
        assert "SECRET" not in (tmp_path / "app.jsonl").read_text(encoding="utf-8")
        assert client.web.httpx_client.is_closed
        assert signing_http.is_closed
    finally:
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)


async def test_connect_event_deadline_and_disconnect_timeout():
    tasks = []

    async def start():
        tasks.append(asyncio.create_task(asyncio.Event().wait()))
        return tasks[-1]

    async def disconnect():
        await asyncio.Event().wait()

    client = SimpleNamespace(add_listener=lambda *args: None, start=start,
                             disconnect=disconnect, web=SimpleNamespace(close=AsyncMock()))
    listener = LiveListener(config(connect_timeout_seconds=0.01, shutdown_timeout_seconds=0.01),
                            asyncio.Queue(), client_factory=lambda **_: client)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(listener._session(), 1)
    assert tasks[0].done()
    client.web.close.assert_awaited_once()


def test_cli_ctrl_c_uses_asyncio_runner_shutdown(monkeypatch, tmp_path, capsys):
    # Exercise actual SIGINT -> Runner cancellation -> listener cleanup, in process.
    # No OS console window or real network is needed on Windows.
    monkeypatch.setattr(sys, "argv", ["app.main", "--mode", "real", "--username", "@owner",
                                     "--diagnostic", "--env-file", str(tmp_path / "absent")])
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    cleaned = []

    async def session(self):
        assert self.settings.diagnostic
        self.on_connect(None)
        asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGINT)
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    monkeypatch.setattr(LiveListener, "_session", session)
    with pytest.raises(SystemExit) as result:
        cli()
    assert result.value.code == 0
    assert cleaned == [True]
    output = capsys.readouterr()
    assert "STOPPED @owner" in output.out
    assert "Traceback" not in output.out + output.err


def test_external_connections_and_dns_are_forbidden():
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="External network forbidden"):
            sock.connect(("203.0.113.1", 443))
    with pytest.raises(AssertionError, match="External DNS forbidden"):
        socket.getaddrinfo("example.invalid", 443)
