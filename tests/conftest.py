"""Shared test fixtures."""

import io
import wave
from collections.abc import Callable, Iterator
from fractions import Fraction
from typing import BinaryIO

import av
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from transcription_service.core.config import Settings
from transcription_service.domain import (
    AudioMode,
    SegmentResult,
    TranscriptionResult,
)
from transcription_service.main import create_app


class FakeTranscriber:
    """Deterministic model substitute used by fast tests."""

    model_id = "test/fake-whisper"
    supported_languages = frozenset({"en", "he"})

    def __init__(self) -> None:
        self.requested_languages: list[str | None] = []
        self.requested_audio_modes: list[AudioMode] = []

    def transcribe(
        self,
        media: BinaryIO,
        *,
        language: str | None,
        audio_mode: AudioMode,
    ) -> TranscriptionResult:
        self.requested_languages.append(language)
        self.requested_audio_modes.append(audio_mode)
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


def _video_codec(container_format: str) -> str:
    return "libvpx" if container_format == "webm" else "mpeg4"


def _audio_codec(container_format: str) -> str:
    return "libopus" if container_format == "webm" else "aac"


def make_video_bytes(
    container_format: str,
    duration_seconds: float = 0.5,
    *,
    include_audio: bool,
) -> bytes:
    """Create a tiny deterministic video container with optional silent audio."""

    frame_rate = 10
    sample_rate = 48_000
    output = io.BytesIO()

    with av.open(output, mode="w", format=container_format) as container:
        video_stream = container.add_stream(_video_codec(container_format), rate=frame_rate)
        video_stream.width = 16
        video_stream.height = 16
        video_stream.pix_fmt = "yuv420p"

        packets: list[av.Packet] = []
        video_frame_count = round(frame_rate * duration_seconds)
        for frame_number in range(video_frame_count):
            frame = av.VideoFrame(width=16, height=16, format="yuv420p")
            frame.pts = frame_number
            frame.time_base = Fraction(1, frame_rate)
            packets.extend(video_stream.encode(frame))
        packets.extend(video_stream.encode())

        if include_audio:
            audio_stream = container.add_stream(_audio_codec(container_format), rate=sample_rate)
            audio_stream.layout = "mono"
            samples_remaining = round(sample_rate * duration_seconds)
            sample_offset = 0
            while samples_remaining:
                sample_count = min(960, samples_remaining)
                frame = av.AudioFrame(format="s16", layout="mono", samples=sample_count)
                frame.sample_rate = sample_rate
                frame.pts = sample_offset
                frame.time_base = Fraction(1, sample_rate)
                frame.planes[0].update(bytes(frame.planes[0].buffer_size))
                packets.extend(audio_stream.encode(frame))
                sample_offset += sample_count
                samples_remaining -= sample_count
            packets.extend(audio_stream.encode())

        def packet_time(packet: av.Packet) -> float:
            if packet.dts is None or packet.time_base is None:
                return 0
            return float(packet.dts * packet.time_base)

        for packet in sorted(packets, key=packet_time):
            container.mux(packet)

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
def video_with_audio_factory() -> Callable[[str, float], bytes]:
    return lambda container_format, duration: make_video_bytes(
        container_format,
        duration,
        include_audio=True,
    )


@pytest.fixture
def video_without_audio_factory() -> Callable[[str, float], bytes]:
    return lambda container_format, duration: make_video_bytes(
        container_format,
        duration,
        include_audio=False,
    )


@pytest.fixture
def app(settings: Settings, fake_transcriber: FakeTranscriber) -> FastAPI:
    return create_app(settings, transcriber_factory=lambda _: fake_transcriber)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
