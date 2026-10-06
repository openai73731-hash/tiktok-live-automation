"""Loopback and same-origin guard with per-lifespan CSRF; no login system."""
import ipaddress
import secrets
from urllib.parse import urlsplit

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

SECURITY_HEADERS = {
    "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


def authority(value: str, *, scheme: str, test_peer: bool = False):
    if any(c.isspace() for c in value) or any(c in value for c in "@/?#\\"):
        return None
    try:
        parsed = urlsplit("//" + value)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} | ({"testserver"} if test_peer else set()):
            return None
        port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
        if not 1 <= port <= 65535:
            return None
        return scheme, parsed.hostname, port
    except ValueError:
        return None


class LocalAdminMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def secured_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.decode().lower() not in SECURITY_HEADERS]
                headers += [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                message = {**message, "headers": headers}
            await send(message)

        async def reject(status, category):
            await JSONResponse({"error": category}, status_code=status)(scope, receive, secured_send)

        peer = (scope.get("client") or ("", 0))[0]
        test_peer = peer == "testclient"  # Starlette TestClient sentinel, never a TCP peer.
        try:
            local_peer = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local_peer = test_peer
        headers = scope.get("headers", [])
        hosts = [v.decode("latin-1") for k, v in headers if k.lower() == b"host"]
        scheme = scope.get("scheme", "http")
        host = authority(hosts[0], scheme=scheme, test_peer=test_peer) if len(hosts) == 1 else None
        if not local_peer or host is None:
            await reject(403, "local_access_only")
            return
        origins = [v.decode("latin-1") for k, v in headers if k.lower() == b"origin"]
        if origins:
            valid_origin = False
            if len(origins) == 1:
                try:
                    origin = urlsplit(origins[0])
                    if origin.scheme in {"http", "https"} and not origin.path and not origin.query and not origin.fragment:
                        valid_origin = authority(origin.netloc, scheme=origin.scheme, test_peer=test_peer) == host
                except ValueError:
                    pass
            if not valid_origin:
                await reject(403, "same_origin_required")
                return
        if scope["method"] not in {"GET", "HEAD"}:
            admin = getattr(scope["app"].state, "admin", None)
            tokens = [v.decode("latin-1") for k, v in headers if k.lower() == b"x-admin-csrf"]
            if admin is None or len(tokens) != 1 or not secrets.compare_digest(
                tokens[0].encode("latin-1"), admin.csrf_token.encode("ascii"),
            ):
                await reject(403, "csrf_required")
                return
            content_types = [v.decode("latin-1") for k, v in headers if k.lower() == b"content-type"]
            if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
                await reject(415, "json_required")
                return
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > 65_536:
                raise HTTPException(status_code=413, detail="Request too large")
            return message

        try:
            await self.app(scope, bounded_receive, secured_send)
        except Exception as exc:
            # Starlette's outer ServerErrorMiddleware re-raises even handled
            # errors to uvicorn. Handle here so exception bodies never reach
            # the server's traceback logger. Cancellation still propagates.
            response = await scope["app"].exception_handlers[Exception](Request(scope), exc)
            if not response_started:
                await response(scope, receive, secured_send)
            else:
                await secured_send({"type": "http.response.body", "body": b"", "more_body": False})
