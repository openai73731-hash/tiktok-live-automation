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


class InstanceFilter(logging.Filter):
    def __init__(self, instance_id):
        super().__init__()
        self.instance_id = instance_id

    def filter(self, record):
        record.fields = {**getattr(record, "fields", {}), "instance_id": self.instance_id}
        return True


class ConsoleFormatter(logging.Formatter):
    def __init__(self, diagnostic: bool = False, replay: bool = False):
        super().__init__()
        self.diagnostic = diagnostic
        self.replay = replay

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        time = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        if self.replay and record.msg == "room_lookup_result":
            outcome = "success" if fields["parsed_room_id"] else "failure"
            return (f"[{time}] REPLAY room_lookup: {fields['room_lookup_strategy']} {outcome} "
                    f"{fields}")
        if record.msg == "replay_result":
            return (f"REPLAY RESULT: {'PASS' if fields['passed'] else 'FAIL'}\n"
                    f"  scenario: {fields['scenario']}\n"
                    f"  states visited: {' -> '.join(fields['states_visited'])}\n"
                    f"  comments processed: {fields['comments_processed']}\n"
                    f"  reply suggestions: {fields['reply_suggestions']}\n"
                    f"  final state: {fields['final_state']}\n"
                    f"  terminal state: {fields['terminal_state']}\n"
                    f"  failed checks: {', '.join(fields['failed_checks']) or 'none'}")
        if record.msg in {"reply_suggestion", "ai_smoke_result"}:
            item = fields["suggestion"]
            safe = lambda value: console_text(str(value)) if value else "—"
            if self.diagnostic or record.msg == "ai_smoke_result":
                return (
                    f"[{time}] ReplySuggestion @{safe(item['username'])}:\n"
                    f"  status: {item['status']}\n"
                    f"  original: {safe(item['original_text'])}\n"
                    f"  detected_language: {safe(item['detected_language'])}\n"
                    f"  should_reply: {str(item['should_reply']).lower()}\n"
                    f"  fact_ids: {item['fact_ids']}\n"
                    f"  intent: {safe(item.get('intent'))}\n"
                    f"  operator_translation: {safe(item['operator_translation'])}\n"
                    f"  suggested_reply: {safe(item['suggested_reply'])}\n"
                    f"  reply_translation: {safe(item['reply_translation'])}\n"
                    f"  provider: {safe(item['provider'])}\n"
                    f"  model: {safe(item.get('model'))}\n"
                    f"  requires_approval: {str(item['requires_approval']).lower()}\n"
                    f"  translation_requires_review: {str(item.get('translation_requires_review', False)).lower()}\n"
                    f"  auto_send_eligible: false\n"
                    f"  reason: {safe(item['reason'])}\n"
                )
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
                f"[{time}] {'@' if self.diagnostic else ''}{console_text(fields['username'])}:\n"
                f"{console_text(fields['text'])}\n"
            )
        if record.msg == "live_state":
            if self.diagnostic:
                state = {"operator_required": "NEED_OPERATOR"}.get(
                    fields['state'], fields['state'].upper(),
                )
                reason = console_text(fields.get('reason') or '')
                suffix = f" ({reason})" if reason else ""
                return f"[{time}] {state} @{console_text(fields['username'])}{suffix}"
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
    file_handler.addFilter(InstanceFilter(settings.instance_id))
    file_handler.setFormatter(JsonFormatter())
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.addFilter(InstanceFilter(settings.instance_id))
    console.setFormatter(ConsoleFormatter(
        settings.diagnostic or settings.live_mode == "replay", replay=settings.live_mode == "replay",
    ))
    logger.addHandler(file_handler)
    logger.addHandler(console)
    return logger
