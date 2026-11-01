"""Upload validation and bounded execution of the transcription pipeline."""

import asyncio
from functools import partial
from time import perf_counter
from typing import BinaryIO

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
from transcription_service.domain import (
    AudioMode,
    CompletedTranscription,
    MediaInspector,
    ModelProfile,
    Transcriber,
    TranscriberFactory,
)
from transcription_service.services.models import ModelRegistry

logger = structlog.get_logger(__name__)


class TranscriptionService:
    """Share one capacity limit across decoding, model loading, and inference."""

    def __init__(
        self,
        transcriber: Transcriber,
        media_inspector: MediaInspector,
        settings: Settings,
        *,
        transcriber_factory: TranscriberFactory | None = None,
    ) -> None:
        self._models = ModelRegistry(transcriber, settings, transcriber_factory)
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
        model_profile: ModelProfile = ModelProfile.FASTER,
    ) -> CompletedTranscription:
        if size_bytes <= 0:
            raise EmptyUploadError
        if size_bytes > self._settings.max_upload_bytes:
            raise UploadTooLargeError(self._settings.max_upload_bytes)

        try:
            await asyncio.wait_for(
                self._inference_slots.acquire(),
                timeout=self._settings.inference_queue_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ModelBusyError(self._settings.inference_queue_timeout_seconds) from exc

        try:
            # Cancellation must not close a stream or release capacity while its thread uses it.
            worker = asyncio.create_task(
                anyio.to_thread.run_sync(
                    partial(
                        self._transcribe_blocking,
                        media,
                        filename=filename,
                        content_type=content_type,
                        language=language,
                        audio_mode=audio_mode,
                        model_profile=model_profile,
                    )
                )
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    while not worker.done():
                        try:
                            await asyncio.shield(worker)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break  # The blocking pipeline logs and sanitizes its own failures.
                    if not worker.cancelled():
                        worker.exception()  # Retrieve failures even when the caller has gone away.
                raise
        finally:
            self._inference_slots.release()

    def _transcribe_blocking(
        self,
        media: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
        language: str | None,
        audio_mode: AudioMode,
        model_profile: ModelProfile,
    ) -> CompletedTranscription:
        started_at = perf_counter()
        try:
            metadata = self._media_inspector.inspect(
                media, filename=filename, content_type=content_type
            )
            self._validate_duration(metadata.duration_seconds)
            transcriber = self._models.get(model_profile)
            if language is not None and language not in transcriber.supported_languages:
                raise InvalidLanguageError(language)

            media.seek(0)
            result = transcriber.transcribe(media, language=language, audio_mode=audio_mode)
            self._validate_duration(result.duration_seconds)
            return CompletedTranscription(
                result=result,
                processing_time_seconds=perf_counter() - started_at,
                model_id=transcriber.model_id,
            )
        except ApplicationError:
            raise
        except Exception as exc:
            logger.exception("transcription_pipeline_failed", model_profile=model_profile.value)
            raise ModelInferenceError from exc

    def _validate_duration(self, duration_seconds: float | None) -> None:
        if (
            duration_seconds is not None
            and duration_seconds > self._settings.max_media_duration_seconds
        ):
            raise MediaTooLongError(self._settings.max_media_duration_seconds)
