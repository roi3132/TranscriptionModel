"""Shared transcription data and blocking adapter contracts."""

from dataclasses import dataclass
from enum import StrEnum
from typing import BinaryIO, Protocol


class AudioMode(StrEnum):
    SPEECH = "speech"
    MUSIC = "music"


class ModelProfile(StrEnum):
    FASTER = "faster"
    LARGER = "larger"


@dataclass(frozen=True, slots=True)
class NormalizedChunk:
    index: int
    start_seconds: float
    media: BinaryIO


@dataclass(frozen=True, slots=True)
class NormalizedMedia:
    chunks: tuple[NormalizedChunk, ...]
    duration_seconds: float
    is_silent: bool


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
    warnings: tuple[str, ...] = ()


class Transcriber(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def supported_languages(self) -> frozenset[str]: ...

    def transcribe(
        self, media: BinaryIO, *, language: str | None, audio_mode: AudioMode
    ) -> TranscriptionResult: ...


class MediaNormalizer(Protocol):
    def normalize(
        self, media: BinaryIO, *, filename: str | None, content_type: str | None
    ) -> NormalizedMedia: ...
