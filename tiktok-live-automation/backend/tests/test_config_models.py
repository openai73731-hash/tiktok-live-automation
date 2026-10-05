from datetime import datetime

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.live.models import Comment


def test_defaults_are_offline_safe(settings):
    assert settings.live_mode == "fake"
    assert settings.tiktok_username == ""


def test_dotenv_environment_and_cli_precedence(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("LIVE_MODE=real\nTIKTOK_USERNAME=@my.account\n", encoding="utf-8")
    assert Settings(_env_file=env).tiktok_username == "my.account"
    monkeypatch.setenv("TIKTOK_USERNAME", "@environment")
    assert Settings(_env_file=env).tiktok_username == "environment"
    assert Settings(_env_file=env, tiktok_username="@cli").tiktok_username == "cli"


@pytest.mark.parametrize("values", [
    {"live_mode": "real"}, {"live_mode": "other"},
    {"tiktok_username": "https://tiktok.com/@account"},
    {"queue_size": 0}, {"reconnect_initial_seconds": 10, "reconnect_max_seconds": 1},
    {"connect_timeout_seconds": 0}, {"fake_repeat": True, "fake_interval_seconds": 0},
    {"fake_interval_seconds": float("nan")},
])
def test_invalid_settings_fail_early(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_comment_receipt_time_is_aware_and_source_fixed():
    comment = Comment(username="alice", text="Привет!")
    assert comment.timestamp.utcoffset().total_seconds() == 0
    assert comment.source == "tiktok"
    assert comment.user_id is None
    with pytest.raises(ValidationError):
        Comment(username="alice", text="Hello", source="other")
    with pytest.raises(ValidationError):
        Comment(username="alice", text="Hello", timestamp=datetime(2026, 1, 1))

