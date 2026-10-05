"""Exercise installed 7.0.1 routes over MockTransport, never the real LIVE."""
import asyncio
from contextlib import asynccontextmanager
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from TikTokLive import TikTokLiveClient
from TikTokLive.client.web.routes.fetch_room_id_live_html import FailedParseRoomIdError

from app.config import Settings
from app.live.listener import AmbiguousSDKState, LiveListener, create_sdk_client
from app.live.models import LiveState
from app.logging_config import configure_logging


def html(user):
    return '<script id="SIGI_STATE" type="application/json">' + json.dumps({
        "LiveRoom": {"liveRoomUserInfo": {"user": user}},
    }) + '</script>'


@asynccontextmanager
async def mocked_client(handler, *, diagnostic=True):
    client = create_sdk_client(unique_id="owner", diagnostic=diagnostic)
    original = client.web.httpx_client
    client.web._httpx = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), event_hooks=original.event_hooks,
    )
    await original.aclose()
    try:
        yield client
    finally:
        await client.web.close()


def results(caplog):
    return [r.fields for r in caplog.records if r.msg == "room_lookup_result"]


@pytest.mark.parametrize("body,expected,exception", [
    (html({"roomId": "123", "status": 2}), (True, True, True, True), None),
    ('<html><h1>Public LIVE</h1></html>', (False, False, False, False), FailedParseRoomIdError),
    (html({"status": 2}), (True, True, False, False), FailedParseRoomIdError),
    ('<script id="SIGI_STATE" type="application/json">{broken</script>',
     (True, False, False, False), FailedParseRoomIdError),
])
async def test_html_lookup_actual_sdk_parser(body, expected, exception, caplog):
    caplog.set_level("INFO", logger="tiktok_assistant")
    requests = []

    def transport(request):
        requests.append(request.url.path)
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    async with mocked_client(transport) as client:
        if exception:
            with pytest.raises(exception):
                await client.web.fetch_room_id_from_html("owner")
        else:
            assert await client.web.fetch_room_id_from_html("owner") == "123"
    item, = results(caplog)
    assert tuple(item[key] for key in (
        "expected_embedded_json_marker_found", "embedded_json_valid",
        "expected_room_id_marker_found", "parsed_room_id",
    )) == expected
    assert item["response_category"] == "html"
    assert item["http_status"] == 200
    assert item["response_length"] == len(body.encode())
    assert item["room_lookup_strategy"] == "html"
    assert item["request_stage"] == ("sdk_parse" if exception else "lookup_complete")
    assert requests == ["/@owner/live"]
    if exception:
        assert item["exception_type"] == "FailedParseRoomIdError"
        assert item["sdk_error_site"] == "html.parse_room_id"
        assert isinstance(item["sdk_error_line"], int)


@pytest.mark.parametrize("payload,expected,exception", [
    ({"message": "", "data": {"user": {"roomId": "123"}}}, (True, True, True, True), None),
    ({"message": "", "data": {"user": {}}}, (True, True, False, False), FailedParseRoomIdError),
    ({"message": "", "data": {}}, (True, False, False, False), FailedParseRoomIdError),
    ({"message": ""}, (False, False, False, False), FailedParseRoomIdError),
])
async def test_api_lookup_actual_sdk_parser(payload, expected, exception, caplog):
    caplog.set_level("INFO", logger="tiktok_assistant")
    requests = []

    def transport(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=payload)

    async with mocked_client(transport) as client:
        # Explicit offline contract test of the route, NOT a runtime fallback.
        if exception:
            with pytest.raises(exception):
                await client.web.fetch_room_id_from_api("owner")
        else:
            assert await client.web.fetch_room_id_from_api("owner") == 123
    item, = results(caplog)
    assert item["valid_json"] is True
    assert tuple(item[key] for key in (
        "data_present", "user_present", "room_id_field_present", "parsed_room_id",
    )) == expected
    assert item["room_lookup_strategy"] == "api"
    assert item["response_category"] == "json"
    assert requests == ["/api-live/user/room/"]
    if exception:
        assert item["sdk_error_site"] == "api.parse_room_id"


@pytest.mark.parametrize("status,body,headers,category,exception", [
    (200, '<html><title>Verify to continue</title></html>',
     {"content-type": "text/html"}, "blocked_or_challenge", AmbiguousSDKState),
    (200, '<html><title>Security challenge</title>' + html({"roomId": "123"}) + '</html>',
     {"content-type": "text/html"}, "blocked_or_challenge", AmbiguousSDKState),
    (503, '<html><title>Verify you are human</title></html>',
     {"content-type": "text/html"}, "blocked_or_challenge", AmbiguousSDKState),
    (403, 'denied', {"content-type": "text/plain"}, "blocked_or_challenge", httpx.HTTPStatusError),
    (429, '', {}, "blocked_or_challenge", httpx.HTTPStatusError),
    (302, '', {"location": "https://example.invalid/?token=DO_NOT_LOG"},
     "redirect", httpx.HTTPStatusError),
    (200, '', {}, "empty", FailedParseRoomIdError),
    (200, 'not a page', {"content-type": "text/plain"}, "unexpected", FailedParseRoomIdError),
])
async def test_failed_html_diagnostics_preserve_need_operator_without_fallback(
    status, body, headers, category, exception, caplog,
):
    caplog.set_level("INFO", logger="tiktok_assistant")
    requests = []

    def transport(request):
        requests.append(request.url.path)
        return httpx.Response(status, text=body, headers=headers)

    async with mocked_client(transport) as client:
        listener = LiveListener(Settings(_env_file=None, live_mode="real",
            tiktok_username="owner", diagnostic=True), asyncio.Queue(),
            client_factory=lambda **_: client)
        await listener.run()
    assert listener.status.state == LiveState.STOPPED
    assert listener.status.terminal_state == LiveState.NEED_OPERATOR
    assert requests == ["/@owner/live"]  # No API, live-check, signer or redirect request.
    item, = results(caplog)
    assert item["response_category"] == category
    assert item["redirect"] is (status == 302)
    assert item["exception_type"] == exception.__name__
    assert item["parsed_room_id"] is False


async def test_non_json_api_response(caplog):
    caplog.set_level("INFO", logger="tiktok_assistant")
    async with mocked_client(lambda _: httpx.Response(200, text="<html>Not JSON</html>")) as client:
        with pytest.raises(json.JSONDecodeError):
            await client.web.fetch_room_id_from_api("owner")
    item, = results(caplog)
    assert item["response_category"] == "html"
    assert item["valid_json"] is False
    assert item["data_present"] is False
    assert item["parsed_room_id"] is False
    assert item["exception_type"] == "JSONDecodeError"


@pytest.mark.parametrize("strategy", ["html", "api"])
async def test_logs_never_contain_payloads_headers_or_urls(strategy, tmp_path, capsys):
    settings = Settings(_env_file=None, live_mode="real", tiktok_username="owner",
                        diagnostic=True, log_dir=tmp_path)
    configure_logging(settings)
    body = (html({"roomId": "987654321001122", "secret": "PRIVATE_BODY_MARKER"})
            if strategy == "html" else json.dumps({
                "message": "", "data": {"user": {"roomId": "987654321001122"}},
                "token": "PRIVATE_BODY_MARKER",
            }))
    media_type = "text/html" if strategy == "html" else "application/json"

    def transport(_):
        return httpx.Response(200, text=body, headers={
            "content-type": media_type + "; secret=PRIVATE_HEADER_MARKER",
            "set-cookie": "sessionid=PRIVATE_COOKIE_MARKER",
            "authorization": "Bearer PRIVATE_AUTH_MARKER",
            "location": "https://example.invalid/?token=PRIVATE_URL_MARKER",
        })

    async with mocked_client(transport) as client:
        client.web.cookies.set("sessionid", "PRIVATE_REQUEST_COOKIE_MARKER")
        client.web.params["token"] = "PRIVATE_QUERY_MARKER"
        route = getattr(client.web, "fetch_room_id_from_" + strategy)
        await route("owner")
    output = capsys.readouterr()
    logs = (tmp_path / "app.jsonl").read_text(encoding="utf-8")
    for value in ("PRIVATE_", "987654321001122", "https://", "SIGI_STATE", "sessionid"):
        assert value not in output.out + output.err + logs
    items = [json.loads(line) for line in logs.splitlines()]
    assert items[-1]["content_type"] == media_type
    assert items[-1]["parsed_room_id"] is True


async def test_unknown_content_type_is_not_echoed(caplog):
    caplog.set_level("INFO", logger="tiktok_assistant")
    async with mocked_client(lambda _: httpx.Response(200, text=html({"roomId": "123"}),
                             headers={"content-type": "PRIVATE_HEADER_MARKER"})) as client:
        await client.web.fetch_room_id_from_html("owner")
    assert results(caplog)[0]["content_type"] == "other"


async def test_no_lookup_telemetry_without_diagnostic(caplog):
    caplog.set_level("INFO", logger="tiktok_assistant")
    async with mocked_client(lambda _: httpx.Response(200, text=html({"roomId": "123"})),
                             diagnostic=False) as client:
        await client.web.fetch_room_id_from_html("owner")
    assert not results(caplog)
    assert not [r for r in caplog.records if r.msg == "room_lookup_started"]


async def test_html_then_live_check_then_sdk_start_no_api_fallback(monkeypatch, caplog):
    caplog.set_level("INFO", logger="tiktok_assistant")
    paths = []

    def transport(request):
        paths.append(request.url.path)
        if request.url.path == "/@owner/live":
            return httpx.Response(200, text=html({"roomId": "123", "status": 2}))
        assert request.url.path == "/webcast/room/check_alive/"
        return httpx.Response(200, json={"data": [{"alive": True}]})

    async with mocked_client(transport) as client:
        socket_task = asyncio.create_task(asyncio.sleep(0))
        start = AsyncMock(return_value=socket_task)
        monkeypatch.setattr(TikTokLiveClient, "start", start)
        assert await client.start() is socket_task
        await socket_task
        start.assert_awaited_once_with(room_id=123, fetch_live_check=False)
    assert paths == ["/@owner/live", "/webcast/room/check_alive/"]
    assert len(results(caplog)) == 1
    assert [r.fields["request_stage"] for r in caplog.records if r.msg == "connection_stage"] == [
        "live_check", "sdk_start",
    ]


async def test_no_response_network_error_is_logged_without_payload(caplog):
    caplog.set_level("INFO", logger="tiktok_assistant")

    def transport(_):
        raise httpx.ConnectError("PRIVATE_EXCEPTION_MARKER")

    async with mocked_client(transport) as client:
        with pytest.raises(httpx.ConnectError):
            await client.web.fetch_room_id_from_html("owner")
    item, = results(caplog)
    assert item["http_status"] is None and item["response_category"] is None
    assert item["request_stage"] == "request_started"
    assert item["exception_type"] == "ConnectError"
    assert "PRIVATE_EXCEPTION_MARKER" not in json.dumps(item)


async def test_response_hook_reads_stream_and_cancellation_stays_cancellable(caplog):
    caplog.set_level("INFO", logger="tiktok_assistant")

    class BodyStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield html({"roomId": "123"}).encode()

    async with mocked_client(lambda _: httpx.Response(200, stream=BodyStream(),
                             headers={"content-type": "text/html"})) as client:
        assert await client.web.fetch_room_id_from_html("owner") == "123"
    assert results(caplog)[0]["parsed_room_id"] is True
    caplog.clear()
    entered = asyncio.Event()

    async def stalled(_):
        entered.set()
        await asyncio.Event().wait()

    async with mocked_client(stalled) as client:
        task = asyncio.create_task(client.web.fetch_room_id_from_html("owner"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert results(caplog)[0]["exception_type"] == "CancelledError"
