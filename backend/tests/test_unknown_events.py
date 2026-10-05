"""Unmapped messages are not control states; exercise the installed SDK offline."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from TikTokLive.events import (
    AccessControlEvent, CommentEvent, ConnectEvent, ControlEvent,
    PerceptionEvent, RoomVerifyEvent, UnauthorizedMemberEvent, UnknownEvent,
)
from TikTokLive.events.proto_events import EVENT_MAPPINGS
from TikTokLive.proto import (
    ControlAction, ProtoMessageFetchResult, ProtoMessageFetchResultBaseProtoMessage,
)

from app.config import Settings
from app.live.listener import LiveListener, create_sdk_client, unknown_event_metadata
from app.live.models import LiveState
from app.logging_config import configure_logging


def envelope(*methods, is_first=False):
    return ProtoMessageFetchResult(
        is_first=is_first, cursor="PRIVATE_CURSOR", ws_params="PRIVATE_WS_PARAMS",
        messages=[ProtoMessageFetchResultBaseProtoMessage(
            method=method, payload=b"PRIVATE_PAYLOAD",
        ) for method in methods],
    )


@pytest.mark.parametrize("count", [1, 2])
def test_unmapped_messages_keep_connected_and_do_not_enqueue(count):
    listener = LiveListener(Settings(_env_file=None, live_mode="real", tiktok_username="owner"),
                            asyncio.Queue())
    listener.on_connect(None)
    for i in range(count):
        # Words in an unmapped method are not security signals.
        listener.on_unknown(UnknownEvent(raw=envelope("WebcastSecurityVerificationUnknown")))
        assert listener.status.state == LiveState.CONNECTED
        assert listener.status.terminal_state is None
        assert not listener.halt.is_set()
        assert listener.queue.empty()  # No Comment can reach the processor/AI.
        assert listener.unknown_event_count == i + 1
    listener.on_comment(CommentEvent.from_dict({
        "user": {"displayId": "viewer"}, "content": "Is this bot trading automatically?",
    }))
    assert listener.queue.qsize() == 1


async def test_sdk_parser_returns_unknown_with_outer_envelope_and_connect_first():
    client = create_sdk_client(unique_id="owner")
    client._room_id = 1
    raw = envelope("WebcastFixtureUnmappedOne", "WebcastFixtureUnmappedTwo", is_first=True)
    assert all(m.method not in EVENT_MAPPINGS for m in raw.messages)
    try:
        events = [event async for event in client._parse_webcast_response(raw)]
        assert isinstance(events[0], ConnectEvent)
        unknown = [event for event in events if isinstance(event, UnknownEvent)]
        assert len(unknown) == 2
        assert all(event.raw is raw for event in unknown)
        for event in unknown:
            assert unknown_event_metadata(event) == {
                "unknown_method_candidates": ["WebcastFixtureUnmappedOne", "WebcastFixtureUnmappedTwo"],
                "payload_length": 2 * len(b"PRIVATE_PAYLOAD"), "is_first_response": True,
            }
    finally:
        await client.web.close()


@pytest.mark.parametrize("method", [
    "https://signed.invalid/?token=SECRET", "cookie=sessionid=SECRET", "\x1b[31mSECRET",
    "Webcast\nSECRET", "WebcastСекрет", "A" * 97, "", "roomId:123456",
])
def test_unknown_metadata_redacts_untrusted_method(method, tmp_path, capsys):
    settings = Settings(_env_file=None, live_mode="real", tiktok_username="owner",
                        diagnostic=True, log_dir=tmp_path)
    configure_logging(settings)
    listener = LiveListener(settings, asyncio.Queue())
    listener.on_unknown(UnknownEvent(raw=envelope(method, "WebcastChatMessage", is_first=True)))
    raw_log = (tmp_path / "app.jsonl").read_text(encoding="utf-8")
    record = json.loads(raw_log)
    assert set(record) == {"timestamp", "level", "event", "logger", "unknown_event_count",
                           "unknown_method_candidates", "payload_length", "is_first_response"}
    assert record["unknown_method_candidates"] == ["invalid_or_redacted"]
    assert record["payload_length"] == len(b"PRIVATE_PAYLOAD")
    assert record["is_first_response"] is True
    assert record["unknown_event_count"] == 1
    assert "unknown_method" not in record
    output = raw_log + capsys.readouterr().out
    for secret in ("SECRET", "PRIVATE_", "cookie", "sessionid", "https://", "cursor", "ws_params", "roomId"):
        assert secret not in output


def test_unknown_candidates_are_unique_and_bounded():
    raw = envelope(*(["A" * 96] * 3), *(f"WebcastUnmapped{i}" for i in range(100)))
    fields = unknown_event_metadata(UnknownEvent(raw=raw))
    assert len(fields["unknown_method_candidates"]) == 16
    assert fields["unknown_method_candidates"][0] == "A" * 96
    assert fields["payload_length"] == 103 * len(b"PRIVATE_PAYLOAD")


def test_nondiagnostic_unknown_logs_only_count(tmp_path, capsys):
    settings = Settings(_env_file=None, live_mode="real", tiktok_username="owner", log_dir=tmp_path)
    configure_logging(settings)
    listener = LiveListener(settings, asyncio.Queue())
    listener.on_unknown(UnknownEvent(raw=envelope("WebcastUnmapped")))
    record = json.loads((tmp_path / "app.jsonl").read_text(encoding="utf-8"))
    assert set(record) == {"timestamp", "level", "event", "logger", "unknown_event_count"}
    assert "WebcastUnmapped" not in capsys.readouterr().out


@pytest.mark.parametrize("event", [
    AccessControlEvent(), RoomVerifyEvent(), PerceptionEvent(), UnauthorizedMemberEvent(),
    ControlEvent(action=ControlAction.STREAM_SUSPENDED),
    ControlEvent(action=ControlAction.FALLBACK_UNKNOWN),
    ControlEvent(action=ControlAction.STREAM_ENDED),
])
async def test_structural_events_after_unknown_still_use_real_handlers(event, monkeypatch):
    client = create_sdk_client(unique_id="owner")
    listener = LiveListener(Settings(_env_file=None, live_mode="real", tiktok_username="owner"),
                            asyncio.Queue(), client_factory=lambda **_: client)
    disconnected = asyncio.Event()

    async def socket_loop():
        client.emit(ConnectEvent.get_type(), ConnectEvent(unique_id="owner", room_id=1))
        client.emit(UnknownEvent.get_type(), UnknownEvent(raw=envelope("WebcastUnmapped")))
        assert listener.status.state == LiveState.CONNECTED
        assert listener.queue.empty()
        if isinstance(event, ControlEvent):
            # Parse actual protobuf through the adapter's control guard.
            message = ProtoMessageFetchResultBaseProtoMessage(
                method="WebcastControlMessage", payload=bytes(event),
            )
            events = await client._parse_webcast_response_message(
                webcast_response=ProtoMessageFetchResult(messages=[message]),
                webcast_response_message=message,
            )
            for parsed in events:
                client.emit(parsed.get_type(), parsed)
        else:
            client.emit(event.get_type(), event)
        await disconnected.wait()

    start = AsyncMock(side_effect=lambda: asyncio.create_task(socket_loop()))
    monkeypatch.setattr(client, "start", start)
    monkeypatch.setattr(client._ws, "disconnect", AsyncMock(side_effect=disconnected.set))
    await listener.run()
    start.assert_awaited_once()
    expected = (LiveState.ENDED if isinstance(event, ControlEvent)
                and event.action == ControlAction.STREAM_ENDED else LiveState.NEED_OPERATOR)
    assert listener.status.terminal_state == expected
    assert listener.status.state == LiveState.STOPPED
    assert listener.unknown_event_count == 1
    assert listener.queue.empty()
    assert client.web.httpx_client.is_closed
