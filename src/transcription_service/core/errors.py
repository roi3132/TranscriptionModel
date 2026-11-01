"""Application errors and their HTTP representation."""

from collections.abc import Awaitable, Callable
from http import HTTPStatus

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from transcription_service.schemas.common import ErrorDetail, ErrorResponse

logger = structlog.get_logger(__name__)
ExceptionHandler = Callable[[Request, Exception], Awaitable[Response]]


class ApplicationError(Exception):
    """A known failure that is safe to represent to an API client."""

    code = "application_error"
    status_code = HTTPStatus.INTERNAL_SERVER_ERROR
    default_message = "The request could not be completed."

    def __init__(
        self,
        message: str | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.client_message = message or self.default_message
        self.headers = headers
        super().__init__(self.client_message)


class ModelNotReadyError(ApplicationError):
    """Raised while the inference model is unavailable."""

    code = "model_not_ready"
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    default_message = "The transcription model is not ready."


class EmptyUploadError(ApplicationError):
    """Raised when the uploaded file has no content."""

    code = "empty_upload"
    status_code = HTTPStatus.BAD_REQUEST
    default_message = "The uploaded file is empty."


class UploadTooLargeError(ApplicationError):
    """Raised when an upload exceeds the configured byte limit."""

    code = "upload_too_large"
    status_code = HTTPStatus.REQUEST_ENTITY_TOO_LARGE

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"The uploaded file exceeds the {limit_bytes} byte limit.")


class UnsupportedMediaError(ApplicationError):
    """Raised when the media container cannot be accepted or decoded."""

    code = "unsupported_media"
    status_code = HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    default_message = "The uploaded media format is unsupported or could not be decoded."


class NoAudioStreamError(ApplicationError):
    """Raised when a readable container has no audio stream."""

    code = "no_audio_stream"
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    default_message = "The uploaded media does not contain an audio stream."


class MediaTooLongError(ApplicationError):
    """Raised when media duration exceeds the synchronous API limit."""

    code = "media_too_long"
    status_code = HTTPStatus.REQUEST_ENTITY_TOO_LARGE

    def __init__(self, limit_seconds: float) -> None:
        super().__init__(f"The media exceeds the {limit_seconds:g} second duration limit.")


class InvalidLanguageError(ApplicationError):
    """Raised for a language code not supported by the configured model."""

    code = "invalid_language"
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY

    def __init__(self, language: str) -> None:
        super().__init__(f"Language code '{language}' is not supported by the configured model.")


class ModelBusyError(ApplicationError):
    """Raised when inference capacity is unavailable within the queue timeout."""

    code = "model_busy"
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    default_message = "The transcription model is busy. Try again shortly."

    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(
            headers={"Retry-After": str(max(1, round(retry_after_seconds)))},
        )


class ModelInferenceError(ApplicationError):
    """Raised when the inference engine fails unexpectedly."""

    code = "transcription_failed"
    status_code = HTTPStatus.INTERNAL_SERVER_ERROR
    default_message = "The transcription could not be completed."


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(code=code, message=message, request_id=_request_id(request))
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


async def _handle_application_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, ApplicationError):
        raise TypeError("application error handler received an unexpected exception")

    log = logger.aerror if exc.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR else logger.awarning
    await log("request_rejected", error_code=exc.code, status_code=int(exc.status_code))
    return _error_response(
        request,
        status_code=int(exc.status_code),
        code=exc.code,
        message=exc.client_message,
        headers=exc.headers,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):
        raise TypeError("validation error handler received an unexpected exception")

    await logger.awarning("request_validation_failed", error_count=len(exc.errors()))
    return _error_response(
        request,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        code="validation_error",
        message="The request parameters are invalid.",
    )


async def _handle_http_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        raise TypeError("HTTP error handler received an unexpected exception")

    message = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
    return _error_response(
        request,
        status_code=exc.status_code,
        code="http_error",
        message=message,
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    await logger.aexception("unexpected_request_error", exception_type=type(exc).__name__)
    return _error_response(
        request,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        code="internal_error",
        message="An unexpected error occurred.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install stable error envelopes without leaking internal exception details."""

    handlers: dict[type[Exception], ExceptionHandler] = {
        ApplicationError: _handle_application_error,
        RequestValidationError: _handle_validation_error,
        StarletteHTTPException: _handle_http_error,
        Exception: _handle_unexpected_error,
    }
    for exception_type, handler in handlers.items():
        app.add_exception_handler(exception_type, handler)
