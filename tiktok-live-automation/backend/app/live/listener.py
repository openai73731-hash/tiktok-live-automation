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
from app.live.models import Comment, LiveState, LiveStatus
from app.logging_config import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)
StateCallback = Callable[[LiveStatus], None]


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
            self.set_state(LiveState.CONNECTED)

    def on_disconnect(self, _: Any) -> None:
        if not self.halt.is_set():
            self.set_state(LiveState.DISCONNECTED)

    def on_end(self, _: Any) -> None:
        self.halt.set()
        if self.status.state != LiveState.OPERATOR_REQUIRED:
            self.set_state(LiveState.ENDED, "LIVE ended; no restart in stage one")

    def on_security(self, event: Any) -> None:
        self.halt.set()
        self.set_state(LiveState.OPERATOR_REQUIRED, type(event).__name__)

    async def _session(self) -> None:
        from TikTokLive import TikTokLiveClient
        from TikTokLive.events import (
            AccessControlEvent, CommentEvent, ConnectEvent, DisconnectEvent,
            LiveEndEvent, PerceptionEvent, RoomVerifyEvent, UnknownEvent,
        )

        factory = self.client_factory or TikTokLiveClient
        client = factory(unique_id=self.settings.tiktok_username)
        for event, handler in (
            (ConnectEvent, self.on_connect), (DisconnectEvent, self.on_disconnect),
            (CommentEvent, self.on_comment), (LiveEndEvent, self.on_end),
            (AccessControlEvent, self.on_security),
            (RoomVerifyEvent, self.on_security),
            (PerceptionEvent, self.on_security),
            (UnknownEvent, self.on_security),
        ):
            client.add_listener(event, handler)
        socket_task = None
        halt_task = None
        try:
            # connect()/run()/close() in 7.0.1 invoke loop-wide cleanup.
            # start() gives us ownership of just this connection task.
            async with asyncio.timeout(self.settings.connect_timeout_seconds):
                socket_task = await client.start()
            halt_task = asyncio.create_task(self.halt.wait())
            done, _ = await asyncio.wait(
                (socket_task, halt_task), return_when=asyncio.FIRST_COMPLETED,
            )
            if socket_task in done:
                await socket_task
        finally:
            if halt_task:
                halt_task.cancel()
                await asyncio.gather(halt_task, return_exceptions=True)
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
                # Do not call client.close()/disconnect(close_client=True):
                # upstream close() calls run_until_complete and cancels unrelated tasks.
                try:
                    async with asyncio.timeout(self.settings.shutdown_timeout_seconds):
                        await client.web.close()
                except Exception as exc:
                    logger.warning("http_cleanup_failed", extra={
                        "fields": {"error_type": type(exc).__name__},
                    })

    @staticmethod
    def retryable(exc: Exception) -> bool:
        import httpx
        from TikTokLive.client.errors import UserOfflineError
        from websockets.exceptions import ConnectionClosed

        # Unknown/security/signing errors deliberately require an operator.
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {500, 502, 503, 504}
        return isinstance(exc, (
            UserOfflineError, httpx.TransportError, ConnectionClosed,
            ConnectionError, TimeoutError,
        ))

    async def run(self) -> None:
        from TikTokLive.client.errors import UserOfflineError

        delay = self.settings.reconnect_initial_seconds
        try:
            while not self.halt.is_set():
                self.set_state(LiveState.CONNECTING)
                try:
                    await self._session()
                    if self.halt.is_set():
                        return
                    self.set_state(LiveState.DISCONNECTED)
                except Exception as exc:
                    logger.error("listener_error", extra={
                        "fields": {"error_type": type(exc).__name__},
                    })
                    if not self.retryable(exc):
                        self.halt.set()
                        self.set_state(LiveState.OPERATOR_REQUIRED, type(exc).__name__)
                        return
                    if isinstance(exc, UserOfflineError):
                        self.set_state(LiveState.OFFLINE)
                if self.halt.is_set():
                    return
                self.set_state(LiveState.RETRYING, f"Retry in {delay:g}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.settings.reconnect_max_seconds)
        finally:
            if self.status.state not in {LiveState.ENDED, LiveState.OPERATOR_REQUIRED}:
                self.set_state(LiveState.STOPPED)


def create_listener(settings: Settings, queue: asyncio.Queue[Comment]) -> Listener:
    listener_type = FakeLiveListener if settings.live_mode == "fake" else LiveListener
    return listener_type(settings, queue)
