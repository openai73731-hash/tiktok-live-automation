"""JSONL files with rotation and a readable console, using stdlib logging."""
import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

from app.config import Settings

LOGGER_NAME = "tiktok_assistant"


def console_text(value: str) -> str:
    # Viewer text must not send terminal escape/control sequences.
    return "".join(c if c.isprintable() else " " for c in value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        result = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        result.update(getattr(record, "fields", {}))
        return json.dumps(result, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        time = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        if record.msg == "reply_suggestion":
            item = fields["suggestion"]
            safe = lambda value: console_text(str(value)) if value else "—"
            action = {
                "ready": "Предложение не отправлено. Решение — за оператором.",
                "skipped": "Ответ не требуется.",
                "needs_operator": "Нужна ручная обработка; ответ не подготовлен.",
            }[item["status"]]
            return (
                f"[{time}] Предложение для @{safe(item['username'])} "
                f"[{safe(item['provider'])}; {safe(item['status'])}]\n"
                f"  Оригинал ({safe(item['detected_language'])}): {safe(item['original_text'])}\n"
                f"  Перевод: {safe(item['operator_translation'])}\n"
                f"  Ответ: {safe(item['suggested_reply'])}\n"
                f"  Перевод ответа: {safe(item['reply_translation'])}\n"
                f"  Причина: {safe(item['reason'])}\n"
                f"  {action}\n"
            )
        if record.msg == "comment_received":
            return (
                f"[{time}] {console_text(fields['username'])}:\n"
                f"{console_text(fields['text'])}\n"
            )
        if record.msg == "live_state":
            suffix = f" ({fields['reason']})" if fields.get("reason") else ""
            mode = " [fake]" if fields["mode"] == "fake" else ""
            return (
                f"[{time}] LIVE {fields['state']}: @{fields['username']}"
                f"{mode}{suffix}\n"
            )
        return f"[{time}] {record.levelname} {record.getMessage()} {fields}"


def configure_logging(settings: Settings) -> logging.Logger:
    # Windows redirected stdout otherwise uses an ANSI code page and corrupts
    # Russian operator text for UTF-8 log collectors and subprocess consumers.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    # Idempotent for app factories and tests.
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    file_handler = RotatingFileHandler(
        settings.log_dir / "app.jsonl", maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count, encoding="utf-8",
    )
    file_handler.setLevel(settings.log_level)
    file_handler.setFormatter(JsonFormatter())
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(ConsoleFormatter())
    logger.addHandler(file_handler)
    logger.addHandler(console)
    return logger
