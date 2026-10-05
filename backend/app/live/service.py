"""Application-facing service; no SDK types or imports."""
import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.config import Settings
from app.live.listener import Listener, create_listener
from app.live.models import Comment, LiveStatus
from app.logging_config import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)
CommentCallback = Callable[[Comment], Awaitable[object]]


class LiveService:
    def __init__(
        self, settings: Settings, *, on_comment: CommentCallback | None = None,
    ) -> None:
        self.queue: asyncio.Queue[Comment] = asyncio.Queue(maxsize=settings.queue_size)
        self.listener: Listener = create_listener(settings, self.queue)
        self.on_comment = on_comment
        self._running = False

    @property
    def status(self) -> LiveStatus:
        return self.listener.status

    async def _consume(self) -> None:
        while True:
            comment = await self.queue.get()
            try:
                logger.info("comment_received", extra={
                    "fields": comment.model_dump(mode="json"),
                })
                if self.on_comment:
                    await self.on_comment(comment)
            except Exception as exc:
                logger.error("comment_consumer_failed", extra={
                    "fields": {"error_type": type(exc).__name__},
                })
            finally:
                self.queue.task_done()

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("Service is already running")
        self._running = True
        consumer = asyncio.create_task(self._consume(), name="comment-consumer")
        producer = asyncio.create_task(self.listener.run(), name="live-listener")
        try:
            await producer
            # A finite fake run and LIVE end deliver all already accepted comments.
            await self.queue.join()
        finally:
            producer.cancel()
            consumer.cancel()
            await asyncio.gather(producer, consumer, return_exceptions=True)
            self._running = False
