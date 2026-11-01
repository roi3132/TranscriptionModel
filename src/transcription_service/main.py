"""FastAPI application factory and process lifecycle."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

import anyio
import structlog
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from transcription_service import __version__
from transcription_service.adapters.faster_whisper import FasterWhisperTranscriber
from transcription_service.adapters.media import PyAvAudioInspector
from transcription_service.api.health import router as health_router
from transcription_service.api.transcriptions import router as transcription_router
from transcription_service.core.config import Settings, get_settings
from transcription_service.core.errors import register_exception_handlers
from transcription_service.core.logging import configure_logging
from transcription_service.core.status import RuntimeStatus
from transcription_service.services.transcription import (
    MediaInspector,
    TranscriberFactory,
    TranscriptionService,
)

logger = structlog.get_logger(__name__)
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _request_id(header_value: str | None) -> str:
    if header_value and REQUEST_ID_PATTERN.fullmatch(header_value):
        return header_value
    return uuid4().hex


def _default_transcriber_factory(settings: Settings) -> FasterWhisperTranscriber:
    return FasterWhisperTranscriber.from_settings(settings)


def create_app(
    settings: Settings | None = None,
    *,
    transcriber_factory: TranscriberFactory = _default_transcriber_factory,
    media_inspector: MediaInspector | None = None,
) -> FastAPI:
    """Build an application instance with explicit, testable dependencies."""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    runtime_status = RuntimeStatus()
    resolved_media_inspector = media_inspector or PyAvAudioInspector()

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI) -> AsyncIterator[None]:
        await logger.ainfo(
            "application_started",
            environment=resolved_settings.environment,
            version=__version__,
        )
        try:
            try:
                transcriber = await anyio.to_thread.run_sync(
                    transcriber_factory,
                    resolved_settings,
                )
                lifespan_app.state.transcription_service = TranscriptionService(
                    transcriber,
                    resolved_media_inspector,
                    resolved_settings,
                )
                runtime_status.mark_ready()
                await logger.ainfo("transcription_model_ready", model=transcriber.model_id)
            except Exception as exc:
                runtime_status.mark_unavailable(type(exc).__name__)
                await logger.aexception(
                    "transcription_model_initialization_failed",
                    exception_type=type(exc).__name__,
                )
            yield
        finally:
            runtime_status.mark_unavailable()
            lifespan_app.state.transcription_service = None
            await logger.ainfo("application_stopped")

    application = FastAPI(
        title=resolved_settings.application_name,
        version=__version__,
        description="Upload media and receive a timestamped transcription.",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.runtime_status = runtime_status
    application.state.transcription_service = None

    register_exception_handlers(application)
    application.include_router(health_router)
    application.include_router(transcription_router, prefix=resolved_settings.api_prefix)

    @application.middleware("http")
    async def request_context(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = _request_id(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started_at = perf_counter()

        try:
            response: Response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            await logger.ainfo(
                "request_completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    return application


app = create_app()
