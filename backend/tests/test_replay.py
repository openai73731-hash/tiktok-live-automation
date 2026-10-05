import asyncio
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
from typing import get_args
import urllib.request

import httpx
from pydantic import ValidationError
import pytest

from app.ai.factory import create_processor
from app.ai.fake_engine import FakeAIReplyEngine
from app.config import ReplayScenarioName, Settings
from app.live.models import LiveState
from app.live.replay import EventFixture, ReplayClient, ReplayContractError, ReplayDriver, ReplayWeb, ScenarioFixture, run_replay
from app.live.service import LiveService
from app.logging_config import configure_logging
from app.main import run_listener

SCENARIOS = get_args(ReplayScenarioName)


def replay_settings(name, **kwargs):
    return Settings(_env_file=None, live_mode="replay", replay_scenario=name, **kwargs)


@pytest.mark.parametrize("name", SCENARIOS)
async def test_every_replay_scenario_with_all_network_entrypoints_blocked(name, tmp_path, monkeypatch, capsys):
    settings = replay_settings(name, log_dir=tmp_path)
    configure_logging(settings)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Replay attempted network or HTTP client creation")

    before = asyncio.all_tasks()
    # The asyncio loop already exists. No socket creation (even loopback), DNS,
    # real client creation or HTTP calls are allowed during integration replay.
    with monkeypatch.context() as m:
        for attr in ("socket", "create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            m.setattr(socket, attr, forbidden)
        for cls in (httpx.Client, httpx.AsyncClient):
            m.setattr(cls, "__init__", forbidden)
            m.setattr(cls, "send", forbidden)
        m.setattr(http.client.HTTPConnection, "connect", forbidden)
        m.setattr(urllib.request, "urlopen", forbidden)
        m.setattr("app.live.listener.create_sdk_client", forbidden)
        result = await run_replay(settings)
    assert calls == []  # Even an attempted call swallowed by the listener fails the test.
    assert result["passed"] is True
    assert result["failed_checks"] == []
    assert result["final_state"] == "STOPPED"
    assert not (asyncio.all_tasks() - before)
    if name in {"html_challenge", "rate_limit"}:
        assert result["requests"] == ["html"]
        assert result["states_visited"] == ["STARTING", "CONNECTING", "NEED_OPERATOR", "STOPPED"]
    if name == "temporary_disconnect":
        assert result["states_visited"] == ["STARTING", "CONNECTING", "CONNECTED",
            "RECONNECTING", "CONNECTING", "CONNECTED", "ENDED", "STOPPED"]
    if name == "live_with_comments":
        assert result["comments_processed"] == result["reply_suggestions"] == 2
        assert result["suggestion_statuses"] == ["skipped", "ready"]
    raw = (tmp_path / "app.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    for record in records:
        if record["event"] == "reply_suggestion":
            item = record["suggestion"]
            assert item["requires_approval"] is True and item["provider"] == "fake"
            if item["status"] == "ready":
                assert item["operator_translation"] and item["reply_translation"]
                assert item["fact_ids"]
    assert sum(r["event"] == "SAFE_API_FALLBACK" for r in records) == (
        1 if name in {"html_fail_api_success", "api_missing_room"} else 0)
    assert sum(r["event"] == "reconnect_scheduled" for r in records) == (1 if name == "temporary_disconnect" else 0)
    stdout = capsys.readouterr().out
    assert "REPLAY RESULT: PASS" in stdout
    assert "REPLAY room_lookup:" in stdout
    for secret in ("<html>", "SIGI_STATE", '"roomId"', "sessionid", "set-cookie", "https://", "access_token"):
        assert secret not in raw + stdout


@pytest.mark.parametrize("name", SCENARIOS)
async def test_replay_result_is_deterministic_and_expected_blocks_exit_zero(name):
    first = await run_replay(replay_settings(name))
    second = await run_replay(replay_settings(name))
    assert first == second
    assert await run_listener(replay_settings(name)) == 0


@pytest.mark.parametrize("values", [
    {"live_mode": "replay"},
    {"live_mode": "replay", "replay_scenario": "../private"},
    {"live_mode": "fake", "replay_scenario": "live_end"},
    {"live_mode": "real", "tiktok_username": "owner", "replay_scenario": "live_end"},
])
def test_replay_settings_require_known_scenario_and_mode(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_replay_uses_synthetic_handle_and_fake_provider(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text(Path("config/project.yaml").read_text(encoding="utf-8").replace(
        "provider: fake", "provider: openai"), encoding="utf-8")
    settings = replay_settings("live_end", tiktok_username="some_account", project_config_path=project)
    assert settings.tiktok_username == "replay"
    assert isinstance(create_processor(settings).engine, FakeAIReplyEngine)
    assert "provider: openai" in project.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["real", "fake"])
async def test_replay_entrypoint_cannot_accept_non_replay_settings(mode, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid replay settings must not construct any listener")

    monkeypatch.setattr("app.live.service.create_listener", forbidden)
    with pytest.raises(ValueError, match="requires replay settings"):
        await run_replay(Settings(_env_file=None, live_mode=mode, tiktok_username="owner"))


async def test_wrong_expectation_produces_fail_and_nonzero_exit(monkeypatch, tmp_path, capsys):
    original = Path("config/replay_scenarios/live_end.json").read_text(encoding="utf-8")
    data = json.loads(original)
    data["expected"]["comments"] = 1
    read_text = Path.read_text

    def changed(path, *args, **kwargs):
        return json.dumps(data) if path.name == "live_end.json" else read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", changed)
    settings = replay_settings("live_end", log_dir=tmp_path)
    configure_logging(settings)
    assert await run_listener(settings) == 1
    assert "REPLAY RESULT: FAIL" in capsys.readouterr().out


async def test_unexpected_fixture_request_fails_closed():
    fixture = ScenarioFixture.model_validate_json(Path("config/replay_scenarios/live_end.json").read_text())
    driver = ReplayDriver(fixture)

    async def hook(_):
        pytest.fail("Unexpected request must not produce a response")

    web = ReplayWeb(fixture.sessions[0], driver, hook)
    with pytest.raises(ReplayContractError):
        await web.get(url="https://replay.invalid/unexpected")
    assert driver.failures == ["unexpected_request"]
    assert driver.requests == []


async def test_cancellation_releases_replay_tasks_and_preserves_unrelated_task(monkeypatch, caplog):
    entered = asyncio.Event()

    async def wait_for_cancel(client):
        event = client.event_factory(EventFixture(type="connect"))
        client.handlers[type(event)](event)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ReplayClient, "_events", wait_for_cancel)
    service = LiveService(replay_settings("live_end"))
    unrelated = asyncio.create_task(asyncio.sleep(3600))
    task = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        client, = service.listener.replay.clients
        assert client.web.closed and client.disconnected.is_set() and client.socket_task.done()
        assert not unrelated.done()
        assert service.status.state == LiveState.STOPPED
        assert service.status.terminal_state is None
        assert not any(r.msg == "replay_result" for r in caplog.records)
    finally:
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)


def test_fixture_files_are_synthetic_and_only_contain_supported_fields():
    paths = sorted(Path("config/replay_scenarios").glob("*.json"))
    assert {p.stem for p in paths} == set(SCENARIOS)
    for path in paths:
        raw = path.read_text(encoding="utf-8")
        fixture = ScenarioFixture.model_validate_json(raw)
        for forbidden in ("https://", "http://", "antilame", "sessionid", "cookie", "authorization",
                          "access_token", "password", "headers"):
            assert forbidden not in raw.lower()
        for session in fixture.sessions:
            for response in session.responses:
                assert response.status in {200, 429}
                if 'roomId' in response.body:
                    assert '"roomId": "101"' in response.body


@pytest.mark.parametrize("scenario", ["live_with_comments", "html_challenge"])
def test_replay_cli_output_and_exit_code(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    completed = subprocess.run([sys.executable, "-m", "app.main", "--mode", "replay",
        "--scenario", scenario, "--env-file", str(tmp_path / "absent")],
        capture_output=True, encoding="utf-8", timeout=20)
    assert completed.returncode == 0
    assert "STARTING @replay" in completed.stdout
    assert "REPLAY RESULT: PASS" in completed.stdout
    assert "final state: STOPPED" in completed.stdout
    assert "Traceback" not in completed.stderr
    if scenario == "live_with_comments":
        for text in ("@test_user:", "Hello", "status: skipped", "status: ready", "provider: fake",
                     "requires_approval: true", "ENDED @replay", "comments processed: 2"):
            assert text in completed.stdout
