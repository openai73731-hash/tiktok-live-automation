"""Bounded fan-out on the app event loop; publishers never await browsers."""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import json

from app.admin.models import AIStatus, CommentView, LiveReplyEvent, LiveView, PlaygroundResultEvent, OperatorView
from app.quality.models import QualityUpdate
from app.delivery.models import ApprovalEvent
from app.delivery.service import DeliveryView

PAYLOAD_TYPES = {
    "live_state": LiveView, "comment_received": CommentView,
    "reply_suggestion": LiveReplyEvent, "ai_status": AIStatus,
    "playground_result": PlaygroundResultEvent,
    "quality_update": QualityUpdate,
    "operator_settings": OperatorView,
    "suggestion_approval": ApprovalEvent,
    "delivery_status": DeliveryView,
}


@dataclass(frozen=True)
class AdminEvent:
    id: int
    name: str
    data: str

    def encode(self) -> str:
        return f"id: {self.id}\nevent: {self.name}\ndata: {self.data}\n\n"


@dataclass(eq=False)
class Subscriber:
    queue: asyncio.Queue = field(repr=False)
    overflowed: bool = False


class AdminEventBus:
    def __init__(self, *, queue_size: int = 32, max_subscribers: int = 16):
        if queue_size < 1 or max_subscribers < 1:
            raise ValueError("Positive event limits required")
        self.queue_size = queue_size
        self.max_subscribers = max_subscribers
        self.subscribers: set[Subscriber] = set()
        self.sequence = 0
        self.closed = False

    def subscribe(self) -> Subscriber:
        if self.closed or len(self.subscribers) >= self.max_subscribers:
            raise RuntimeError("Event stream unavailable")
        subscriber = Subscriber(asyncio.Queue(maxsize=self.queue_size))
        self.subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self.subscribers.discard(subscriber)

    @contextmanager
    def subscription(self):
        subscriber = self.subscribe()
        try:
            yield subscriber
        finally:
            self.unsubscribe(subscriber)

    def publish(self, name: str, payload) -> None:
        model = PAYLOAD_TYPES.get(name)
        if model is None or type(payload) is not model:
            raise ValueError("Only allowlisted UI projections may be published")
        if self.closed:
            return
        self.sequence += 1
        event = AdminEvent(self.sequence, name, payload.model_dump_json())
        for subscriber in tuple(self.subscribers):
            if subscriber.queue.full():
                subscriber.queue.get_nowait()
                subscriber.overflowed = True
            subscriber.queue.put_nowait(event)

    def close(self) -> None:
        self.closed = True
        for subscriber in tuple(self.subscribers):
            while not subscriber.queue.empty():
                subscriber.queue.get_nowait()
            subscriber.queue.put_nowait(None)
        self.subscribers.clear()


def sse_message(name: str, payload: dict) -> str:
    # JSON escapes comment newlines, preventing injection of extra SSE frames.
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
