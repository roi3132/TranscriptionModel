"""Shared transcription data and blocking adapter contracts."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import BinaryIO, Protocol

from transcription_service.core.config import Settings


class AudioMode(StrEnum):
    SPEECH = "speech"
    MUSIC = "music"


class ModelProfile(StrEnum):
    FASTER = "faster"
    LARGER = "larger"


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    duration_seconds: float | None
    format_name: str


@dataclass(frozen=True, slots=True)
class SegmentResult:
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    language: str
    language_probability: float
    duration_seconds: float
    segments: tuple[SegmentResult, ...]


@dataclass(frozen=True, slots=True)
class CompletedTranscription:
    result: TranscriptionResult
    processing_time_seconds: float
    model_id: str


class Transcriber(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def supported_languages(self) -> frozenset[str]: ...

    def transcribe(
        self, media: BinaryIO, *, language: str | None, audio_mode: AudioMode
    ) -> TranscriptionResult: ...


class MediaInspector(Protocol):
    def inspect(
        self, media: BinaryIO, *, filename: str | None, content_type: str | None
    ) -> MediaMetadata: ...


TranscriberFactory = Callable[[Settings], Transcriber]
