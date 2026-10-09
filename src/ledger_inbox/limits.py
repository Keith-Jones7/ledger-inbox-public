"""Bound the multipart stream before Starlette spools uploaded files to disk."""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class UploadLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("path") != "/v1/ingest/images"
            or scope.get("method") != "POST"
        ):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        raw = headers.get(b"content-length")
        if raw:
            try:
                size = int(raw)
            except ValueError:
                return await JSONResponse(
                    {"detail": "invalid content length"}, status_code=400
                )(scope, receive, send)
            if size > self.max_bytes:
                return await JSONResponse(
                    {"detail": "request is too large"}, status_code=413
                )(scope, receive, send)
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                raise HTTPException(413, "request is too large")
            return message

        await self.app(scope, limited_receive, send)
