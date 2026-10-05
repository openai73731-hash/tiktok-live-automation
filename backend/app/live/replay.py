"""Deterministic fixtures around the real listener, with no network backend."""
import asyncio
import logging
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import Settings
from app.live.listener import LiveListener, create_replay_client
from app.live.models import LiveState
from app.logging_config import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)
Stage = Literal["html", "api", "live_check"]


class FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResponseFixture(FixtureModel):
    stage: Stage
    status: int = Field(ge=100, le=599)
    content_type: Literal["text/html", "application/json", "text/plain"]
    body: str = Field(max_length=100_000)


class EventFixture(FixtureModel):
    type: Literal["connect", "comment", "end", "transport_failure", "security"]
    username: str = ""
    text: str = ""

    @model_validator(mode="after")
    def validate_comment(self):
        if self.type == "comment" and (not self.username or not self.text):
            raise ValueError("Replay comments require username and text")
        return self


class SessionFixture(FixtureModel):
    responses: list[ResponseFixture] = Field(min_length=1, max_length=3)
    events: list[EventFixture] = Field(max_length=100)


class ExpectedFixture(FixtureModel):
    states: list[LiveState]
    requests: list[Stage]
    comments: int = Field(ge=0)
    suggestions: list[Literal["ready", "skipped", "needs_operator"]]
    terminal_state: Literal["ended", "operator_required"]


class ScenarioFixture(FixtureModel):
    sessions: list[SessionFixture] = Field(min_length=1, max_length=5)
    expected: ExpectedFixture


class ReplayContractError(RuntimeError):
    """Unexpected fixture access. Never fall through to real I/O."""


class ReplayWeb:
    """SDK web facade: returns in-memory Response objects, creates no HTTP client."""

    def __init__(self, session, driver, response_hook):
        self.session = session
        self.driver = driver
        self.response_hook = response_hook
        self.cursor = 0
        self.closed = False

    async def get(self, *, url, **kwargs):
        stage = {
            "/@replay/live": "html", "/api-live/user/room/": "api",
            "/webcast/room/check_alive/": "live_check",
        }.get(urlsplit(url).path)
        if self.closed or stage is None or self.cursor >= len(self.session.responses):
            self.driver.fail("unexpected_request")
        fixture = self.session.responses[self.cursor]
        if stage != fixture.stage or set(kwargs) - {"base_params", "extra_params"}:
            self.driver.fail("request_order")
        self.cursor += 1
        self.driver.requests.append(stage)
        # Only synthetic Content-Type. The SDK URL is neither visited nor logged.
        response = httpx.Response(fixture.status, content=fixture.body.encode("utf-8"),
            headers={"content-type": fixture.content_type},
            request=httpx.Request("GET", "https://replay.invalid/fixture"))
        await self.response_hook(response)
        return response

    async def close(self):
        self.closed = True


class ReplayClient:
    """Replay only the transport boundary; listener owns state transitions/cleanup."""

    def __init__(self, session, driver, web, connection, event_factory):
        self.session = session
        self.driver = driver
        self.web = web
        self.connection = connection
        self.event_factory = event_factory
        self.handlers = {}
        self.disconnected = asyncio.Event()
        self.socket_task = None
        self.events_delivered = 0

    @property
    def _room_lookup_pending(self):
        return self.connection.pending

    def add_listener(self, event_type, handler):
        self.handlers[event_type] = handler

    async def start(self):
        await self.connection.resolve(self.web, "replay")
        self.socket_task = asyncio.create_task(self._events(), name="replay-events")
        return self.socket_task

    async def _events(self):
        for frame in self.session.events:
            self.events_delivered += 1
            if frame.type == "transport_failure":
                raise ConnectionError("Synthetic replay transport failure")
            event = self.event_factory(frame)
            handler = self.handlers.get(type(event))
            if handler is None:
                self.driver.fail("unhandled_event")
            handler(event)  # Same synchronous callbacks registered by LiveListener.
            await asyncio.sleep(0)
            if frame.type in {"end", "security"}:
                await self.disconnected.wait()
                return
        self.driver.fail("missing_terminal_event")

    async def disconnect(self):
        self.disconnected.set()


class ReplayDriver:
    def __init__(self, scenario):
        self.scenario = scenario
        self.clients = []
        self.states = []
        self.requests = []
        self.failures = []

    def fail(self, code):
        self.failures.append(code)
        raise ReplayContractError()

    def client(self, *, operator_required, **kwargs):
        if len(self.clients) >= len(self.scenario.sessions):
            self.fail("unexpected_session")
        client = create_replay_client(self.scenario.sessions[len(self.clients)], self,
                                      operator_required=operator_required)
        self.clients.append(client)
        return client

    def result(self, name, status, comments, suggestions):
        expected = self.scenario.expected
        checks = {
            "states": self.states == expected.states,
            "requests": self.requests == expected.requests,
            "comments": comments == expected.comments,
            "suggestions": [s.status for s in suggestions] == expected.suggestions,
            "approval": all(s.requires_approval and s.provider == "fake" for s in suggestions),
            "final_state": status.state == LiveState.STOPPED,
            "terminal_state": status.terminal_state == expected.terminal_state,
            "sessions": len(self.clients) == len(self.scenario.sessions),
            "fixtures_consumed": all(c.web.cursor == len(c.session.responses)
                and c.events_delivered == len(c.session.events) for c in self.clients),
            "shutdown": all(c.web.closed and c.disconnected.is_set()
                and (c.socket_task is None or c.socket_task.done()) for c in self.clients),
            "fixture_contract": not self.failures,
        }
        display = lambda s: "NEED_OPERATOR" if s == LiveState.NEED_OPERATOR else s.upper()
        return {
            "scenario": name, "passed": all(checks.values()),
            "states_visited": [display(s) for s in self.states],
            "comments_processed": comments, "reply_suggestions": len(suggestions),
            "suggestion_statuses": [s.status for s in suggestions],
            "requests": self.requests.copy(),
            "final_state": display(status.state),
            "terminal_state": display(status.terminal_state) if status.terminal_state else None,
            "failed_checks": [key for key, ok in checks.items() if not ok],
        }


class ReplayListener(LiveListener):
    def __init__(self, settings, queue):
        # Name is validated by Settings; fixtures contain no URLs or real accounts.
        path = Path("config/replay_scenarios") / f"{settings.replay_scenario}.json"
        scenario = ScenarioFixture.model_validate_json(path.read_text(encoding="utf-8"))
        self.replay = ReplayDriver(scenario)
        # Keep production retry algorithm/budget; shorten only replay's clock delays.
        replay_settings = settings.model_copy(update={
            "reconnect_initial_seconds": 0.001, "reconnect_max_seconds": 0.004,
        })
        super().__init__(replay_settings, queue,
            on_state=lambda status: self.replay.states.append(status.state),
            client_factory=lambda **kwargs: self.replay.client(operator_required=self.halt.is_set, **kwargs))


async def run_replay(settings: Settings) -> dict:
    from app.ai.factory import create_processor
    from app.live.service import LiveService

    if settings.live_mode != "replay" or settings.replay_scenario is None:
        raise ValueError("run_replay requires replay settings")
    processor = create_processor(settings)
    suggestions = []
    comments = 0

    async def handle(comment):
        nonlocal comments
        comments += 1
        suggestions.append(await processor.handle(comment))

    service = LiveService(settings, on_comment=handle)
    await service.run()
    result = service.listener.replay.result(settings.replay_scenario, service.status, comments, suggestions)
    logger.info("replay_result", extra={"fields": result})
    return result
