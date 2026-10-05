import socket
import ipaddress
import logging

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # No unit test may accidentally reach TikTok or a signing service.
    def guard(original):
        def connect(sock, address):
            # Windows asyncio implements socketpair using loopback TCP.
            if not ipaddress.ip_address(address[0]).is_loopback:
                raise AssertionError("External network forbidden in unit tests")
            return original(sock, address)
        return connect

    monkeypatch.setattr(socket.socket, "connect", guard(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(socket.socket.connect_ex))
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    yield
    logger = logging.getLogger("tiktok_assistant")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True


@pytest.fixture
def settings():
    return Settings(_env_file=None, fake_interval_seconds=0)
