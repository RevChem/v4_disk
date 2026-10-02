"""HTTP middleware: request_id + structlog context.

Чистый ASGI, без BaseHTTPMiddleware: тот буферизует тело ответа и откладывает
первые байты FileResponse, пока файл целиком не прочитан в память.
"""
from __future__ import annotations

import uuid

import structlog


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = ""
        for key, value in scope.get("headers") or []:
            if key == b"x-request-id":
                request_id = value.decode("latin-1").strip()
                break
        if not request_id:
            request_id = uuid.uuid4().hex

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            path=scope.get("path") or "",
            method=scope.get("method") or "",
        )
        request_id_bytes = request_id.encode("latin-1")

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id_bytes))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            structlog.contextvars.clear_contextvars()
