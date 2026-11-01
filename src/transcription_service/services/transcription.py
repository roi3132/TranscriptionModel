"""Model-independent transcription types, ports, and use case."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from time import perf_counter
from typing import BinaryIO, Protocol, runtime_checkable

import anyio
import structlog

from transcription_service.core.config import Settings
from transcription_service.core.errors import (
    ApplicationError,
    EmptyUploadError,
    InvalidLanguageError,
    MediaTooLongError,
    ModelBusyError,
    ModelInferenceError,
    UploadTooLargeError,
)

logger = structlog.get_logger(__name__)


class AudioMode(StrEnum):
    """User-facing processing profile for the uploaded audio track."""

    SPEECH = "speech"
    MUSIC = "music"


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    """Metadata read from a media container before inference."""

    duration_seconds: float | None
    format_name: str


@dataclass(frozen=True, slots=True)
class SegmentResult:
    """One timestamped model result."""

    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Model output before it is mapped to the public API schema."""

    text: str
    language: str
    language_probability: float
    duration_seconds: float
    segments: tuple[SegmentResult, ...]


@dataclass(frozen=True, slots=True)
class CompletedTranscription:
    """A model result enriched with application execution metadata."""

    result: TranscriptionResult
    processing_time_seconds: float
    model_id: str


@runtime_checkable
class Transcriber(Protocol):
    """Blocking model inference port."""

    @property
    def model_id(self) -> str:
        """Public identifier of the configured model."""
        ...

    @property
    def supported_languages(self) -> frozenset[str]:
        """Language codes accepted by the configured model."""
        ...

    def transcribe(
        self,
        media: BinaryIO,
        *,
        language: str | None,
        audio_mode: AudioMode,
    ) -> TranscriptionResult:
        """Transcribe the current media stream position into timestamped text."""
        ...


@runtime_checkable
class MediaInspector(Protocol):
    """Port for validating a container and reading its audio metadata."""

    def inspect(
        self,
        media: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
    ) -> MediaMetadata:
        """Inspect media without taking ownership of the stream."""
        ...


class TranscriptionService:
    """Apply upload guardrails and coordinate bounded model inference."""

    def __init__(
        self,
        transcriber: Transcriber,
        media_inspector: MediaInspector,
        settings: Settings,
    ) -> None:
        self._transcriber = transcriber
        self._media_inspector = media_inspector
        self._settings = settings
        self._inference_slots = asyncio.Semaphore(settings.max_concurrent_transcriptions)

    async def transcribe(
        self,
        media: BinaryIO,
        *,
        size_bytes: int,
        filename: str | None,
        content_type: str | None,
        language: str | None,
        audio_mode: AudioMode,
    ) -> CompletedTranscription:
        """Validate an upload, wait for capacity, and execute inference off-loop."""

        self._validate_size(size_bytes)
        self._validate_language(language)

        metadata = await anyio.to_thread.run_sync(
            partial(
                self._media_inspector.inspect,
                media,
                filename=filename,
                content_type=content_type,
            )
        )
        self._validate_duration(metadata.duration_seconds)

        try:
            await asyncio.wait_for(
                self._inference_slots.acquire(),
                timeout=self._settings.inference_queue_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ModelBusyError(self._settings.inference_queue_timeout_seconds) from exc

        started_at = perf_counter()
        try:
            media.seek(0)
            result = await anyio.to_thread.run_sync(
                partial(
                    self._transcriber.transcribe,
                    media,
                    language=language,
                    audio_mode=audio_mode,
                )
            )
        except ApplicationError:
            raise
        except Exception as exc:
            await logger.aexception(
                "model_inference_failed",
                model=self._transcriber.model_id,
                media_format=metadata.format_name,
            )
            raise ModelInferenceError from exc
        finally:
            self._inference_slots.release()

        self._validate_duration(result.duration_seconds)
        return CompletedTranscription(
            result=result,
            processing_time_seconds=perf_counter() - started_at,
            model_id=self._transcriber.model_id,
        )

    def _validate_size(self, size_bytes: int) -> None:
        if size_bytes <= 0:
            raise EmptyUploadError
        if size_bytes > self._settings.max_upload_bytes:
            raise UploadTooLargeError(self._settings.max_upload_bytes)

    def _validate_language(self, language: str | None) -> None:
        if language is not None and language not in self._transcriber.supported_languages:
            raise InvalidLanguageError(language)

    def _validate_duration(self, duration_seconds: float | None) -> None:
        if (
            duration_seconds is not None
            and duration_seconds > self._settings.max_media_duration_seconds
        ):
            raise MediaTooLongError(self._settings.max_media_duration_seconds)


TranscriberFactory = Callable[[Settings], Transcriber]
