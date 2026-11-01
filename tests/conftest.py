"""Shared test fixtures."""

import io
import wave
from collections.abc import Callable, Iterator
from typing import BinaryIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from transcription_service.core.config import Settings
from transcription_service.main import create_app
from transcription_service.services.transcription import (
    SegmentResult,
    TranscriptionResult,
)


class FakeTranscriber:
    """Deterministic model substitute used by fast tests."""

    model_id = "test/fake-whisper"
    supported_languages = frozenset({"en", "he"})

    def __init__(self) -> None:
        self.requested_languages: list[str | None] = []

    def transcribe(
        self,
        media: BinaryIO,
        *,
        language: str | None,
    ) -> TranscriptionResult:
        self.requested_languages.append(language)
        detected_language = language or "he"
        text = "שלום עולם" if detected_language == "he" else "Hello world"
        return TranscriptionResult(
            text=text,
            language=detected_language,
            language_probability=0.98,
            duration_seconds=0.25,
            segments=(SegmentResult(start=0, end=0.25, text=text),),
        )


def make_wav_bytes(duration_seconds: float = 0.25) -> bytes:
    """Create a small, deterministic mono PCM WAV without external tools."""

    sample_rate = 16_000
    frame_count = round(sample_rate * duration_seconds)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(frame_count * 2))
    return output.getvalue()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, environment="test", log_level="CRITICAL")


@pytest.fixture
def fake_transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
def wav_factory() -> Callable[[float], bytes]:
    return make_wav_bytes


@pytest.fixture
def app(settings: Settings, fake_transcriber: FakeTranscriber) -> FastAPI:
    return create_app(settings, transcriber_factory=lambda _: fake_transcriber)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
