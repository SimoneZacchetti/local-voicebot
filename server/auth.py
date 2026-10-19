"""Who may talk to the server.

The server listens on two loopback ports (see ``server/__main__.py``): the local one for the
computer's own browser and the remote one, published on the tailnet by ``tailscale serve``. Which
one a request came in on is decided by the listening port, never by headers.

- Local port: Host must be loopback (DNS rebinding) and a page opening /ws must come from an
  allowed origin, so a random website open in Chrome cannot drive the house.
- Remote port: fail-closed. ``tailscale serve`` sets Tailscale-User-Login (overwriting any
  client value); it must be an allowed login, Host must be PUBLIC_HOST, and anything that can
  act (WebSocket, POST...) needs the exact origin or a Bearer token (Siri Shortcuts).
"""

from __future__ import annotations

import hmac
import logging
from email.header import decode_header, make_header

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import cfg

log = logging.getLogger("voicebot.auth")

OPEN_PATHS = {"/healthz"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def is_remote(scope: Scope) -> bool:
    server = scope.get("server")
    return server is not None and server[1] == cfg.remote_port


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def ts_login(headers: dict[str, str]) -> str:
    """Tailscale login of the caller; non-ASCII values come RFC 2047 encoded."""
    raw = headers.get("tailscale-user-login", "")
    try:
        return str(make_header(decode_header(raw))).strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def valid_bearer(headers: dict[str, str]) -> bool:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    # compare against every token (no early exit on match position)
    hits = [hmac.compare_digest(token.strip().encode(), t.encode()) for t in cfg.api_tokens]
    return any(hits)


def _strip_port(host: str) -> str:
    return host.rsplit(":", 1)[0].strip("[]").lower()


def _host(headers: dict[str, str]) -> str:
    return _strip_port(headers.get("host", ""))


def denial(scope: Scope) -> str | None:
    """Why this request must be refused, or None if it may proceed."""
    if scope["path"] in OPEN_PATHS:
        return None
    h = _headers(scope)
    origin = h.get("origin")
    acts = scope["type"] == "websocket" or scope.get("method", "GET") not in SAFE_METHODS
    if not is_remote(scope):
        if _host(h) not in cfg.local_hosts:
            return "host"
        if scope["type"] == "websocket" and origin is not None and origin not in cfg.local_origins:
            return "origin"
        return None
    if not cfg.remote_enabled or not cfg.public_host:
        return "remote disabled"
    if ts_login(h) not in cfg.ts_allowed_logins:
        return "login"
    if _host(h) != _strip_port(cfg.public_host):
        return "host"
    expected = f"https://{cfg.public_host}"
    if origin is not None and origin != expected:
        return "origin"
    if acts and origin != expected and not valid_bearer(h):
        return "origin"
    return None


class RemoteGate:
    """ASGI middleware applying :func:`denial` to every HTTP and WebSocket request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        reason = denial(scope)
        remote = is_remote(scope)
        if reason is None:
            if remote and (scope["type"] == "websocket" or scope["path"].startswith("/api")):
                log.info("remote %s %s login=%s", scope["type"], scope["path"], ts_login(_headers(scope)))
            return await self.app(scope, receive, send)
        log.warning("refused %s %s %s: %s (login=%s)", "remote" if remote else "local", scope["type"],
                    scope["path"], reason, ts_login(_headers(scope)) or "-")
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            return await send({"type": "websocket.close", "code": 1008})
        await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"forbidden"})


# No framing (another site could click "Yes" or "Start talking" in a hidden frame), no MIME sniffing.
SECURITY_HEADERS = [
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]


class SecurityHeaders:
    """ASGI middleware adding :data:`SECURITY_HEADERS` to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *SECURITY_HEADERS]
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def require_token(request: Request) -> None:
    """FastAPI dependency for /api: Authorization: Bearer <one of API_TOKENS>."""
    if not cfg.api_tokens:
        raise HTTPException(503, "API_TOKENS not configured")
    if not valid_bearer({k.lower(): v for k, v in request.headers.items()}):
        raise HTTPException(401, "invalid token")
