"""Upload validation and bounded execution of the transcription pipeline."""

import os
from pathlib import Path
from time import perf_counter
from typing import BinaryIO

import structlog
from pydantic import TypeAdapter, ValidationError

from transcription_service.core.config import Settings
from transcription_service.core.errors import (
    ApplicationError,
    EmptyUploadError,
    InvalidLanguageError,
    ModelInferenceError,
    UploadTooLargeError,
)
from transcription_service.domain import (
    AudioMode,
    CompletedTranscription,
    MediaNormalizer,
    NormalizedMedia,
    SegmentResult,
    Transcriber,
    TranscriptionResult,
)

logger = structlog.get_logger(__name__)
checkpoint_adapter = TypeAdapter(dict[int, TranscriptionResult])


class TranscriptionService:
    """Share one capacity limit across decoding, model loading, and inference."""

    def __init__(
        self,
        transcriber: Transcriber,
        media_normalizer: MediaNormalizer,
        settings: Settings,
    ) -> None:
        self._transcriber = transcriber
        self._media_normalizer = media_normalizer
        self._settings = settings

    def transcribe(
        self,
        media: BinaryIO,
        *,
        size_bytes: int,
        filename: str | None,
        content_type: str | None,
        language: str | None,
        audio_mode: AudioMode,
        checkpoint_path: Path | None = None,
    ) -> CompletedTranscription:
        if size_bytes <= 0:
            raise EmptyUploadError
        if size_bytes > self._settings.max_upload_bytes:
            raise UploadTooLargeError(self._settings.max_upload_bytes)

        started_at = perf_counter()
        normalized: NormalizedMedia | None = None
        try:
            normalized = self._media_normalizer.normalize(
                media, filename=filename, content_type=content_type
            )
            if normalized.is_silent:
                return CompletedTranscription(
                    result=TranscriptionResult(
                        text="",
                        language=language or "und",
                        language_probability=1.0 if language else 0.0,
                        duration_seconds=normalized.duration_seconds,
                        segments=(),
                    ),
                    processing_time_seconds=perf_counter() - started_at,
                    model_id=self._transcriber.model_id,
                    warnings=("no_speech_detected",),
                )

            transcriber = self._transcriber
            if language is not None and language not in transcriber.supported_languages:
                raise InvalidLanguageError(language)

            results = self._load_checkpoint(checkpoint_path)
            for chunk in normalized.chunks:
                if chunk.index in results:
                    continue
                for attempt in range(1, self._settings.chunk_max_attempts + 1):
                    try:
                        chunk.media.seek(0)
                        results[chunk.index] = transcriber.transcribe(
                            chunk.media,
                            language=language,
                            audio_mode=audio_mode,
                        )
                        self._save_checkpoint(checkpoint_path, results)
                        break
                    except Exception:
                        if attempt >= self._settings.chunk_max_attempts:
                            raise
                        logger.warning(
                            "transcription_chunk_retry",
                            chunk_index=chunk.index,
                            attempt=attempt,
                        )

            result = self._assemble(normalized, results)
            return CompletedTranscription(
                result=result,
                processing_time_seconds=perf_counter() - started_at,
                model_id=transcriber.model_id,
                warnings=("no_speech_detected",) if not result.text else (),
            )
        except ApplicationError:
            raise
        except Exception as exc:
            logger.exception("transcription_pipeline_failed", model=self._transcriber.model_id)
            raise ModelInferenceError from exc
        finally:
            if normalized is not None:
                for chunk in normalized.chunks:
                    chunk.media.close()

    @staticmethod
    def _assemble(
        normalized: NormalizedMedia,
        results: dict[int, TranscriptionResult],
    ) -> TranscriptionResult:
        ordered = [(chunk, results[chunk.index]) for chunk in normalized.chunks]
        segments: list[SegmentResult] = []
        for chunk, result in ordered:
            for segment in result.segments:
                start = chunk.start_seconds + segment.start
                end = min(normalized.duration_seconds, chunk.start_seconds + segment.end)
                if start < end:
                    segments.append(SegmentResult(start=start, end=end, text=segment.text))
        best_language = max(
            (result for _, result in ordered),
            key=lambda result: result.language_probability,
        )
        return TranscriptionResult(
            text=" ".join(result.text for _, result in ordered if result.text).strip(),
            language=best_language.language,
            language_probability=best_language.language_probability,
            duration_seconds=normalized.duration_seconds,
            segments=tuple(segments),
        )

    @staticmethod
    def _load_checkpoint(path: Path | None) -> dict[int, TranscriptionResult]:
        if path is None or not path.exists():
            return {}
        try:
            return checkpoint_adapter.validate_json(path.read_bytes())
        except (OSError, ValidationError):
            logger.warning("transcription_checkpoint_invalid", path=str(path))
            return {}

    @staticmethod
    def _save_checkpoint(
        path: Path | None,
        results: dict[int, TranscriptionResult],
    ) -> None:
        if path is None:
            return
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_bytes(checkpoint_adapter.dump_json(results))
        os.replace(temporary, path)
