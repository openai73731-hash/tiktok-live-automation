import socket
import ipaddress
import logging
import os
from pathlib import Path

import httpx
import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # No unit test may accidentally reach TikTok or a signing service.
    async def no_http(self, request):
        raise AssertionError("Real HTTP forbidden in unit tests; use MockTransport")

    # Loopback sockets below remain necessary for Windows asyncio socketpair,
    # but they must not allow accidental local-model inference.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_http)

    def guard(original):
        def connect(sock, address):
            # Windows asyncio implements socketpair using loopback TCP.
            try:
                loopback = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                raise AssertionError("External network forbidden in unit tests")
            return original(sock, address)
        return connect

    monkeypatch.setattr(socket.socket, "connect", guard(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(socket.socket.connect_ex))
    original_getaddrinfo = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if host not in {None, "localhost", "127.0.0.1", "::1"}:
            raise AssertionError("External DNS forbidden in unit tests")
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    for name in tuple(os.environ):
        if name.upper().startswith('WINDOWS__'): monkeypatch.delenv(name)
    yield
    logger = logging.getLogger("tiktok_assistant")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True


@pytest.fixture
def settings(tmp_path):
    # The ignored operator overlay lives beside project.yaml. Never read the
    # operator's real provider/device settings during default web tests.
    project = tmp_path / "project.yaml"
    project.write_bytes(Path("config/project.yaml").read_bytes())
    return Settings(_env_file=None, fake_interval_seconds=0, project_config_path=project)
