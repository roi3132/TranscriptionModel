"""Application errors and their HTTP representation."""

from collections.abc import Awaitable, Callable, Mapping
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
    """Raised when media duration exceeds the worker processing limit."""

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


class ModelInferenceError(ApplicationError):
    """Raised when the inference engine fails unexpectedly."""

    code = "transcription_failed"
    status_code = HTTPStatus.INTERNAL_SERVER_ERROR
    default_message = "The transcription could not be completed."


class UploadBusyError(ApplicationError):
    """Reject uploads before parsing when all admission slots are occupied."""

    code = "upload_busy"
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    default_message = "The service is busy processing uploads. Try again shortly."

    def __init__(self) -> None:
        super().__init__(headers={"Retry-After": "1"})


class JobNotFoundError(ApplicationError):
    """Raised when a requested transcription job does not exist."""

    code = "job_not_found"
    status_code = HTTPStatus.NOT_FOUND
    default_message = "The transcription job was not found."


class JobResultNotReadyError(ApplicationError):
    """Raised when a job has not produced a result yet."""

    code = "job_result_not_ready"
    status_code = HTTPStatus.CONFLICT
    default_message = "The transcription result is not ready."


class JobInfrastructureError(ApplicationError):
    """Raised when durable job infrastructure is unavailable."""

    code = "job_infrastructure_unavailable"
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    default_message = "The transcription job service is unavailable."


class JobServiceNotReadyError(ApplicationError):
    """Raised when the database, broker, or worker is not ready."""

    code = "job_service_not_ready"
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    default_message = "The transcription job service is not ready."


class InvalidIdempotencyKeyError(ApplicationError):
    """Raised when an idempotency key is unsafe or too long."""

    code = "invalid_idempotency_key"
    status_code = HTTPStatus.BAD_REQUEST
    default_message = "The Idempotency-Key header is invalid."


class IdempotencyConflictError(ApplicationError):
    """Raised when a key is reused for a different request identity."""

    code = "idempotency_conflict"
    status_code = HTTPStatus.CONFLICT
    default_message = "The Idempotency-Key was already used for a different transcription."


class JobQueueFullError(ApplicationError):
    """Raised when the configured number of active jobs is already admitted."""

    code = "job_queue_full"
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    default_message = "The transcription queue is full. Try again later."

    def __init__(self) -> None:
        super().__init__(headers={"Retry-After": "5"})


class MediaStorageFullError(ApplicationError):
    """Raised when retained uploads have exhausted the configured local budget."""

    code = "media_storage_full"
    status_code = HTTPStatus.INSUFFICIENT_STORAGE
    default_message = "The transcription media store is full. Try again later."


class RateLimitExceededError(ApplicationError):
    """Raised when one client exceeds the local upload admission policy."""

    code = "rate_limit_exceeded"
    status_code = HTTPStatus.TOO_MANY_REQUESTS
    default_message = "Too many transcription requests. Try again later."

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(headers={"Retry-After": str(retry_after_seconds)})


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(code=code, message=message, request_id=_request_id(request))
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers={**(headers or {}), "X-Request-ID": _request_id(request)},
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

    message = (
        exc.detail if isinstance(exc.detail, str) else "The HTTP request could not be completed."
    )
    return _error_response(
        request,
        status_code=exc.status_code,
        code="http_error",
        message=message,
        headers=exc.headers,
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    await logger.aexception(
        "unexpected_request_error",
        exception_type=type(exc).__name__,
        request_id=_request_id(request),
        exc_info=(type(exc), exc, exc.__traceback__),
    )
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
