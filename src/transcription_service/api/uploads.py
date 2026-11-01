"""Bound multipart parsing before FastAPI resolves upload dependencies."""

from collections.abc import Callable, Coroutine
from typing import Any

import anyio
from fastapi import Request, Response
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.types import Message

from transcription_service.core.config import Settings
from transcription_service.core.errors import UploadBusyError, UploadTooLargeError
from transcription_service.core.rate_limit import LocalRateLimiter

# Separate the file limit from a small allowance for fields, boundaries, and part headers.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class BoundedUploadRoute(APIRoute):
    """Use Starlette's parser and cleanup with a bounded incoming byte stream."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded_handler(request: Request) -> Response:
            if request.method != "POST":
                return await handler(request)
            settings: Settings = request.app.state.settings
            limiter: LocalRateLimiter = request.app.state.rate_limiter
            limiter.check(request.client.host if request.client is not None else "unknown")
            body_limit = settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise HTTPException(400, "Invalid Content-Length.") from exc
                if declared_size < 0:
                    raise HTTPException(400, "Invalid Content-Length.")
                if declared_size > body_limit:
                    raise UploadTooLargeError(settings.max_upload_bytes)

            received_bytes = 0
            exceeded = False

            async def receive_limited() -> Message:
                nonlocal received_bytes, exceeded
                message = await request.receive()
                if message["type"] == "http.request":
                    received_bytes += len(message.get("body", b""))
                    if received_bytes > body_limit:
                        exceeded = True
                        # Let the multipart parser close partial files before mapping to HTTP 413.
                        raise MultiPartException("Request body exceeds the upload limit.")
                return message

            bounded_request = Request(request.scope, receive=receive_limited)
            slots: anyio.CapacityLimiter = request.app.state.upload_slots
            try:
                slots.acquire_nowait()
            except anyio.WouldBlock as exc:
                raise UploadBusyError from exc
            try:
                # Parsing is cached by Request, so FastAPI reuses the form and its files.
                async with bounded_request.form(max_files=1, max_fields=3, max_part_size=4096):
                    return await handler(bounded_request)
            except HTTPException as exc:
                if exceeded:
                    raise UploadTooLargeError(settings.max_upload_bytes) from exc
                raise
            finally:
                slots.release()

        return bounded_handler
