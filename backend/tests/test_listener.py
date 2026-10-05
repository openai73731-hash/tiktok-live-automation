import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from TikTokLive import TikTokLiveClient
from TikTokLive.client.errors import UserOfflineError, WebcastBlockedError
from TikTokLive.events import (
    AccessControlEvent, CommentEvent, ConnectEvent, LiveEndEvent, UnknownEvent,
)

from app.config import Settings
from app.live.listener import FakeLiveListener, LiveListener, comment_from_event
from app.live.models import LiveState


def real_settings(**kwargs):
    return Settings(_env_file=None, live_mode="real", tiktok_username="owner", **kwargs)


def sdk_comment(**changes):
    body = {"user": {"displayId": "john92", "id": "123", "nickname": "John"},
            "content": "How does the bot work?"}
    body.update(changes)
    return CommentEvent.from_dict(body)


def test_real_sdk_v3_event_conversion():
    comment = comment_from_event(sdk_comment())
    assert comment.username == "john92"
    assert comment.user_id == "123"
    assert comment.text == "How does the bot work?"


def test_missing_handle_uses_nickname_and_missing_id_is_none():
    comment = comment_from_event(sdk_comment(user={"nickname": "Alice"}))
    assert comment.username == "Alice"
    assert comment.user_id is None


@pytest.mark.parametrize("bad", [
    None, SimpleNamespace(user=None),
    SimpleNamespace(user=SimpleNamespace(display_id="x", nickname="x", id=0), content=None),
    sdk_comment(content="   "),
    sdk_comment(user={}),
])
def test_bad_event_is_skipped_and_next_comment_survives(bad, caplog):
    listener = LiveListener(real_settings(), asyncio.Queue())
    with caplog.at_level(logging.WARNING, logger="tiktok_assistant"):
        listener.on_comment(bad)
        listener.on_comment(sdk_comment())
    assert listener.queue.qsize() == 1
    assert listener.queue.get_nowait().username == "john92"
    assert "malformed_comment_skipped" in caplog.text


def test_queue_overflow_is_bounded_and_logged(caplog):
    queue = asyncio.Queue(maxsize=1)
    listener = LiveListener(real_settings(), queue)
    listener.on_comment(sdk_comment())
    listener.on_comment(sdk_comment())
    assert queue.qsize() == 1
    assert listener.dropped_comments == 1
    assert "comment_dropped_queue_full" in caplog.text


async def test_fake_produces_examples(settings):
    queue = asyncio.Queue()
    listener = FakeLiveListener(settings, queue)
    await listener.run()
    assert [(c.username, c.text) for c in [queue.get_nowait() for _ in range(3)]] == [
        ("john92", "How does the bot work?"),
        ("alice", "Is it automatic?"),
        ("mike", "Hello!"),
    ]
    assert listener.status.state == LiveState.STOPPED


async def test_temporary_error_retries_then_end_stops(monkeypatch):
    listener = LiveListener(real_settings(reconnect_initial_seconds=0.001), asyncio.Queue())
    calls = []

    async def session():
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("temporary")
        listener.on_end(None)

    monkeypatch.setattr(listener, "_session", session)
    await asyncio.wait_for(listener.run(), 1)
    assert len(calls) == 2
    assert listener.status.state == LiveState.STOPPED
    assert listener.status.terminal_state == LiveState.ENDED


async def test_offline_retries_with_bounded_exponential_delay(monkeypatch):
    listener = LiveListener(real_settings(
        reconnect_initial_seconds=0.01, reconnect_max_seconds=0.025,
    ), asyncio.Queue())
    delays = []
    calls = 0

    async def session():
        nonlocal calls
        calls += 1
        if calls < 5:
            raise UserOfflineError()
        listener.on_end(None)

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(listener, "_session", session)
    monkeypatch.setattr("app.live.listener.asyncio.sleep", sleep)
    await listener.run()
    assert delays == [0.01, 0.02, 0.025, 0.025]


@pytest.mark.parametrize("error", [WebcastBlockedError(), RuntimeError("unknown state")])
async def test_unknown_or_security_error_never_retries(error, monkeypatch):
    listener = LiveListener(real_settings(), asyncio.Queue())
    session = AsyncMock(side_effect=error)
    monkeypatch.setattr(listener, "_session", session)
    await listener.run()
    session.assert_awaited_once()
    assert listener.status.state == LiveState.STOPPED
    assert listener.status.terminal_state == LiveState.OPERATOR_REQUIRED


@pytest.mark.parametrize("event", [AccessControlEvent(), UnknownEvent()])
def test_security_state_is_latched(event):
    listener = LiveListener(real_settings(), asyncio.Queue())
    listener.on_security(event)
    listener.on_disconnect(None)
    listener.on_connect(None)
    listener.on_end(None)
    listener.on_comment(sdk_comment())
    assert listener.status.state == LiveState.OPERATOR_REQUIRED
    assert listener.queue.empty()


async def test_cancel_during_retry_is_prompt(monkeypatch):
    listener = LiveListener(real_settings(reconnect_initial_seconds=60), asyncio.Queue())
    retry = asyncio.Event()
    listener.on_state = lambda status: retry.set() if status.state == LiveState.RETRYING else None
    monkeypatch.setattr(listener, "_session", AsyncMock(side_effect=httpx.ConnectError("lost")))
    task = asyncio.create_task(listener.run())
    await asyncio.wait_for(retry.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert listener.status.state == LiveState.STOPPED


async def test_sdk_dispatch_and_shutdown_do_not_cancel_other_tasks(monkeypatch):
    # Use the installed client's real event emitter and disconnect implementation.
    client = TikTokLiveClient(unique_id="owner")
    disconnected = asyncio.Event()
    unrelated = asyncio.create_task(asyncio.Event().wait())
    original_web_close = client.web.close
    closed = AsyncMock(wraps=original_web_close)
    monkeypatch.setattr(client.web, "close", closed)
    monkeypatch.setattr(client, "close", AsyncMock(side_effect=AssertionError("unsafe close")))

    async def socket_loop():
        client.emit(ConnectEvent.get_type(), ConnectEvent(unique_id="owner", room_id=1))
        client.emit(CommentEvent.get_type(), sdk_comment())
        client.emit(LiveEndEvent.get_type(), LiveEndEvent())
        await disconnected.wait()

    async def start():
        task = asyncio.create_task(socket_loop())
        client._event_loop_task = task
        return task

    monkeypatch.setattr(client, "start", start)
    monkeypatch.setattr(client._ws, "disconnect", AsyncMock(side_effect=disconnected.set))
    listener = LiveListener(real_settings(), asyncio.Queue(), client_factory=lambda **_: client)
    try:
        await asyncio.wait_for(listener.run(), 2)
        assert listener.queue.qsize() == 1
        assert listener.status.state == LiveState.STOPPED
        assert listener.status.terminal_state == LiveState.ENDED
        closed.assert_awaited_once()
        assert not unrelated.done()
    finally:
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)
        await original_web_close()


async def test_cancel_active_connection_closes_socket_and_http(monkeypatch):
    started = asyncio.Event()
    stop_socket = asyncio.Event()
    client = SimpleNamespace(
        add_listener=lambda *args: None,
        web=SimpleNamespace(close=AsyncMock()),
        disconnect=AsyncMock(side_effect=stop_socket.set),
    )

    async def start():
        started.set()
        return asyncio.create_task(stop_socket.wait())

    client.start = start
    listener = LiveListener(real_settings(), asyncio.Queue(), client_factory=lambda **_: client)
    task = asyncio.create_task(listener.run())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    client.disconnect.assert_awaited_once()
    client.web.close.assert_awaited_once()


async def test_start_timeout_closes_http(monkeypatch):
    client = SimpleNamespace(
        add_listener=lambda *args: None, start=AsyncMock(side_effect=lambda: None),
        disconnect=AsyncMock(), web=SimpleNamespace(close=AsyncMock()),
    )

    async def never_start():
        await asyncio.Event().wait()

    client.start = never_start
    listener = LiveListener(real_settings(connect_timeout_seconds=0.01),
                            asyncio.Queue(), client_factory=lambda **_: client)
    with pytest.raises(TimeoutError):
        await listener._session()
    client.disconnect.assert_awaited_once()
    client.web.close.assert_awaited_once()


@pytest.mark.parametrize("status,expected", [(403, False), (429, False), (503, True)])
def test_http_security_and_service_error_policy(status, expected):
    request = httpx.Request("GET", "https://example.invalid")
    response = httpx.Response(status, request=request)
    error = httpx.HTTPStatusError("test", request=request, response=response)
    assert LiveListener.retryable(error) is expected
