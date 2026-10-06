"""Provider-independent allowlisted failure metadata. Never exception messages."""
from typing import Literal, get_args

ProcessingError = Literal[
    "provider_error", "model_not_found", "model_or_request_error", "timeout", "connection_error",
    "invalid_output", "incomplete_output", "redirect_rejected", "http_error",
    "invalid_models_response", "translation_rejected", "client_closed",
]
ProcessingErrorDetail = Literal[
    "response_too_large", "response_json_invalid", "response_shape_invalid", "choice_count_invalid",
    "non_stop_finish", "empty_content", "refusal", "tool_call", "thinking_output", "markdown_output",
    "json_invalid", "schema_validation_failed", "unknown_field", "missing_field", "invalid_enum",
    "invalid_reason", "selection_inconsistent", "unknown_fact_id", "duplicate_fact_id",
    "language_invalid", "translation_missing", "translation_invalid",
    "translation_language_mismatch", "translation_source_mismatch", "translation_operator_mismatch",
    "translation_untranslated", "translation_claim", "translation_style", "translation_length",
    "translation_sentence_limit",
]
ERROR_CATEGORIES = frozenset(get_args(ProcessingError))
ERROR_DETAILS = frozenset(get_args(ProcessingErrorDetail))


def safe_error(exc) -> tuple[str, str | None]:
    category = getattr(exc, "category", None)
    detail = getattr(exc, "detail", None)
    category = category if type(category) is str and category in ERROR_CATEGORIES else "provider_error"
    detail = detail if type(detail) is str and detail in ERROR_DETAILS else None
    return ("timeout" if isinstance(exc, TimeoutError) else category), detail


def validation_detail(exc) -> str:
    # Inspect only predefined field locations/types. No input, ctx, message or URL.
    errors = exc.errors(include_input=False, include_context=False, include_url=False)
    for error in errors:
        kind, loc = error["type"], error["loc"]
        if kind == "json_invalid": return "json_invalid"
        if kind == "extra_forbidden": return "unknown_field"
        if kind == "missing": return "missing_field"
        if loc == ("reason",): return "invalid_reason"
        if loc in {("detected_language",), ("target_language",)}: return "language_invalid"
        if loc and loc[0] in {"operator_translation", "viewer_language_reply", "operator_reply_translation_ru", "back_translation_en"}:
            return "translation_invalid"
        if kind == "literal_error": return "invalid_enum"
        if loc == () and kind == "value_error": return "selection_inconsistent"
    return "schema_validation_failed"
