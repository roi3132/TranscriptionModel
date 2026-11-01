"""faster-whisper implementation of the transcription port."""

from typing import BinaryIO, Self

from faster_whisper import WhisperModel

from transcription_service.core.config import Settings
from transcription_service.services.transcription import (
    SegmentResult,
    TranscriptionResult,
)


class FasterWhisperTranscriber:
    """Thin adapter around a process-wide CTranslate2 Whisper model."""

    def __init__(
        self,
        model: WhisperModel,
        *,
        model_id: str,
        beam_size: int,
        vad_filter: bool,
    ) -> None:
        self._model = model
        self._model_id = model_id
        self._beam_size = beam_size
        self._vad_filter = vad_filter
        self._supported_languages = frozenset(model.supported_languages)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Download/load the configured model and construct the adapter."""

        model = WhisperModel(
            settings.model_id,
            device=settings.device,
            compute_type=settings.compute_type,
            revision=settings.model_revision,
        )
        return cls(
            model,
            model_id=settings.model_id,
            beam_size=settings.beam_size,
            vad_filter=settings.vad_filter,
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def supported_languages(self) -> frozenset[str]:
        return self._supported_languages

    def transcribe(
        self,
        media: BinaryIO,
        *,
        language: str | None,
    ) -> TranscriptionResult:
        """Run blocking inference and eagerly consume the lazy segment generator."""

        media.seek(0)
        segments, info = self._model.transcribe(
            media,
            language=language,
            task="transcribe",
            beam_size=self._beam_size,
            vad_filter=self._vad_filter,
        )
        segment_results = tuple(
            SegmentResult(start=segment.start, end=segment.end, text=segment.text.strip())
            for segment in segments
            if segment.text.strip()
        )
        text = " ".join(segment.text for segment in segment_results).strip()

        return TranscriptionResult(
            text=text,
            language=info.language,
            language_probability=info.language_probability,
            duration_seconds=info.duration,
            segments=segment_results,
        )
