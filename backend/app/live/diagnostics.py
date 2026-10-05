"""Allowlisted room-lookup metadata. Never retain or log HTTP payloads."""
import asyncio
from contextvars import ContextVar
import json
import logging
import re
from typing import Any

from app.logging_config import LOGGER_NAME
from app.live.security import classify_security

logger = logging.getLogger(LOGGER_NAME)


class InvalidRoomLookupResponse(RuntimeError):
    """A lookup result is ambiguous and must not be used for connecting."""


def positive_room_id(value: Any) -> bool:
    text = str(value)
    return (
        type(value) in (str, int) and text.isascii() and text.isdecimal()
        and bool(text.strip("0"))
    )


class RoomLookupDiagnostics:
    """Classify SDK lookups for guards; optionally emit allowlisted telemetry."""

    def __init__(self, html_pattern: re.Pattern, *, enabled: bool = True) -> None:
        # Use the installed SDK's exact pattern, not a more permissive parser.
        self.html_pattern = html_pattern
        self.enabled = enabled
        self.current: ContextVar[dict | None] = ContextVar("room_lookup", default=None)
        self.last_result: ContextVar[dict | None] = ContextVar("room_lookup_result", default=None)

    def allow_api_fallback(self) -> bool:
        fields = self.last_result.get() or {}
        return (
            fields.get("room_lookup_strategy") == "html"
            and fields.get("request_stage") == "sdk_parse"
            and fields.get("http_status") == 200
            and fields.get("response_category") == "html"
            and fields.get("redirect") is False
            and fields.get("blocked_or_challenge") is False
            and fields.get("exception_type") == "FailedParseRoomIdError"
            and fields.get("sdk_error_site") == "html.parse_room_id"
        )

    def wrap(self, strategy: str, route):
        async def lookup(*args, **kwargs):
            fields: dict[str, Any] = {
                "room_lookup_strategy": strategy,
                "request_stage": "request_started",
                "http_status": None,
                "content_type": None,
                "response_category": None,
                "response_length": None,
                "redirect": False,
                "blocked_or_challenge": False,
                "challenge_reason_codes": [],
                "parsed_room_id": False,
                "exception_type": None,
                "sdk_error_site": None,
                "sdk_error_line": None,
            }
            if strategy == "html":
                fields.update(expected_room_id_marker_found=False,
                              expected_embedded_json_marker_found=False,
                              embedded_json_valid=False)
            else:
                fields.update(valid_json=False, data_present=False,
                              user_present=False, room_id_field_present=False,
                              room_id_value_valid=False)
            self.last_result.set(None)
            token = self.current.set(fields)
            if self.enabled:
                logger.info("room_lookup_started", extra={"fields": fields.copy()})
            try:
                result = await route(*args, **kwargs)
                # Validate the original API value too: SDK int() accepts bools
                # and truncates floats, which are not trustworthy room IDs.
                if not positive_room_id(result) or (strategy == "api" and not (
                    fields["http_status"] == 200
                    and fields["response_category"] == "json"
                    and fields["room_id_value_valid"]
                )):
                    raise InvalidRoomLookupResponse()
                fields["parsed_room_id"] = True
                fields["request_stage"] = "lookup_complete"
                return result
            except asyncio.CancelledError:
                fields["exception_type"] = "CancelledError"
                raise
            except Exception as exc:
                # Exception messages/tracebacks may contain HTTP bodies or URLs.
                fields["exception_type"] = type(exc).__name__
                trace = exc.__traceback__
                while trace:
                    code = trace.tb_frame.f_code
                    # Only literal method labels and a line number, never a
                    # traceback, local variables or an absolute SDK file path.
                    for suffix, site in (
                        ("fetch_room_id_live_html.py", "html.parse_room_id"),
                        ("fetch_room_id_api.py", "api.parse_room_id"),
                    ):
                        if code.co_filename.endswith(suffix) and code.co_name == "parse_room_id":
                            fields.update(sdk_error_site=site, sdk_error_line=trace.tb_lineno)
                    trace = trace.tb_next
                raise
            finally:
                self.last_result.set(fields.copy())
                if self.enabled:
                    logger.info("room_lookup_result", extra={"fields": fields.copy()})
                self.current.reset(token)

        return lookup

    async def observe_response(self, response) -> bool:
        fields = self.current.get()
        if fields is None:
            return False  # Exclude live-check, signing and all unrelated requests.
        fields["request_stage"] = "http_response"
        fields["http_status"] = response.status_code
        fields["redirect"] = 300 <= response.status_code < 400
        # Do not echo arbitrary headers, even a malformed Content-Type value.
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        fields["content_type"] = media_type if media_type in {
            "text/html", "application/xhtml+xml", "application/json",
            "text/plain", "application/octet-stream",
        } else ("other" if media_type else "missing")
        # HTTPX response hooks run before the body is read. The SDK route needs
        # that same body next; aread() caches it without issuing another request.
        await response.aread()
        fields["response_length"] = len(response.content)
        body = response.text
        try:
            payload = response.json()
            valid_json = True
        except (ValueError, UnicodeError):
            payload = None
            valid_json = False
        html = media_type in {"text/html", "application/xhtml+xml"} or bool(
            re.match(r"\s*(?:<!doctype\s+html|<html|<head|<body|<script)\b", body, re.I)
        )
        has_live_state = False
        if fields["room_lookup_strategy"] == "html":
            match = self.html_pattern.search(body)
            fields["expected_embedded_json_marker_found"] = match is not None
            embedded = None
            if match:
                try:
                    embedded = json.loads(match.group(1))
                    fields["embedded_json_valid"] = True
                except ValueError:
                    pass
            user = embedded
            for key in ("LiveRoom", "liveRoomUserInfo", "user"):
                user = user.get(key) if isinstance(user, dict) else None
            fields["expected_room_id_marker_found"] = isinstance(user, dict) and "roomId" in user
            has_live_state = isinstance(user, dict) and positive_room_id(user.get("roomId"))
        else:
            data = payload.get("data") if isinstance(payload, dict) else None
            user = data.get("user") if isinstance(data, dict) else None
            fields.update(
                valid_json=valid_json, data_present=isinstance(data, dict),
                user_present=isinstance(user, dict),
                room_id_field_present=isinstance(user, dict) and "roomId" in user,
                room_id_value_valid=isinstance(user, dict) and positive_room_id(user.get("roomId")),
            )
        blocked, reasons = classify_security(
            status=response.status_code, body=body, payload=payload,
            is_html=html, has_live_state=has_live_state,
        )
        fields["blocked_or_challenge"] = blocked
        fields["challenge_reason_codes"] = reasons
        if blocked:
            category = "blocked_or_challenge"
        elif fields["redirect"]:
            category = "redirect"
        elif not response.content.strip():
            category = "empty"
        elif valid_json:
            category = "json"
        elif html:
            category = "html"
        else:
            category = "unexpected"
        fields["response_category"] = category
        # Preserve HTTPStatusError for status guards; structural hard blocks
        # raise AmbiguousSDKState in the adapter even on a successful status.
        return blocked and response.status_code not in {401, 403, 429}

    def parsing(self) -> None:
        fields = self.current.get()
        if fields is not None:
            fields["request_stage"] = "sdk_parse"
