"""
API authentication middleware.

The dashboard is publicly reachable on Railway, and several endpoints mutate
state (start/stop trading, close a live position, run the scanner).  Without
authentication anyone on the internet can control the bot.  This module adds a
single shared-secret check in front of every ``/api`` route and the ``/ws``
socket.

Rules
-----
* If ``API_AUTH_KEY`` is empty, authentication is disabled.  This keeps local
  development frictionless.  In production the key is always set.
* A request may present the key in any of these ways:
    - ``X-API-Key: <key>`` header
    - ``Authorization: Bearer <key>`` header
    - ``?api_key=<key>`` query parameter (needed by WebSocket clients)
* ``/health``, ``/docs``, ``/openapi.json`` and the static dashboard assets stay
  public so Railway health checks and the browser work.
"""
from __future__ import annotations

import hmac
from typing import Iterable, Optional

from starlette.datastructures import Headers, QueryParams
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import settings


PUBLIC_PREFIXES: tuple[str, ...] = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/assets/",
    "/favicon.ico",
    "/vite.svg",
)

PROTECTED_PREFIXES: tuple[str, ...] = ("/api", "/ws")


def _extract_key(headers: Headers, query: QueryParams) -> Optional[str]:
    key = headers.get("x-api-key")
    if key:
        return key.strip()

    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()

    # WebSocket clients cannot set headers from the browser, so allow query.
    return (query.get("api_key") or "").strip() or None


def _is_public(path: str) -> bool:
    if path == "/":
        return True
    for prefix in PUBLIC_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    # Static files served by the React build (e.g. /logo.png)
    if "." in path.rsplit("/", 1)[-1] and not path.startswith("/api"):
        return True
    return False


def _is_protected(path: str) -> bool:
    for prefix in PROTECTED_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


class APIKeyMiddleware:
    """Pure ASGI middleware so it works for HTTP *and* WebSocket scopes."""

    def __init__(self, app: ASGIApp, api_key: Optional[str] = None):
        self.app = app
        self._configured_key = api_key if api_key is not None else settings.api_auth_key

    @property
    def enabled(self) -> bool:
        return bool(self._configured_key and self._configured_key.strip())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or "/"

        if not self.enabled or _is_public(path) or not _is_protected(path):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        query = QueryParams(scope.get("query_string", b"").decode("latin-1"))
        provided = _extract_key(headers, query)

        if provided and hmac.compare_digest(provided, self._configured_key.strip()):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # Reject the handshake cleanly.
            await send({"type": "websocket.close", "code": 1008})
            return

        response = JSONResponse(
            {"detail": "Unauthorized: missing or invalid API key"},
            status_code=401,
        )
        await response(scope, receive, send)
