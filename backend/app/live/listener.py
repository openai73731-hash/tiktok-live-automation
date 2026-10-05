"""The ONLY production module allowed to import TikTokLive.

Bounded non-blocking event delivery prevents SDK callback tasks accumulating.
SDK objects never cross this boundary.
"""
import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from app.config import Settings
from app.live.diagnostics import RoomLookupDiagnostics
from app.live.models import Comment, LiveState, LiveStatus
from app.logging_config import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)
StateCallback = Callable[[LiveStatus], None]


class AmbiguousSDKState(RuntimeError):
    """An SDK response cannot safely be interpreted as a LIVE state."""


class SDKLogFilter(logging.Filter):
    """SDK diagnostics may contain payloads, cookies or signed URLs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return False


def create_sdk_client(*, unique_id: str, diagnostic: bool = False) -> Any:
    """Small, version-pinned adapter; imported lazily so fake mode stays offline."""
    from TikTokLive import TikTokLiveClient
    from TikTokLive.client.errors import UserOfflineError
    from TikTokLive.client.web.routes.fetch_room_id_live_html import FetchRoomIdLiveHTMLRoute
    from TikTokLive.events import ControlEvent, LiveEndEvent, WebsocketResponseEvent
    from TikTokLive.proto import ControlAction

    diagnostics = RoomLookupDiagnostics(FetchRoomIdLiveHTMLRoute.SIGI_PATTERN) if diagnostic else None

    async def check_response(response):
        if diagnostics and await diagnostics.observe_response(response):
            # A challenge must stop even if stale embedded data has a room ID
            # or the HTTP status would normally permit a transient retry.
            raise AmbiguousSDKState()
        # Run before SDK routes can parse an error page or conceal its status.
        response.raise_for_status()
        if diagnostics:
            diagnostics.parsing()

    class ReadOnlyTikTokClient(TikTokLiveClient):
        async def start(self):
            # Upstream start() falls back to a second endpoint even on CAPTCHA
            # or 403. Resolve once, outside that fallback, and fail closed.
            room_id = int(await self.web.fetch_room_id_from_html(self.unique_id))
            if room_id <= 0:
                raise AmbiguousSDKState()
            if diagnostic:
                logger.info("connection_stage", extra={"fields": {"request_stage": "live_check"}})
            # The SDK __call__ catches BaseException (including cancellation).
            # Use its underlying route to keep Ctrl+C and timeouts cancellable.
            alive = await self.web.fetch_is_live.fetch_is_live_room_ids(room_id)
            if len(alive) != 1 or type(alive[0]) is not bool:
                raise AmbiguousSDKState()
            if not alive[0]:
                raise UserOfflineError()
            if diagnostic:
                logger.info("connection_stage", extra={"fields": {"request_stage": "sdk_start"}})
            return await super().start(room_id=room_id, fetch_live_check=False)

        async def _parse_webcast_response_message(self, **kwargs):
            events = await super()._parse_webcast_response_message(**kwargs)
            if not events or all(type(event) is WebsocketResponseEvent for event in events):
                # Upstream silently skips malformed known protobuf events.
                raise AmbiguousSDKState()
            return events

        async def handle_custom_event(self, response, event):
            if isinstance(event, ControlEvent):
                if event.action == ControlAction.STREAM_ENDED:
                    # The listener owns disconnect; upstream spawns an untracked
                    # disconnect task here, racing our cleanup.
                    return LiveEndEvent().parse(response.payload)
                if event.action not in {
                    ControlAction.STREAM_PAUSED, ControlAction.STREAM_UNPAUSED,
                }:
                    # Suspension and unrecognised control actions need a human.
                    raise AmbiguousSDKState()
            return await super().handle_custom_event(response, event)

    client = ReadOnlyTikTokClient(
        unique_id=unique_id,
        web_kwargs={"httpx_kwargs": {"event_hooks": {"response": [check_response]}}},
    )
    client.ignore_broken_payload = True
    client.parse_error_ignorelist = []
    if diagnostics:
        # Instrument both existing routes; start() still calls HTML only.
        # Wrapping the API route does NOT enable or probe the SDK fallback.
        client.web.fetch_room_id_from_html = diagnostics.wrap("html", client.web.fetch_room_id_from_html)
        client.web.fetch_room_id_from_api = diagnostics.wrap("api", client.web.fetch_room_id_from_api)
    return client


class Listener(Protocol):
    status: LiveStatus

    async def run(self) -> None: ...


class BaseListener:
    def __init__(
        self, settings: Settings, queue: asyncio.Queue[Comment],
        on_state: StateCallback | None = None,
    ) -> None:
        self.settings = settings
        self.queue = queue
        self.on_state = on_state
        self.status = LiveStatus(
            username=settings.tiktok_username or "demo", mode=settings.live_mode,
        )

    def set_state(self, state: LiveState, reason: str | None = None) -> None:
        self.status = self.status.model_copy(update={
            "state": state, "reason": reason,
            "terminal_state": state if state in {
                LiveState.ENDED, LiveState.OPERATOR_REQUIRED,
            } else self.status.terminal_state,
            "updated_at": datetime.now(UTC),
        })
        logger.info("live_state", extra={"fields": self.status.model_dump(mode="json")})
        if self.on_state:
            try:
                self.on_state(self.status)
            except Exception as exc:
                logger.error("status_callback_failed", extra={
                    "fields": {"error_type": type(exc).__name__},
                })


class FakeLiveListener(BaseListener):
    MESSAGES = (
        ("john92", "How does the bot work?"),
        ("alice", "Is it automatic?"),
        ("mike", "Hello!"),
    )

    async def run(self) -> None:
        self.set_state(LiveState.CONNECTED)
        try:
            while True:
                for username, text in self.MESSAGES:
                    await asyncio.sleep(self.settings.fake_interval_seconds)
                    await self.queue.put(Comment(username=username, text=text))
                    # Let the consumer display the sample before the next state.
                    await asyncio.sleep(0)
                if not self.settings.fake_repeat:
                    return
        finally:
            self.set_state(LiveState.STOPPED)


def comment_from_event(event: Any) -> Comment:
    """TikTokLive 7.0.1 / v3 schema. Missing optional user ID is normal."""
    user = event.user
    if user is None:
        raise ValueError("Comment has no user")
    username = user.display_id or user.nickname
    raw_id = user.id
    return Comment(
        username=username, user_id=str(raw_id) if raw_id else None,
        text=event.content,
    )


class LiveListener(BaseListener):
    def __init__(
        self, settings: Settings, queue: asyncio.Queue[Comment],
        on_state: StateCallback | None = None,
        *, client_factory: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(settings, queue, on_state)
        self.client_factory = client_factory
        self.halt = asyncio.Event()
        self.connected = asyncio.Event()
        self.dropped_comments = 0

    def on_comment(self, event: Any) -> None:
        if self.halt.is_set():
            return
        try:
            comment = comment_from_event(event)
        except Exception as exc:
            logger.warning("malformed_comment_skipped", extra={
                "fields": {"error_type": type(exc).__name__},
            })
            return
        try:
            self.queue.put_nowait(comment)
        except asyncio.QueueFull:
            self.dropped_comments += 1
            logger.warning("comment_dropped_queue_full", extra={
                "fields": {"dropped_total": self.dropped_comments},
            })

    def on_connect(self, _: Any) -> None:
        if not self.halt.is_set():
            self.connected.set()
            self.set_state(LiveState.CONNECTED)

    def on_disconnect(self, _: Any) -> None:
        if not self.halt.is_set():
            logger.info("sdk_disconnected", extra={"fields": {"event_type": "DisconnectEvent"}})

    def on_end(self, _: Any) -> None:
        self.halt.set()
        if self.status.state != LiveState.OPERATOR_REQUIRED:
            self.set_state(LiveState.ENDED, "LiveEndEvent")

    def on_security(self, event: Any) -> None:
        self.halt.set()
        self.set_state(LiveState.OPERATOR_REQUIRED, type(event).__name__)

    async def _session(self) -> None:
        from TikTokLive.events import (
            AccessControlEvent, CommentEvent, ConnectEvent, DisconnectEvent,
            LiveEndEvent, PerceptionEvent, RoomVerifyEvent, UnknownEvent,
            UnauthorizedMemberEvent,
        )

        if self.client_factory:
            client = self.client_factory(unique_id=self.settings.tiktok_username)
        else:
            client = create_sdk_client(
                unique_id=self.settings.tiktok_username,
                diagnostic=self.settings.diagnostic and self.settings.live_mode == "real",
            )
        sdk_logger = getattr(client, "logger", None)
        sdk_filter = SDKLogFilter()
        if sdk_logger is not None:
            sdk_logger.addFilter(sdk_filter)
        socket_task = None
        halt_task = None
        connected_task = None
        self.connected.clear()
        try:
            for event, handler in (
                (ConnectEvent, self.on_connect), (DisconnectEvent, self.on_disconnect),
                (CommentEvent, self.on_comment), (LiveEndEvent, self.on_end),
                (AccessControlEvent, self.on_security),
                (RoomVerifyEvent, self.on_security),
                (PerceptionEvent, self.on_security),
                (UnauthorizedMemberEvent, self.on_security),
                (UnknownEvent, self.on_security),
            ):
                client.add_listener(event, handler)
            # connect()/run()/close() in 7.0.1 invoke loop-wide cleanup.
            # start() gives us ownership of just this connection task.
            async with asyncio.timeout(self.settings.connect_timeout_seconds):
                socket_task = await client.start()
                if not isinstance(socket_task, asyncio.Task):
                    socket_task = None
                    raise AmbiguousSDKState()
                halt_task = asyncio.create_task(self.halt.wait(), name="live-halt")
                connected_task = asyncio.create_task(self.connected.wait(), name="live-connected")
                done, _ = await asyncio.wait(
                    (socket_task, halt_task, connected_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if socket_task in done:
                    await socket_task
                    return
                if halt_task in done:
                    return
            done, _ = await asyncio.wait(
                (socket_task, halt_task), return_when=asyncio.FIRST_COMPLETED,
            )
            if socket_task in done:
                await socket_task
        finally:
            waiters = [task for task in (halt_task, connected_task) if task is not None]
            for task in waiters:
                task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            try:
                async with asyncio.timeout(self.settings.shutdown_timeout_seconds):
                    await client.disconnect()
            except Exception as exc:
                logger.warning("disconnect_failed", extra={
                    "fields": {"error_type": type(exc).__name__},
                })
            finally:
                if socket_task and not socket_task.done():
                    socket_task.cancel()
                if socket_task:
                    await asyncio.gather(socket_task, return_exceptions=True)
                # 7.0.1 does not put heartbeat cleanup in a finally block.
                # Touch only the task owned by this particular SDK client.
                ping_task = getattr(getattr(client, "_ws", None), "_ping_loop", None)
                if isinstance(ping_task, asyncio.Task):
                    ping_task.cancel()
                    await asyncio.gather(ping_task, return_exceptions=True)
                # Do not call client.close()/disconnect(close_client=True):
                # upstream close() calls run_until_complete and cancels unrelated tasks.
                try:
                    closers = [("http_cleanup_failed", client.web.close)]
                    # web.close() in 7.0.1 closes only TikTok HTTP, leaving
                    # the lazily allocated signing-service HTTP pool open.
                    signer = getattr(client.web, "signer", None)
                    signer_sdk = getattr(signer, "sdk_client", None)
                    sign_http = getattr(signer_sdk, "_async_client", None)
                    if sign_http is not None:
                        closers.append(("signer_cleanup_failed", sign_http.aclose))
                    for event, close in closers:
                        try:
                            async with asyncio.timeout(self.settings.shutdown_timeout_seconds):
                                await close()
                        except Exception as exc:
                            logger.warning(event, extra={
                                "fields": {"error_type": type(exc).__name__},
                            })
                finally:
                    if sdk_logger is not None:
                        sdk_logger.removeFilter(sdk_filter)

    @staticmethod
    def retryable(exc: Exception) -> bool:
        import httpx
        from TikTokLive.client.errors import UserOfflineError
        from websockets.exceptions import ConnectionClosed, InvalidStatus
        from websockets.legacy.exceptions import InvalidStatusCode

        # Unknown/security/signing errors deliberately require an operator.
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {500, 502, 503, 504}
        if isinstance(exc, InvalidStatusCode):
            return exc.status_code in {500, 502, 503, 504}
        if isinstance(exc, InvalidStatus):
            return exc.response.status_code in {500, 502, 503, 504}
        if isinstance(exc, ConnectionClosed):
            # Policy violations / unknown close states must not retry.
            codes = [frame.code for frame in (exc.rcvd, exc.sent) if frame is not None]
            return all(code in {1000, 1001, 1006, 1011, 1012, 1013} for code in codes)
        return isinstance(exc, (
            UserOfflineError, httpx.TransportError,
            ConnectionError, TimeoutError,
        ))

    async def run(self) -> None:
        delay = self.settings.reconnect_initial_seconds
        attempt = 0
        self.set_state(LiveState.STARTING)
        try:
            while not self.halt.is_set():
                self.set_state(LiveState.CONNECTING)
                error_type = "DisconnectEvent"
                try:
                    await self._session()
                    if self.halt.is_set():
                        return
                except Exception as exc:
                    error_type = type(exc).__name__
                    logger.error("listener_error", extra={
                        "fields": {"error_type": type(exc).__name__},
                    })
                    if not self.retryable(exc):
                        self.halt.set()
                        self.set_state(LiveState.OPERATOR_REQUIRED, type(exc).__name__)
                        return
                if self.halt.is_set():
                    return
                if attempt >= self.settings.reconnect_max_attempts:
                    self.halt.set()
                    self.set_state(LiveState.OPERATOR_REQUIRED, "Reconnect attempts exhausted")
                    return
                attempt += 1
                self.set_state(LiveState.RECONNECTING)
                logger.warning("reconnect_scheduled", extra={"fields": {
                    "attempt": attempt, "delay_seconds": delay, "error_type": error_type,
                }})
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.settings.reconnect_max_seconds)
        finally:
            self.halt.set()
            self.set_state(LiveState.STOPPED, self.status.reason)


def create_listener(settings: Settings, queue: asyncio.Queue[Comment]) -> Listener:
    listener_type = FakeLiveListener if settings.live_mode == "fake" else LiveListener
    return listener_type(settings, queue)
