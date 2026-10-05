import json
import logging

from app.logging_config import configure_logging


def test_structured_unicode_logs_rotation_and_console(settings, tmp_path, capsys):
    settings = settings.model_copy(update={"log_dir": tmp_path, "log_max_bytes": 1024})
    logger = configure_logging(settings)
    try:
        logger.info("comment_received", extra={"fields": {
            "username": "alice", "text": "Привет!\u001b[31m",
        }})
        output = capsys.readouterr().out
        assert "alice:" in output
        assert "Привет!" in output
        assert "\u001b" not in output
        record = json.loads((tmp_path / "app.jsonl").read_text(encoding="utf-8"))
        assert record["event"] == "comment_received"
        assert record["text"] == "Привет!\u001b[31m"
        for _ in range(30):
            logger.info("rotation_test", extra={"fields": {"detail": "x" * 100}})
        assert (tmp_path / "app.jsonl.1").exists()
        assert len(list(tmp_path.glob("app.jsonl*"))) <= 4
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.propagate = True


def test_configure_logging_is_idempotent(settings, tmp_path):
    settings = settings.model_copy(update={"log_dir": tmp_path})
    configure_logging(settings)
    logger = configure_logging(settings)
    try:
        assert len(logger.handlers) == 2
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.propagate = True

